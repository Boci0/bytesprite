"""Frameless translucent floating desktop window for ByteSprite."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QEnterEvent,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QWidget

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.graphics.sprite_matrix import FrameMatrix, get_sprite_frames
from src.ui.context_menu import PetContextMenu


@dataclass(frozen=True)
class ScreenRect:
    """Pure value representation of an available desktop screen rectangle."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains_point(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def distance_sq_to_point(self, px: int, py: int) -> int:
        dx = 0
        if px < self.x:
            dx = self.x - px
        elif px > self.right:
            dx = px - self.right

        dy = 0
        if py < self.y:
            dy = self.y - py
        elif py > self.bottom:
            dy = py - self.bottom
        return dx * dx + dy * dy


def clamp_position_to_screens(
    x: int,
    y: int,
    width: int,
    height: int,
    screens: Sequence[ScreenRect],
) -> tuple[int, int]:
    """Clamp window top-left (x, y) to the nearest containing or closest monitor."""
    if not screens:
        return (x, y)

    cx = x + width // 2
    cy = y + height // 2

    target: ScreenRect | None = None
    for scr in screens:
        if scr.contains_point(cx, cy):
            target = scr
            break

    if target is None:
        target = min(screens, key=lambda s: s.distance_sq_to_point(cx, cy))

    max_x = max(target.x, target.right - width)
    max_y = max(target.y, target.bottom - height)
    clamped_x = max(target.x, min(max_x, x))
    clamped_y = max(target.y, min(max_y, y))
    return (clamped_x, clamped_y)


def frame_matrix_to_qimage(frame: FrameMatrix) -> QImage:
    """Convert an RGBA matrix into an owned QImage without smoothing blur."""
    height = len(frame)
    width = len(frame[0]) if height > 0 else 0
    raw = bytes(ch for row in frame for pixel in row for ch in pixel)
    image = QImage(raw, width, height, width * 4, QImage.Format.Format_RGBA8888)
    return image.copy()


def build_app_icon(size: int = 64) -> QIcon:
    """Generate a crisp nearest-neighbor pixel icon from the embedded 16x16 idle sprite."""
    base_frame = get_sprite_frames("idle", size=16)[0]
    qimg = frame_matrix_to_qimage(base_frame).scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    return QIcon(QPixmap.fromImage(qimg))


