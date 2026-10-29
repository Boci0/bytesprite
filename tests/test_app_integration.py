"""Integration tests for ByteSpriteApp wiring, auto-save, and graceful shutdown."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetState, PetStats
from src.main import AUTO_SAVE_INTERVAL_MS, ByteSpriteApp
from src.storage.storage_manager import StorageManager


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class TestByteSpriteAppIntegration:
    def test_restores_saved_state_and_persists_on_shutdown(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        state_file = tmp_path / "state.json"
        storage = StorageManager(state_file)
        storage.save(
            stats=PetStats(hunger=35.0, energy=72.0, happiness=64.0, experience=120),
            state=PetState.IDLE,
            position=(150, 180),
            always_on_top=False,
            auto_wander=False,
        )

        controller = ByteSpriteApp(
            app=qapp,
            storage=storage,
            enable_tray=False,
        )
        try:
            assert controller.auto_save_timer.interval() == AUTO_SAVE_INTERVAL_MS == 60_000
            assert controller.state_machine.stats.hunger == 35.0
            assert controller.state_machine.stats.level == 2
            assert controller.window.always_on_top is False
            assert controller.window.auto_wander is False

            controller.window.trigger_feed()
            assert controller.state_machine.stats.hunger == 60.0
        finally:
            controller.shutdown()

        # Verify save-on-shutdown wrote updated stats atomically
        reloaded = storage.load()
        assert reloaded.stats.hunger == 60.0
        assert reloaded.stats.experience == 130
        assert not controller.has_active_timers()

    def test_window_close_event_triggers_full_app_shutdown_not_frozen_tray(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        storage = StorageManager(tmp_path / "close_state.json")
        controller = ByteSpriteApp(app=qapp, storage=storage, enable_tray=False)
        controller.window.trigger_pet()
        # Simulate user pressing Alt+F4 on PetWindow directly
        controller.window.close()

        assert not controller.has_active_timers()
        saved = storage.load()
        assert saved.stats.experience > 0

    def test_requirements_txt_is_valid_utf8_without_utf16_bom(self) -> None:
        req_path = Path(__file__).resolve().parents[1] / "requirements.txt"
        raw = req_path.read_bytes()
        assert not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"))
        decoded = raw.decode("utf-8")
        assert "PyQt6" in decoded
