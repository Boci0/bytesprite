"""Unit tests for multi-monitor geometry clamping, PetWindow interactions, and ContextMenu."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.ui.context_menu import PetContextMenu
from src.ui.pet_window import PetWindow, ScreenRect, clamp_position_to_screens


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class TestScreenGeometryClamping:
    def test_clamps_within_single_monitor_bounds(self) -> None:
        screens = [ScreenRect(0, 0, 1920, 1040)]
        assert clamp_position_to_screens(-100, -50, 168, 204, screens) == (0, 0)
        assert clamp_position_to_screens(2500, 1400, 168, 204, screens) == (
            1920 - 168,
            1040 - 204,
        )
        assert clamp_position_to_screens(400, 300, 168, 204, screens) == (400, 300)

    def test_supports_negative_coordinate_secondary_monitor(self) -> None:
        screens = [
            ScreenRect(-1920, 0, 1920, 1080),  # Left monitor
            ScreenRect(0, 0, 1920, 1040),      # Primary right monitor
        ]
        # Valid position on left monitor stays on left monitor
        assert clamp_position_to_screens(-1200, 250, 168, 204, screens) == (-1200, 250)
        # Dragged past far-left edge clamps to left monitor's left edge (-1920)
        assert clamp_position_to_screens(-2400, 250, 168, 204, screens) == (-1920, 250)
        # Dragged past far-right edge clamps to right monitor's right edge
        assert clamp_position_to_screens(2400, 250, 168, 204, screens) == (1920 - 168, 250)

    def test_empty_screens_returns_input_unchanged(self) -> None:
        assert clamp_position_to_screens(123, 456, 168, 204, []) == (123, 456)


class TestPetWindowAndContextMenu:
    def test_window_flags_and_translucent_attributes(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, always_on_top=True)
        try:
            flags = win.windowFlags()
            assert bool(flags & Qt.WindowType.FramelessWindowHint)
            assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
            assert win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

            win.set_always_on_top(False)
            assert not bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        finally:
            win.shutdown()
            win.close()

    def test_mouse_drag_moves_window_within_clamped_bounds(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(
            state_machine=sm,
            animation_controller=anim,
            auto_wander=False,
        )
        try:
            win.move(200, 200)
            win.show()
            qapp.processEvents()

            # Click on center of the sprite area
            local_pt = win.sprite_center_point()
            global_start = win.mapToGlobal(local_pt)

            press_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(local_pt),
                QPointF(global_start),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mousePressEvent(press_event)

            global_target = global_start + QPoint(80, 60)
            move_event = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(local_pt),
                QPointF(global_target),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mouseMoveEvent(move_event)

            release_event = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(local_pt),
                QPointF(global_target),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mouseReleaseEvent(release_event)

            # Position moved from (200, 200) toward (280, 260) within screen bounds
            assert win.pos() != QPoint(200, 200)
        finally:
            win.shutdown()
            win.close()

    def test_click_without_drag_triggers_pet_interaction(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats(hunger=80.0, energy=80.0, happiness=50.0))
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        try:
            local_pt = win.sprite_center_point()
            global_pt = win.mapToGlobal(local_pt)

            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mousePressEvent(press)
            win.mouseReleaseEvent(release)

            assert sm.stats.happiness > 50.0
        finally:
            win.shutdown()
            win.close()

    def test_context_menu_actions_trigger_state_changes(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats(hunger=20.0, energy=80.0, happiness=60.0))
        anim = AnimationController(size=32)
        quit_called: list[bool] = []
        reset_called: list[bool] = []

        menu = PetContextMenu(
            state_machine=sm,
            always_on_top=True,
            auto_wander=True,
            on_feed=sm.feed,
            on_play=sm.play,
            on_toggle_sleep=lambda: sm.wake() if sm.state == PetState.SLEEPING else sm.rest(),
            on_reset_position=lambda: reset_called.append(True),
            on_toggle_on_top=lambda _val: None,
            on_toggle_wander=lambda _val: None,
            on_quit=lambda: quit_called.append(True),
        )

        action_texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert any("Feed" in t for t in action_texts)
        assert any("Play" in t for t in action_texts)
        assert any("Put to Sleep" in t for t in action_texts)
        assert any("Reset Position" in t for t in action_texts)
        assert any("Quit" in t for t in action_texts)

        menu.feed_action.trigger()
        assert sm.stats.hunger > 20.0
        assert sm.state == PetState.IDLE

        menu.sleep_action.trigger()
        assert sm.state == PetState.SLEEPING

        menu.reset_position_action.trigger()
        assert reset_called == [True]

        menu.quit_action.trigger()
        assert quit_called == [True]

    def test_paint_dispatch_renders_crisp_sprite_and_transparent_corners(
        self, qapp: QApplication
    ) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        try:
            canvas = QImage(win.width(), win.height(), QImage.Format.Format_ARGB32_Premultiplied)
            for _ in range(15):
                anim.advance(160)
                canvas.fill(0)
                win.render(canvas)

            # Top-left corner must remain fully transparent when HUD is hidden
            assert canvas.pixelColor(0, 0).alpha() == 0
            # Center of sprite region must have opaque pixel art rendered
            sprite_center = win.sprite_center_point()
            assert canvas.pixelColor(sprite_center.x(), sprite_center.y()).alpha() == 255
        finally:
            win.shutdown()
            assert not win.has_active_timers()
            win.close()
