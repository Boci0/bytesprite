"""Main application entrypoint assembling simulation, UI, system tray, and persistence."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.storage.storage_manager import StorageManager
from src.ui.pet_window import PetWindow, build_app_icon

SIMULATION_TICK_INTERVAL_MS: int = 5_000
AUTO_SAVE_INTERVAL_MS: int = 60_000


class ByteSpriteApp:
    """Application controller coordinating engine, floating window, tray, and storage."""

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
        self._storage = storage or StorageManager()
        self._is_shut_down = False

        saved = self._storage.load()
        self.state_machine = PetStateMachine(
            stats=saved.stats,
            initial_state=saved.state,
        )
        self.animation_controller = AnimationController(size=32)
        self.window = PetWindow(
            state_machine=self.state_machine,
            animation_controller=self.animation_controller,
            always_on_top=saved.always_on_top,
            auto_wander=saved.auto_wander,
            on_quit_requested=self.quit,
        )

        if saved.position is not None:
            self.window.move_clamped(saved.position[0], saved.position[1])
        else:
            self.window.reset_to_default_position()

        # Simulation tick timer
        self.sim_timer = QTimer(self.window)
        self.sim_timer.setInterval(max(100, sim_tick_ms))
        self.sim_timer.timeout.connect(self._on_simulation_tick)
        self.sim_timer.start()

        # Periodic 60-second auto-save timer
        self.auto_save_timer = QTimer(self.window)
        self.auto_save_timer.setInterval(max(1_000, auto_save_ms))
        self.auto_save_timer.timeout.connect(self.save_state)
        self.auto_save_timer.start()

        # Optional Windows system tray icon for quick recovery and controls
        self.tray_icon: QSystemTrayIcon | None = None
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(build_app_icon(64), self.window)
            tray.setToolTip("ByteSprite Desktop Companion")
            tray.activated.connect(self._on_tray_activated)
            tray.show()
            self.tray_icon = tray

        self._app.aboutToQuit.connect(self.shutdown)
        self._app.commitDataRequest.connect(lambda _mgr: self.save_state())

    def show(self) -> None:
        self.window.show()
        self.window.raise_()

    def _on_simulation_tick(self) -> None:
        self.state_machine.tick(1)

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
            self.window._show_toast("HELLO", QColor("#73EFF7"))

    def save_state(self) -> bool:
        pos: QPoint = self.window.pos()
        return self._storage.save(
            stats=self.state_machine.stats,
            state=self.state_machine.state,
            position=(pos.x(), pos.y()),
            always_on_top=self.window.always_on_top,
            auto_wander=self.window.auto_wander,
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

        self.sim_timer.stop()
        self.auto_save_timer.stop()
        self.save_state()
        self.window.shutdown()

        if self.tray_icon is not None:
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
            self.tray_icon = None

    def quit(self) -> None:
        self.shutdown()
        self.window.close()
        self._app.quit()


def main(argv: list[str] | None = None) -> int:
    """Launch ByteSprite on the Windows desktop."""
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
