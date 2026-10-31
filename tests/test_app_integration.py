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
            stats=PetStats(cpu_usage=35.0, ram_usage=72.0, disk_usage=64.0, completed_sessions=8),
            state=PetState.IDLE,
            position=(150, 180),
            always_on_top=False,
            auto_wander=False,
            active_tool="pomodoro",
            tools_data={"pomodoro": {"completed_sessions": 8, "remaining_seconds": 1200}},
        )

        controller = ByteSpriteApp(
            app=qapp,
            storage=storage,
            enable_tray=False,
        )
        try:
            assert controller.auto_save_timer.interval() == AUTO_SAVE_INTERVAL_MS == 60_000
            assert controller.state_machine.active_tool_id == "pomodoro"
            assert controller.state_machine.stats.completed_sessions == 8
            assert controller.state_machine.stats.level == 3
            assert controller.window.always_on_top is False
            assert controller.window.auto_wander is False

            controller.window.trigger_play()
            assert controller.state_machine.state == PetState.PLAYING
        finally:
            controller.shutdown()

        # Verify save-on-shutdown wrote updated tool state atomically
        reloaded = storage.load()
        assert reloaded.active_tool == "pomodoro"
        assert reloaded.state == PetState.PLAYING
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
        assert saved.active_tool == "pomodoro"

    def test_song_switches_to_music_clip_and_focus_done_cheers(
        self, qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.engine.pet_state import PetStateMachine

        # Pin IDLE: live CPU above 90% would otherwise put the pet in ALERT
        monkeypatch.setattr(PetStateMachine, "state", property(lambda _self: PetState.IDLE))
        controller = ByteSpriteApp(app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False)
        window, sm = controller.window, controller.state_machine
        lyrics = sm.registry.get_plugin("lyrics")
        lyrics._enabled, lyrics._playing = True, True  # skip the PowerShell reader
        window._handle_stats_change(sm.stats)
        assert window._anim.base_state == "music"

        lyrics._playing = False
        window._handle_stats_change(sm.stats)
        assert window._anim.base_state == "idle"

        sm.registry.get_plugin("pomodoro")._notice = ("FOCUS DONE", "#73EFF7")
        window._handle_stats_change(sm.stats)
        assert window._anim.active_clip_name == "cheer"
        lyrics._enabled = False
        controller.quit()

    def test_hud_only_opens_for_user_actions(self, qapp: QApplication, tmp_path: Path) -> None:
        from PyQt6.QtGui import QColor

        controller = ByteSpriteApp(app=qapp, storage=StorageManager(tmp_path / "h.json"), enable_tray=False)
        window = controller.window
        window._hovered = False
        # RAM crossing the 90% alert line flips state by itself; that must not pop the card
        window._handle_state_change(PetState.IDLE, PetState.HUNGRY, controller.state_machine.stats)
        window._show_toast("FOCUS DONE", QColor("#73EFF7"))
        assert not window._is_hud_visible()
        window.trigger_pet()  # a click cycles the view, which the card shows
        assert window._is_hud_visible()
        controller.quit()

    def test_requirements_txt_is_valid_utf8_without_utf16_bom(self) -> None:
        req_path = Path(__file__).resolve().parents[1] / "requirements.txt"
        raw = req_path.read_bytes()
        assert not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"))
        decoded = raw.decode("utf-8")
        assert "PyQt6" in decoded

    def test_venv_pythonw_uses_windows_gui_subsystem_without_console(self) -> None:
        import struct

        pythonw = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "pythonw.exe"
        if pythonw.exists():
            raw = pythonw.read_bytes()
            pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
            subsystem = struct.unpack_from("<H", raw, pe_offset + 0x5C)[0]
            assert subsystem == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
            # Ensure it uses CPython's venvwlauncher (which resolves pyvenv.cfg) rather than
            # uv's console trampoline or a bare pythonw.exe that directly imports python312.dll
            assert b"python.exe\x02PAD" not in raw
            assert b"python312.dll" not in raw


def test_wheel_slices_hide_persist_and_keep_one(qapp: QApplication, tmp_path: Path) -> None:
    storage = StorageManager(tmp_path / "wheel.json")
    controller = ByteSpriteApp(app=qapp, storage=storage, enable_tray=False)
    win = controller.window
    all_ids = [o.callback_id for o in win.all_tool_wheel_options()]
    win.trigger_tool_action("wheel:toggle:snip:text")
    assert "snip:text" not in [o.callback_id for o in win.tool_wheel_options()]
    menu = win.build_context_menu()
    assert [a.isChecked() for a in menu.wheel_menu.actions()][0] is False
    menu.deleteLater()
    for cid in all_ids[1:]:
        win.trigger_tool_action(f"wheel:toggle:{cid}")
    assert len(win.tool_wheel_options()) == 1 and win._toast_text == "KEEP ONE"
    controller.quit()

    saved_hidden = storage.load().tools_data["wheel"]["hidden"]
    assert set(saved_hidden) == set(all_ids[:-1])
    again = ByteSpriteApp(app=qapp, storage=storage, enable_tray=False)
    assert [o.callback_id for o in again.window.tool_wheel_options()] == all_ids[-1:]
    again.quit()


def test_tool_wheel_closes_when_release_never_arrives(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    import sys

    from PyQt6.QtCore import QPoint

    from src.ui.tool_wheel import ToolWheel, ToolWheelOption

    if sys.platform != "win32":
        pytest.skip("Win32 button watchdog")
    wheel = ToolWheel(on_selected=lambda _o: None, always_on_top=False)
    wheel.open_tools([ToolWheelOption("A", "a", "#FFFFFF", "x")], window_top_left=QPoint(0, 0))
    monkeypatch.setattr(ctypes.windll.user32, "GetAsyncKeyState", lambda _vk: 0x8000)
    wheel._on_watchdog_tick()
    wheel._on_watchdog_tick()
    assert wheel.isVisible()  # button still held
    monkeypatch.setattr(ctypes.windll.user32, "GetAsyncKeyState", lambda _vk: 0)
    wheel._on_watchdog_tick()
    assert wheel.isVisible()  # one tick of grace for the normal release event
    wheel._on_watchdog_tick()
    assert not wheel.isVisible()
    wheel.deleteLater()
