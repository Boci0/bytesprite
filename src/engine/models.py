"""Core data models, HUD specifications, and utility state enumerations for ByteSprite."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompanionState(str, Enum):
    """Discrete utility and operational states for ByteSprite."""

    IDLE = "idle"
    SLEEPING = "sleeping"
    PLAYING = "playing"
    HUNGRY = "hungry"

    # Multi-tool operational aliases mapped to the core sprite clip states
    FOCUS = "playing"
    ALERT = "hungry"
    DND = "sleeping"


# Backward-compatible alias
PetState = CompanionState


@dataclass(frozen=True)
class HudBar:
    """Single compact progress bar specification for the pixel HUD."""

    label: str
    value: float
    color_hex: str = "#41A6F6"
    display_text: str | None = None

    @property
    def clamped_value(self) -> float:
        if math.isnan(self.value):
            return 0.0
        if math.isinf(self.value):
            return 100.0 if self.value > 0 else 0.0
        return max(0.0, min(100.0, float(self.value)))

    @property
    def formatted_value(self) -> str:
        if self.display_text is not None:
            return self.display_text
        return f"{int(round(self.clamped_value)):3d}%"


@dataclass(frozen=True)
class HudData:
    """Declarative HUD card payload produced by the active tool plugin."""

    badge_left: str
    badge_right: str
    badge_right_color: str = "#38B764"
    bars: tuple[HudBar, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ToolMenuItem:
    """Declarative context menu action exposed by a registered tool plugin."""

    label: str
    callback_id: str
    enabled: bool = True
    checkable: bool = False
    checked: bool = False
    submenu: str | None = None


def _clamp_stat(value: Any, default: float = 0.0) -> float:
    """Coerce and clamp a percentage metric value to [0.0, 100.0]."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(numeric):
        return default
    if math.isinf(numeric):
        return 100.0 if numeric > 0 else 0.0
    return max(0.0, min(100.0, numeric))


def _sanitize_int(value: Any, default: int = 0) -> int:
    """Coerce an integer counter to a non-negative int."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0, int(numeric))


def _sanitize_timestamp(value: Any) -> float:
    """Coerce a timestamp to a finite positive float."""
    if isinstance(value, bool):
        return time.time()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return time.time()
    if not math.isfinite(numeric) or numeric <= 0.0:
        return time.time()
    return numeric


@dataclass
class PetStats:
    """Mutable utility and telemetry snapshot for ByteSprite."""

    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    disk_usage: float = 0.0
    completed_sessions: int = 0
    last_updated: float = field(default_factory=time.time)

    def __init__(
        self,
        cpu_usage: float | None = None,
        ram_usage: float | None = None,
        disk_usage: float | None = None,
        completed_sessions: int | None = None,
        last_updated: float | None = None,
        *,
        hunger: float | None = None,
        energy: float | None = None,
        happiness: float | None = None,
        experience: int | None = None,
    ) -> None:
        raw_cpu = cpu_usage if cpu_usage is not None else (hunger if hunger is not None else 0.0)
        raw_ram = ram_usage if ram_usage is not None else (energy if energy is not None else 0.0)
        raw_disk = (
            disk_usage if disk_usage is not None else (happiness if happiness is not None else 0.0)
        )
        raw_sessions = (
            completed_sessions
            if completed_sessions is not None
            else (experience if experience is not None else 0)
        )
        self.cpu_usage = _clamp_stat(raw_cpu, default=0.0)
        self.ram_usage = _clamp_stat(raw_ram, default=0.0)
        self.disk_usage = _clamp_stat(raw_disk, default=0.0)
        self.completed_sessions = _sanitize_int(raw_sessions, default=0)
        self.last_updated = _sanitize_timestamp(
            last_updated if last_updated is not None else time.time()
        )

    def clamp(self) -> None:
        """Enforce [0.0, 100.0] bounds on system metrics and non-negative counters."""
        self.cpu_usage = _clamp_stat(self.cpu_usage, default=0.0)
        self.ram_usage = _clamp_stat(self.ram_usage, default=0.0)
        self.disk_usage = _clamp_stat(self.disk_usage, default=0.0)
        self.completed_sessions = _sanitize_int(self.completed_sessions, default=0)
        self.last_updated = _sanitize_timestamp(self.last_updated)

    # Legacy property aliases so callers using either naming convention work seamlessly
    @property
    def hunger(self) -> float:
        return self.cpu_usage

    @hunger.setter
    def hunger(self, value: float) -> None:
        self.cpu_usage = _clamp_stat(value, default=0.0)

    @property
    def energy(self) -> float:
        return self.ram_usage

    @energy.setter
    def energy(self, value: float) -> None:
        self.ram_usage = _clamp_stat(value, default=0.0)

    @property
    def happiness(self) -> float:
        return self.disk_usage

    @happiness.setter
    def happiness(self, value: float) -> None:
        self.disk_usage = _clamp_stat(value, default=0.0)

    @property
    def experience(self) -> int:
        return self.completed_sessions

    @experience.setter
    def experience(self, value: int) -> None:
        self.completed_sessions = _sanitize_int(value, default=0)

    @property
    def level(self) -> int:
        """Derived productivity rank starting at 1."""
        return 1 + (self.completed_sessions // 4)

    def to_dict(self) -> dict[str, float | int]:
        """Serialize telemetry and session stats to a JSON-compatible dictionary."""
        self.clamp()
        return {
            "cpu_usage": round(self.cpu_usage, 2),
            "ram_usage": round(self.ram_usage, 2),
            "disk_usage": round(self.disk_usage, 2),
            "completed_sessions": self.completed_sessions,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PetStats:
        """Reconstruct validated PetStats from a v1 or v2 dictionary."""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            cpu_usage=_clamp_stat(raw.get("cpu_usage", raw.get("hunger", 0.0)), default=0.0),
            ram_usage=_clamp_stat(raw.get("ram_usage", raw.get("energy", 0.0)), default=0.0),
            disk_usage=_clamp_stat(raw.get("disk_usage", raw.get("happiness", 0.0)), default=0.0),
            completed_sessions=_sanitize_int(
                raw.get("completed_sessions", raw.get("experience", 0)), default=0
            ),
            last_updated=_sanitize_timestamp(raw.get("last_updated", time.time())),
        )
