"""Unit tests for StorageManager persistence, corruption recovery, and atomic writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.engine.models import PetState, PetStats
from src.storage.storage_manager import PetSaveData, StorageManager


class TestStorageManager:
    def test_default_path_points_to_home_bytesprite_state_json(self) -> None:
        manager = StorageManager()
        assert manager.state_path == Path.home() / ".bytesprite" / "state.json"

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        state_file = tmp_path / ".bytesprite" / "state.json"
        manager = StorageManager(state_file)
        loaded = manager.load()
        assert isinstance(loaded, PetSaveData)
        assert loaded.state == PetState.IDLE
        assert 0.0 <= loaded.stats.hunger <= 100.0
        assert loaded.position is None
        assert loaded.always_on_top is True

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "nested" / "state.json"
        manager = StorageManager(state_file)
        stats = PetStats(
            hunger=42.5,
            energy=67.25,
            happiness=91.0,
            experience=230,
            last_updated=1700000000.0,
        )
        manager.save(
            stats=stats,
            state=PetState.SLEEPING,
            position=(320, 480),
            always_on_top=False,
            auto_wander=False,
        )

        assert state_file.exists()
        loaded = manager.load()
        assert loaded.stats.hunger == 42.5
        assert loaded.stats.energy == 67.25
        assert loaded.stats.happiness == 91.0
        assert loaded.stats.experience == 230
        assert loaded.state == PetState.SLEEPING
        assert loaded.position == (320, 480)
        assert loaded.always_on_top is False
        assert loaded.auto_wander is False

    def test_corrupted_truncated_json_recovers_to_defaults(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text('{"stats": {"hunger": 55.0, "ener', encoding="utf-8")

        manager = StorageManager(state_file)
        loaded = manager.load()

        assert loaded.state == PetState.IDLE
        assert loaded.stats.hunger == 80.0
        assert (tmp_path / "state.json.corrupt").exists()

    @pytest.mark.parametrize(
        "payload",
        [
            "[]",
            "null",
            '"just a string"',
            json.dumps({"stats": "invalid", "state": "flying", "window": {"x": "bad"}}),
            json.dumps({"stats": {"hunger": -50, "energy": 500, "happiness": "oops"}}),
        ],
    )
    def test_malformed_schema_values_sanitized_safely(
        self, tmp_path: Path, payload: str
    ) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(payload, encoding="utf-8")

        manager = StorageManager(state_file)
        loaded = manager.load()
        assert isinstance(loaded.state, PetState)
        assert 0.0 <= loaded.stats.hunger <= 100.0
        assert 0.0 <= loaded.stats.energy <= 100.0
        assert 0.0 <= loaded.stats.happiness <= 100.0

    def test_atomic_write_leaves_existing_file_intact_on_replace_failure(
        self, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        manager = StorageManager(state_file)

        initial_stats = PetStats(hunger=77.0, energy=88.0, happiness=99.0, experience=50)
        manager.save(stats=initial_stats, state=PetState.IDLE)

        updated_stats = PetStats(hunger=12.0, energy=34.0, happiness=56.0, experience=999)
        with patch.object(os, "replace", side_effect=OSError("simulated disk error")):
            result = manager.save(stats=updated_stats, state=PetState.HUNGRY)
            assert result is False

        # Original state file must remain untouched and no temp files left behind
        loaded = manager.load()
        assert loaded.stats.hunger == 77.0
        assert loaded.stats.experience == 50
        leftover_temps = list(tmp_path.glob("*.tmp"))
        assert leftover_temps == []
