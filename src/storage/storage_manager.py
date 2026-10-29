"""Atomic JSON state persistence and corruption recovery for ByteSprite."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.engine.models import PetState, PetStats

logger = logging.getLogger(__name__)


@dataclass
class PetSaveData:
    """Deserialized snapshot of pet simulation state and UI preferences."""

    stats: PetStats = field(default_factory=PetStats)
    state: PetState = PetState.IDLE
    position: tuple[int, int] | None = None
    always_on_top: bool = True
    auto_wander: bool = True


def _parse_position(window_dict: Any) -> tuple[int, int] | None:
    if not isinstance(window_dict, dict):
        return None
    raw_x = window_dict.get("x")
    raw_y = window_dict.get("y")
    if isinstance(raw_x, bool) or isinstance(raw_y, bool):
        return None
    if raw_x is None or raw_y is None:
        return None
    try:
        fx = float(raw_x)
        fy = float(raw_y)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(fx) and math.isfinite(fy)):
        return None
    return (int(fx), int(fy))


class StorageManager:
    """Persists and restores ByteSprite state at ~/.bytesprite/state.json."""

    SCHEMA_VERSION: int = 1

    def __init__(self, state_path: Path | str | None = None) -> None:
        if state_path is None:
            self._state_path = Path.home() / ".bytesprite" / "state.json"
        else:
            self._state_path = Path(state_path)

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> PetSaveData:
        """Load saved state, recovering gracefully to defaults on missing or corrupt files."""
        if not self._state_path.exists():
            return PetSaveData()

        try:
            raw_text = self._state_path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Corrupt state file at %s (%s); falling back to defaults.", self._state_path, exc)
            self._backup_corrupt_file()
            return PetSaveData()

        if not isinstance(payload, dict):
            logger.warning("Invalid root JSON type in %s; falling back to defaults.", self._state_path)
            return PetSaveData()

        stats_raw = payload.get("stats")
        stats = PetStats.from_dict(stats_raw) if isinstance(stats_raw, dict) else PetStats()

        raw_state = payload.get("state")
        try:
            state = PetState(raw_state)
        except ValueError:
            state = PetState.IDLE

        window_raw = payload.get("window")
        position = _parse_position(window_raw)
        always_on_top = True
        auto_wander = True
        if isinstance(window_raw, dict):
            if isinstance(window_raw.get("always_on_top"), bool):
                always_on_top = window_raw["always_on_top"]
            if isinstance(window_raw.get("auto_wander"), bool):
                auto_wander = window_raw["auto_wander"]

        return PetSaveData(
            stats=stats,
            state=state,
            position=position,
            always_on_top=always_on_top,
            auto_wander=auto_wander,
        )

    def save(
        self,
        stats: PetStats,
        state: PetState,
        *,
        position: tuple[int, int] | None = None,
        always_on_top: bool = True,
        auto_wander: bool = True,
    ) -> bool:
        """Atomically serialize state to disk using temp-file + os.replace."""
        window_payload: dict[str, Any] = {
            "always_on_top": bool(always_on_top),
            "auto_wander": bool(auto_wander),
        }
        if position is not None:
            window_payload["x"] = int(position[0])
            window_payload["y"] = int(position[1])

        document = {
            "version": self.SCHEMA_VERSION,
            "state": state.value if isinstance(state, PetState) else str(state),
            "stats": stats.to_dict(),
            "window": window_payload,
        }

        temp_path: Path | None = None
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(document, indent=2, sort_keys=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._state_path.parent),
                prefix=".state_",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                temp_path = Path(tmp_file.name)
                tmp_file.write(serialized)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(temp_path, self._state_path)
            temp_path = None
            return True
        except OSError as exc:
            logger.error("Failed to save state to %s: %s", self._state_path, exc)
            return False
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _backup_corrupt_file(self) -> None:
        backup_path = self._state_path.with_name(f"{self._state_path.name}.corrupt")
        try:
            shutil.copy2(self._state_path, backup_path)
        except OSError:
            pass
