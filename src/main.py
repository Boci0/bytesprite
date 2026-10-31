"""Main application entrypoint assembling multi-tool plugins, UI, system tray, and persistence."""

from __future__ import annotations

import ctypes
import signal
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.storage.storage_manager import StorageManager
from src.ui.pet_window import PetWindow, build_app_icon
from src.ui.quick_tools import GlobalHotkeys
from src.updater import is_installed

# Stored beside plugin state in state.json "tools"; not a ToolPlugin, so it never joins the HUD cycle
RECENT_FILES_KEY = "recent"
WHEEL_KEY = "wheel"

SIMULATION_TICK_INTERVAL_MS: int = 2_000
AUTO_SAVE_INTERVAL_MS: int = 60_000


class ByteSpriteApp:
    """Application controller coordinating multi-tool engine, floating window, tray, and storage."""

    def __init__(
        self,
        *,
        app: QApplication,
        storage: StorageManager | None = None,
        sim_tick_ms: int = SIMULATION_TICK_INTERVAL_MS,
        auto_save_ms: int = AUTO_SAVE_INTERVAL_MS,
        enable_tray: bool = True,
    ) -> None:
        self._app = app
        # Pet/overlay windows are Qt.Tool and don't count as "last window", so closing any
        # dialog (file pickers) would quit the app; quitting goes via quit() only
        self._app.setQuitOnLastWindowClosed(False)
        self._storage = storage or StorageManager()
        self._is_shut_down = False

        saved = self._storage.load()
        self.state_machine = PetStateMachine(
            stats=saved.stats,
            initial_state=saved.state,
        )

        # Restore per-plugin state and active HUD tool
        for plugin in self.state_machine.registry.plugins:
            plugin_data = saved.tools_data.get(plugin.tool_id)
            if isinstance(plugin_data, dict):
                plugin.load_dict(plugin_data)
        self.state_machine.select_tool(saved.active_tool)

        clip_vault = self.state_machine.clip_vault_plugin
        if clip_vault is not None:
            clip_vault.set_on_change(self.state_machine.notify_plugin_activity)
            clip_vault.attach_qt_clipboard(self._app.clipboard())

        self.animation_controller = AnimationController(size=32)
        self.window = PetWindow(
            state_machine=self.state_machine,
            animation_controller=self.animation_controller,
            always_on_top=saved.always_on_top,
            auto_wander=saved.auto_wander,
            on_quit_requested=self.quit,
            on_notification=self._show_tray_message,
        )
        self.window.recent.load_dict(saved.tools_data.get(RECENT_FILES_KEY))
        hidden = saved.tools_data.get(WHEEL_KEY, {}).get("hidden")
        if isinstance(hidden, list):
            self.window.wheel_hidden = {h for h in hidden if isinstance(h, str)}

        if saved.position is not None:
            self.window.move_clamped(saved.position[0], saved.position[1])
        else:
            self.window.reset_to_default_position()

        # Utility tick timer (2s base; scales dynamically to 1s/250ms when tools are active)
        self._base_tick_ms = max(100, sim_tick_ms)
        self.sim_timer = QTimer(self.window)
        self.sim_timer.setInterval(self._desired_tick_interval_ms())
        self.sim_timer.timeout.connect(self._on_simulation_tick)
        self.sim_timer.start()
        self._unsub_sim_sync = self.state_machine.on_stats_change(
            lambda _s: self._sync_sim_timer_interval()
        )

        # Periodic 60-second auto-save timer
        self.auto_save_timer = QTimer(self.window)
        self.auto_save_timer.setInterval(max(1_000, auto_save_ms))
        self.auto_save_timer.timeout.connect(self.save_state)
        self.auto_save_timer.start()

        # Optional Windows system tray icon for quick recovery and controls
        self.tray_icon: QSystemTrayIcon | None = None
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(build_app_icon(64), self.window)
            tray.setToolTip("ByteSprite Desktop Multi-Tool")
            tray.activated.connect(self._on_tray_activated)
            tray.show()
            self.tray_icon = tray

        # System-wide snip hotkeys share the desktop-integration switch with the tray, so
        # headless tests never grab real key combos
        self.hotkeys: GlobalHotkeys | None = None
        if enable_tray:
            self.hotkeys = GlobalHotkeys()
            self.hotkeys.triggered.connect(self.window.trigger_snip)
            self.hotkeys.start()
            if is_installed():
                # One quiet check per launch; installing waits for Settings > Check for Updates
                QTimer.singleShot(20_000, lambda: self.window.check_for_updates(install=False))

        self._app.aboutToQuit.connect(self.shutdown)
        self._app.commitDataRequest.connect(lambda _mgr: self.save_state())

    def _show_tray_message(self, text: str) -> None:
        if self.tray_icon is not None:
            self.tray_icon.showMessage("ByteSprite", text, build_app_icon(64), 4_000)

    def _desired_tick_interval_ms(self) -> int:
        return self.state_machine.registry.min_active_tick_interval_ms(self._base_tick_ms)

    def _sync_sim_timer_interval(self) -> None:
        if self._is_shut_down:
            return
        desired = self._desired_tick_interval_ms()
        if self.sim_timer.interval() != desired:
            self.sim_timer.setInterval(desired)

    def show(self) -> None:
        self.window.show()
        self.window.raise_()

    def _on_simulation_tick(self) -> None:
        self.state_machine.tick(1)
        desired = self._desired_tick_interval_ms()
        if self.sim_timer.interval() != desired:
            self.sim_timer.setInterval(desired)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context:
            menu = self.window.build_context_menu()
            menu.exec(QCursor.pos())
        elif reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
            self.window._show_toast("READY", QColor("#73EFF7"))

    def save_state(self) -> bool:
        pos: QPoint = self.window.pos()
        tools_payload: dict[str, dict[str, Any]] = {}
        for plugin in self.state_machine.registry.plugins:
            tools_payload[plugin.tool_id] = plugin.to_dict()
        tools_payload[RECENT_FILES_KEY] = self.window.recent.to_dict()
        tools_payload[WHEEL_KEY] = {"hidden": sorted(self.window.wheel_hidden)}
        return self._storage.save(
            stats=self.state_machine.stats,
            state=self.state_machine.state,
            position=(pos.x(), pos.y()),
            always_on_top=self.window.always_on_top,
            auto_wander=self.window.auto_wander,
            active_tool=self.state_machine.active_tool_id,
            tools_data=tools_payload,
        )

    def has_active_timers(self) -> bool:
        return (
            self.sim_timer.isActive()
            or self.auto_save_timer.isActive()
            or self.window.has_active_timers()
        )

    def shutdown(self) -> None:
        """Idempotently stop timers, persist state, and release tray resources."""
        if self._is_shut_down:
            return
        self._is_shut_down = True

        if self._unsub_sim_sync is not None:
            self._unsub_sim_sync()
            self._unsub_sim_sync = None  # type: ignore[assignment]
        self.sim_timer.stop()
        self.auto_save_timer.stop()
        if self.hotkeys is not None:
            self.hotkeys.close()
            self.hotkeys = None
        self.save_state()
        self.window.shutdown()
        self.state_machine.shutdown()

        if self.tray_icon is not None:
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
            self.tray_icon = None

    def quit(self) -> None:
        self.shutdown()
        self.window.close()
        self._app.quit()


def _detach_console_if_present() -> None:
    """Ensure no console window remains attached if launched directly from Explorer."""
    if sys.platform == "win32" and "--console" not in sys.argv:
        try:
            # Detach from our own console buffer without hiding any parent shell window
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    """Launch ByteSprite on the Windows desktop."""
    if argv is None:
        _detach_console_if_present()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    args = argv if argv is not None else sys.argv
    app = QApplication(args)
    app.setApplicationName("ByteSprite")
    app.setWindowIcon(build_app_icon(64))

    storage_override: StorageManager | None = None
    if "--state-file" in args:
        idx = args.index("--state-file")
        if idx + 1 < len(args):
            storage_override = StorageManager(Path(args[idx + 1]))

    controller = ByteSpriteApp(app=app, storage=storage_override)

    # Allow graceful Ctrl+C exit when launched from a terminal
    signal.signal(signal.SIGINT, lambda *_sig: controller.quit())

    controller.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