class PetWindow(QWidget):
    """Borderless translucent desktop widget rendering ByteSprite and hover HUD."""

    WINDOW_WIDTH: int = 176
    WINDOW_HEIGHT: int = 204
    SPRITE_SCALE: int = 4  # 32x32 * 4 = 128x128 crisp integer scaling
    DRAG_THRESHOLD_PX: int = 5

    def __init__(
        self,
        *,
        state_machine: PetStateMachine,
        animation_controller: AnimationController | None = None,
        always_on_top: bool = True,
        auto_wander: bool = True,
        on_quit_requested: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state_machine = state_machine
        self._anim = animation_controller or AnimationController(size=32)
        self._always_on_top = always_on_top
        self._auto_wander = auto_wander
        self._on_quit_requested = on_quit_requested

        self._sprite_size_px = 128
        sprite_x = (self.WINDOW_WIDTH - self._sprite_size_px) // 2
        sprite_y = self.WINDOW_HEIGHT - self._sprite_size_px - 6
        self._sprite_rect = QRect(sprite_x, sprite_y, self._sprite_size_px, self._sprite_size_px)
        self._hud_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 60)

        # Cached QImages keyed by (clip_name, frame_index, facing_left)
        self._image_cache: dict[tuple[str, int, bool], QImage] = {}

        # Mouse drag and hover state
        self._press_global_pos: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._did_drag: bool = False
        self._hovered: bool = False
        self._hud_pinned_until_ticks: int = 0

        # Floating micro-toast feedback
        self._toast_text: str = ""
        self._toast_color: QColor = QColor("#73EFF7")
        self._toast_steps: int = 0
        self._toast_max_steps: int = 12

        # Gentle autonomous walk steps
        self._walk_dx: int = 0
        self._walk_steps_remaining: int = 0

        self._configure_window()
        self._sync_animation_to_pet_state(self._state_machine.state)

        self._unsub_state = self._state_machine.on_state_change(self._handle_state_change)
        self._unsub_stats = self._state_machine.on_stats_change(self._handle_stats_change)

        # Frame timer uses exact frame durations when idle and steps smoothly during toasts/walks
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_animation_tick)
        self._schedule_next_anim_tick()

        # Ambient behavior timer (occasional blinks and tiny hops while idle)
        self._ambient_timer = QTimer(self)
        self._ambient_timer.setInterval(4200)
        self._ambient_timer.timeout.connect(self._on_ambient_tick)
        self._ambient_timer.start()

        # HUD auto-hide countdown timer (250ms resolution only while HUD/toast active)
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(90)
        self._overlay_timer.timeout.connect(self._on_overlay_tick)

    def _configure_window(self) -> None:
        self.setWindowTitle("ByteSprite")
        self.setWindowIcon(build_app_icon(64))
        self.setFixedSize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        self.setPalette(palette)

        self._apply_window_flags(re_show=False)

    def _apply_window_flags(self, *, re_show: bool) -> None:
        was_visible = self.isVisible()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        if re_show and was_visible:
            self.show()

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    @property
    def auto_wander(self) -> bool:
        return self._auto_wander

    def set_always_on_top(self, enabled: bool) -> None:
        if self._always_on_top == bool(enabled):
            return
        self._always_on_top = bool(enabled)
        self._apply_window_flags(re_show=True)

    def set_auto_wander(self, enabled: bool) -> None:
        self._auto_wander = bool(enabled)
        if not self._auto_wander and self._walk_steps_remaining > 0:
            self._walk_steps_remaining = 0
            self._sync_animation_to_pet_state(self._state_machine.state)
            self.update()

    def sprite_center_point(self) -> QPoint:
        return self._sprite_rect.center()

    def available_screen_rects(self) -> list[ScreenRect]:
        screens = QGuiApplication.screens()
        result: list[ScreenRect] = []
        for scr in screens:
            geom = scr.availableGeometry()
            result.append(ScreenRect(geom.x(), geom.y(), geom.width(), geom.height()))
        return result

    def move_clamped(self, x: int, y: int) -> None:
        cx, cy = clamp_position_to_screens(
            x,
            y,
            self.width(),
            self.height(),
            self.available_screen_rects(),
        )
        self.move(cx, cy)

    def reset_to_default_position(self) -> None:
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            self.move(120, 120)
            return
        avail = primary.availableGeometry()
        target_x = avail.right() - self.width() - 48
        target_y = avail.bottom() - self.height() - 18
        self.move_clamped(target_x, target_y)
        self._show_toast("HOME", QColor("#73EFF7"))

    # ------------------------------------------------------------------
    # Simulation & Animation Synchronization
    # ------------------------------------------------------------------

    def _sync_animation_to_pet_state(self, state: PetState) -> None:
        mapping = {
            PetState.IDLE: "idle",
            PetState.SLEEPING: "sleep",
            PetState.PLAYING: "excited",
            PetState.HUNGRY: "hungry",
        }
        target_clip = mapping.get(state, "idle")
        self._anim.set_state(target_clip)

    def _handle_state_change(
        self, _old_state: PetState, new_state: PetState, _stats: PetStats
    ) -> None:
        self._walk_steps_remaining = 0
        self._sync_animation_to_pet_state(new_state)
        self._pin_hud(32)
        self._schedule_next_anim_tick()
        self.update()

    def _handle_stats_change(self, _stats: PetStats) -> None:
        if self._is_hud_visible():
            self.update()

    def _schedule_next_anim_tick(self) -> None:
        duration = max(60, self._anim.current_frame_duration_ms())
        self._anim_timer.start(duration)

    def _on_animation_tick(self) -> None:
        elapsed = self._anim_timer.interval()
        changed = self._anim.advance(elapsed)
        self._schedule_next_anim_tick()
        if changed:
            self.update()

    def _on_ambient_tick(self) -> None:
        if self._press_global_pos is not None:
            return
        if self._state_machine.state != PetState.IDLE:
            return

        roll = random.random()
        if roll < 0.48:
            # Subtle blink
            self._anim.play_oneshot("blink")
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.72 and self._auto_wander and not self._hovered:
            # Gentle short hop left or right
            direction = random.choice((-1, 1))
            self._walk_dx = direction * 4
            self._walk_steps_remaining = random.randint(6, 10)
            self._anim.facing_left = direction < 0
            self._anim.set_state("walk")
            self._schedule_next_anim_tick()
            if not self._overlay_timer.isActive():
                self._overlay_timer.start()
            self.update()

    def _on_overlay_tick(self) -> None:
        needs_repaint = False

        if self._hud_pinned_until_ticks > 0:
            self._hud_pinned_until_ticks -= 1
            if self._hud_pinned_until_ticks == 0:
                needs_repaint = True

        if self._toast_steps > 0:
            self._toast_steps -= 1
            needs_repaint = True

        if self._walk_steps_remaining > 0:
            self._walk_steps_remaining -= 1
            before = self.pos()
            self.move_clamped(before.x() + self._walk_dx, before.y())
            if self.pos() == before or self._walk_steps_remaining == 0:
                self._walk_steps_remaining = 0
                if self._state_machine.state == PetState.IDLE:
                    self._anim.set_state("idle")
                    self._schedule_next_anim_tick()
            needs_repaint = True

        if (
            self._hud_pinned_until_ticks <= 0
            and self._toast_steps <= 0
            and self._walk_steps_remaining <= 0
        ):
            self._overlay_timer.stop()

        if needs_repaint:
            self.update()

    def _pin_hud(self, steps: int = 35) -> None:
        self._hud_pinned_until_ticks = max(self._hud_pinned_until_ticks, steps)
        if not self._overlay_timer.isActive():
            self._overlay_timer.start()
        self.update()

    def _show_toast(self, text: str, color: QColor) -> None:
        self._toast_text = text
        self._toast_color = color
        self._toast_steps = self._toast_max_steps
        self._pin_hud(36)

    def _is_hud_visible(self) -> bool:
        return self._hovered or self._hud_pinned_until_ticks > 0

    # ------------------------------------------------------------------
    # Interactive Actions
    # ------------------------------------------------------------------

    def trigger_feed(self) -> None:
        if self._state_machine.feed():
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            self._show_toast("+FOOD", QColor("#38B764"))

    def trigger_play(self) -> None:
        if self._state_machine.play():
            self._anim.set_state("excited", force=True)
            self._schedule_next_anim_tick()
            self._show_toast("+PLAY", QColor("#FFCD75"))
        else:
            self._show_toast("TIRED", QColor("#8A9AF2"))

    def trigger_pet(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._show_toast("ZZZ...", QColor("#8A9AF2"))
            return
        if self._state_machine.pet():
            self._anim.play_oneshot("excited")
            self._schedule_next_anim_tick()
            self._show_toast("+HAPPY", QColor("#FF77A8"))

    def trigger_toggle_sleep(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._state_machine.wake()
            self._show_toast("AWAKE", QColor("#73EFF7"))
        else:
            self._state_machine.rest()
            self._show_toast("SLEEP", QColor("#8A9AF2"))

    def trigger_quit(self) -> None:
        if self._on_quit_requested is not None:
            self._on_quit_requested()
        else:
            self.shutdown()
            self.close()
            app = QApplication.instance()
            if app is not None:
                app.quit()

    # ------------------------------------------------------------------
    # Mouse & Context Menu Events
    # ------------------------------------------------------------------

    def _is_interactive_hit(self, local_pos: QPoint) -> bool:
        if self._is_hud_visible() and self._hud_rect.contains(local_pos):
            return True
        return self._sprite_rect.adjusted(-6, -6, 6, 6).contains(local_pos)

    def enterEvent(self, event: QEnterEvent | None) -> None:
        self._hovered = True
        self.update()
        if event is not None:
            super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = False
        self.update()
        if event is not None:
            super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        local_pos = event.position().toPoint()
        if not self._is_interactive_hit(local_pos):
            event.ignore()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            self._press_global_pos = global_pos
            self._drag_offset = global_pos - self.frameGeometry().topLeft()
            self._did_drag = False
            self._walk_steps_remaining = 0
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._open_context_menu(event.globalPosition().toPoint())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if (
            self._press_global_pos is not None
            and self._drag_offset is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            global_pos = event.globalPosition().toPoint()
            delta = global_pos - self._press_global_pos
            if delta.manhattanLength() >= self.DRAG_THRESHOLD_PX:
                self._did_drag = True
            if self._did_drag:
                if delta.x() != 0:
                    self._anim.facing_left = delta.x() < 0
                candidate = global_pos - self._drag_offset
                self.move_clamped(candidate.x(), candidate.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_global_pos is not None:
            was_drag = self._did_drag
            self._press_global_pos = None
            self._drag_offset = None
            self._did_drag = False
            if not was_drag:
                self.trigger_pet()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is None:
            return
        if not self._is_interactive_hit(event.pos()):
            event.ignore()
            return
        self._open_context_menu(event.globalPos())
        event.accept()

    def build_context_menu(self) -> PetContextMenu:
        return PetContextMenu(
            state_machine=self._state_machine,
            always_on_top=self._always_on_top,
            auto_wander=self._auto_wander,
            on_feed=self.trigger_feed,
            on_play=self.trigger_play,
            on_pet=self.trigger_pet,
            on_toggle_sleep=self.trigger_toggle_sleep,
            on_reset_position=self.reset_to_default_position,
            on_toggle_on_top=self.set_always_on_top,
            on_toggle_wander=self.set_auto_wander,
            on_quit=self.trigger_quit,
            parent=self,
        )

    def _open_context_menu(self, global_pos: QPoint) -> None:
        menu = self.build_context_menu()
        menu.exec(global_pos)

    # ------------------------------------------------------------------
    # Painting (Crisp Nearest-Neighbor Sprite + Pixel HUD)
    # ------------------------------------------------------------------

    def _current_qimage(self) -> QImage:
        key = (
            self._anim.active_clip_name,
            self._anim.frame_index,
            self._anim.facing_left,
        )
        cached = self._image_cache.get(key)
        if cached is None:
            cached = frame_matrix_to_qimage(self._anim.current_frame())
            self._image_cache[key] = cached
        return cached

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # Subtle hover ring beneath ByteSprite's feet
        if self._hovered:
            painter.fillRect(
                QRect(
                    self._sprite_rect.center().x() - 34,
                    self._sprite_rect.bottom() - 10,
                    68,
                    4,
                ),
                QColor(115, 239, 247, 95),
            )

        # Crisp nearest-neighbor 4x pixel-art character
        painter.drawImage(self._sprite_rect, self._current_qimage())

        if self._is_hud_visible():
            self._paint_hud(painter)

        if self._toast_steps > 0 and self._toast_text:
            self._paint_toast(painter)

        painter.end()

    def _paint_hud(self, painter: QPainter) -> None:
        r = self._hud_rect
        # Stepped retro pixel panel frame
        painter.fillRect(r.adjusted(2, 0, -2, 0), QColor(24, 20, 37, 242))
        painter.fillRect(r.adjusted(0, 2, 0, -2), QColor(24, 20, 37, 242))
        painter.fillRect(r.adjusted(2, 2, -2, -2), QColor(34, 40, 66, 238))

        # Inner 1px pixel highlight border
        painter.setPen(QPen(QColor(82, 108, 152, 190), 1))
        painter.drawRect(r.adjusted(3, 3, -4, -4))

        stats = self._state_machine.stats
        state = self._state_machine.state

        title_font = QFont("Consolas", 8, QFont.Weight.Bold)
        title_font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(title_font)

        # Level badge + state pill
        painter.setPen(QColor("#73EFF7"))
        painter.drawText(
            QRect(r.left() + 8, r.top() + 5, 76, 13),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"LV.{stats.level:02d}",
        )

        state_colors = {
            PetState.IDLE: QColor("#38B764"),
            PetState.PLAYING: QColor("#FFCD75"),
            PetState.HUNGRY: QColor("#EF7D57"),
            PetState.SLEEPING: QColor("#8A9AF2"),
        }
        painter.setPen(state_colors.get(state, QColor("#F0F7F4")))
        painter.drawText(
            QRect(r.left() + 68, r.top() + 5, r.width() - 76, 13),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            state.value.upper(),
        )

        # Three segmented retro stat bars
        food_color = (
            QColor("#38B764")
            if stats.hunger > 50.0
            else (QColor("#FFCD75") if stats.hunger > 25.0 else QColor("#EF7D57"))
        )
        bars = [
            ("FD", stats.hunger, food_color),
            ("EN", stats.energy, QColor("#41A6F6")),
            ("HP", stats.happiness, QColor("#FF77A8")),
        ]
        bar_font = QFont("Consolas", 7, QFont.Weight.Bold)
        painter.setFont(bar_font)

        for idx, (label, val, color) in enumerate(bars):
            row_y = r.top() + 21 + idx * 12
            painter.setPen(QColor("#C2D3CD"))
            painter.drawText(
                QRect(r.left() + 8, row_y - 1, 20, 10),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

            track = QRect(r.left() + 28, row_y + 1, r.width() - 62, 7)
            painter.fillRect(track, QColor(16, 18, 30, 255))

            fill_w = int(round((max(0.0, min(100.0, val)) / 100.0) * (track.width() - 2)))
            if fill_w > 0:
                fill_rect = QRect(track.left() + 1, track.top() + 1, fill_w, track.height() - 2)
                painter.fillRect(fill_rect, color)
                # Specular 1px top highlight on bar
                painter.fillRect(
                    QRect(fill_rect.left(), fill_rect.top(), fill_rect.width(), 1),
                    QColor(255, 255, 255, 90),
                )

            painter.setPen(QColor("#F0F7F4"))
            painter.drawText(
                QRect(track.right() + 4, row_y - 1, 26, 10),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{int(round(val)):3d}",
            )

    def _paint_toast(self, painter: QPainter) -> None:
        progress = 1.0 - (self._toast_steps / max(1, self._toast_max_steps))
        rise_px = int(progress * 14)
        alpha = max(60, min(255, int(255 * (self._toast_steps / max(1, self._toast_max_steps)) * 1.35)))

        pill_w = 64
        pill_h = 18
        pill_x = (self.width() - pill_w) // 2
        pill_y = max(2, self._sprite_rect.top() - 10 - rise_px)
        pill = QRect(pill_x, pill_y, pill_w, pill_h)

        bg = QColor(24, 20, 37, min(235, alpha))
        painter.fillRect(pill.adjusted(1, 0, -1, 0), bg)
        painter.fillRect(pill.adjusted(0, 1, 0, -1), bg)

        fg = QColor(self._toast_color)
        fg.setAlpha(alpha)
        painter.setPen(fg)
        toast_font = QFont("Consolas", 8, QFont.Weight.Bold)
        painter.setFont(toast_font)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self._toast_text)

    # ------------------------------------------------------------------
    # Lifecycle & Cleanup
    # ------------------------------------------------------------------

    def has_active_timers(self) -> bool:
        return (
            self._anim_timer.isActive()
            or self._ambient_timer.isActive()
            or self._overlay_timer.isActive()
        )

    def shutdown(self) -> None:
        """Stop all internal timers and detach state listeners cleanly."""
        self._anim_timer.stop()
        self._ambient_timer.stop()
        self._overlay_timer.stop()
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None  # type: ignore[assignment]
        if self._unsub_stats is not None:
            self._unsub_stats()
            self._unsub_stats = None  # type: ignore[assignment]

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.shutdown()
        if event is not None:
            super().closeEvent(event)
