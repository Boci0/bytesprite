"""Frameless translucent floating desktop window for ByteSprite."""

from __future__ import annotations

import ctypes
import os
import random
import struct
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QDateTime, QPoint, QRect, QRectF, QStandardPaths, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
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
from PyQt6.QtWidgets import QApplication, QFileDialog, QMenu, QWidget

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.graphics.sprite_matrix import FrameMatrix, get_sprite_frames
from src.tools.file_forge import ForgeMode, ForgeWheelOption, get_wheel_options_for_paths
from src.storage.recent_files import RecentFiles
from src.tools.web_drop import WebImageJob, is_web_image_source, web_image_stem
from src.ui.context_menu import PetContextMenu, style_menu
from src.ui.drop_wheel import RadialDropWheel
from src.ui.quick_tools import OcrJob, SnipSession
from src.ui.sfx import preload_tick_sounds
from src.ui.tool_wheel import WEB_SHORTCUTS, ToolWheel, ToolWheelOption, google_search_url
from src.ui.theme import GLASS_RGBA, HAIRLINE_RGBA, HOVER_RGBA, TEXT_DIM_HEX, TEXT_HEX, qcolor, ui_font


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
    """Clamp window top-left (x, y) across adjacent monitors or to the nearest screen."""
    if not screens:
        return (x, y)

    right_edge = x + width - 1
    bottom_edge = y + height - 1

    # Allow smooth straddling across horizontally adjacent monitors
    for left_scr in screens:
        if left_scr.x <= x < left_scr.right and right_edge >= left_scr.right:
            for right_scr in screens:
                if (
                    right_scr is not left_scr
                    and left_scr.right == right_scr.x
                    and right_scr.x <= right_edge < right_scr.right
                ):
                    shared_top = max(left_scr.y, right_scr.y)
                    shared_bot = min(left_scr.bottom, right_scr.bottom)
                    if shared_bot - shared_top >= height:
                        return (x, max(shared_top, min(shared_bot - height, y)))

    # Allow smooth straddling across vertically adjacent monitors
    for top_scr in screens:
        if top_scr.y <= y < top_scr.bottom and bottom_edge >= top_scr.bottom:
            for bot_scr in screens:
                if (
                    bot_scr is not top_scr
                    and top_scr.bottom == bot_scr.y
                    and bot_scr.y <= bottom_edge < bot_scr.bottom
                ):
                    shared_left = max(top_scr.x, bot_scr.x)
                    shared_right = min(top_scr.right, bot_scr.right)
                    if shared_right - shared_left >= width:
                        return (max(shared_left, min(shared_right - width, x)), y)

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


def export_windows_ico(
    output_path: Path | str,
    sizes: Sequence[int] = (16, 32, 48, 64, 128, 256),
) -> Path:
    """Write a multi-resolution Windows .ico file with PNG-encoded nearest-neighbor frames."""
    from PyQt6.QtCore import QBuffer, QIODevice

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base_frame = get_sprite_frames("idle", size=32)[0]
    base_img = frame_matrix_to_qimage(base_frame)

    png_blobs: list[tuple[int, bytes]] = []
    for sz in sizes:
        scaled = base_img.scaled(
            sz,
            sz,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "PNG")
        png_blobs.append((sz, bytes(buf.data())))

    count = len(png_blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + (16 * count)
    entries = bytearray()
    payload = bytearray()

    for sz, blob in png_blobs:
        dim_byte = 0 if sz >= 256 else sz
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                dim_byte,
                dim_byte,
                0,
                0,
                1,
                32,
                len(blob),
                offset,
            )
        )
        payload.extend(blob)
        offset += len(blob)

    dest.write_bytes(header + bytes(entries) + bytes(payload))
    return dest


VK_VOLUME_MUTE = 0xAD
KEYEVENTF_KEYUP = 0x0002


class PetWindow(QWidget):
    """Borderless translucent desktop widget rendering ByteSprite and hover HUD."""

    WINDOW_WIDTH: int = 176
    WINDOW_HEIGHT: int = 222
    SPRITE_SCALE: int = 4  # 32x32 * 4 = 128x128 crisp integer scaling
    DRAG_THRESHOLD_PX: int = 5
    HOLD_TO_WHEEL_MS: int = 450

    def __init__(
        self,
        *,
        state_machine: PetStateMachine,
        animation_controller: AnimationController | None = None,
        always_on_top: bool = True,
        auto_wander: bool = True,
        on_quit_requested: Callable[[], None] | None = None,
        on_notification: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state_machine = state_machine
        self._anim = animation_controller or AnimationController(size=32)
        self._always_on_top = always_on_top
        self._auto_wander = auto_wander
        self._on_quit_requested = on_quit_requested
        self._on_notification = on_notification

        self._sprite_size_px = 128
        sprite_x = (self.WINDOW_WIDTH - self._sprite_size_px) // 2
        sprite_y = self.WINDOW_HEIGHT - self._sprite_size_px - 6
        self._sprite_rect = QRect(sprite_x, sprite_y, self._sprite_size_px, self._sprite_size_px)
        self._hud_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 72)

        # Cached QImages keyed by (clip_name, frame_index, facing_left)
        self._image_cache: dict[tuple[str, int, bool], QImage] = {}

        # Mouse drag and hover state
        self._press_global_pos: QPoint | None = None
        self._last_drag_global_pos: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._did_drag: bool = False
        self._hovered: bool = False
        self._hud_pinned_until_ticks: int = 0

        # Floating micro-toast feedback
        self._toast_text: str = ""
        self._toast_color: QColor = QColor("#73EFF7")
        self._toast_steps: int = 0
        self._toast_max_steps: int = 12
        # Synced lyric shown in a speech bubble above the sprite (Lyrics tool)
        self._lyric_line: str = ""

        # Gentle autonomous walk steps
        self._walk_dx: int = 0
        self._walk_steps_remaining: int = 0
        self._is_shutting_down: bool = False
        self._drop_wheel: RadialDropWheel | None = None
        self._snip_session: SnipSession | None = None
        # Single click waits out the double-click interval so a double-click snips without also cycling tools
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.styleHints().mouseDoubleClickInterval())
        self._click_timer.timeout.connect(self.trigger_pet)
        # Holding still on the pet opens the tool wheel; any drag past the threshold cancels it
        self._tool_wheel: ToolWheel | None = None
        self._wheel_holding: bool = False
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self.HOLD_TO_WHEEL_MS)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self._ocr_jobs: list[OcrJob] = []
        # Image dragged out of a browser (URL or raw image), fetched only once a slice is chosen
        self._pending_web_source: str | QImage | None = None
        self._drag_paths: list[Path] = []  # cached at drag-enter; re-stat per move lags big drops
        self._web_jobs: list[WebImageJob] = []
        # Clean PNG from a web drop, deleted once File Forge converts it to the chosen format
        self._web_intermediate: Path | None = None
        self.recent = RecentFiles()
        QTimer.singleShot(0, preload_tick_sounds)  # wheels are built lazily; sounds must be ready first

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

        # Other topmost windows can push the pet down the z-order; Qt sets the flag only once
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(2000)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        self._topmost_timer.start()

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
        self.setAcceptDrops(True)

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
        self.setAcceptDrops(True)
        if re_show and was_visible:
            self.show()

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    @property
    def auto_wander(self) -> bool:
        return self._auto_wander

    def _open_wheels(self) -> list:
        return [w for w in (self._tool_wheel, self._drop_wheel) if w is not None and w.isVisible()]

    def _reassert_topmost(self) -> None:
        if sys.platform != "win32" or not self._always_on_top or not self.isVisible():
            return
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE: re-raise without stealing focus
        set_pos = ctypes.windll.user32.SetWindowPos
        set_pos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        wheels = self._open_wheels()
        if not wheels:
            set_pos(int(self.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        for wheel in wheels:
            wheel.restack_topmost()  # wheels stay above the pet but below an in-flight drag image

    def set_always_on_top(self, enabled: bool) -> None:
        if self._always_on_top == bool(enabled):
            return
        self._always_on_top = bool(enabled)
        self._apply_window_flags(re_show=True)
        if self._drop_wheel is not None:
            self._drop_wheel._always_on_top = self._always_on_top
            self._drop_wheel._configure_window()
        if self._tool_wheel is not None:
            self._tool_wheel._always_on_top = self._always_on_top
            self._tool_wheel._configure_window()

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
            PetState.PLAYING: "focus",
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
        self._collect_forge_outputs()
        lyric = self._state_machine.lyric_line
        if lyric != self._lyric_line:
            self._lyric_line = lyric
            self.update()
        notifications = self._state_machine.pop_notifications()
        if notifications:
            toast_text, color_hex = notifications[-1]
            if self._state_machine.state == PetState.IDLE:
                self._anim.play_oneshot("spin")
                self._schedule_next_anim_tick()
            self._show_toast(toast_text, QColor(color_hex or "#38B764"))
            if self._on_notification is not None:
                self._on_notification(toast_text)
            return
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
        if self._open_wheels():
            return  # a hop or blink must not move the pet out from under an open wheel
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
        if self._open_wheels():
            return False  # the HUD would cover wheel slices
        return self._hovered or self._hud_pinned_until_ticks > 0

    # ------------------------------------------------------------------
    # Interactive Multi-Tool Actions
    # ------------------------------------------------------------------

    def trigger_feed(self) -> None:
        if self._state_machine.refresh_metrics():
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            cpu = int(round(self._state_machine.stats.cpu_usage))
            self._show_toast(f"CPU {cpu}%", QColor("#38B764"))

    def trigger_play(self) -> None:
        running = self._state_machine.toggle_pomodoro()
        if running:
            self._anim.set_state("focus", force=True)
            self._schedule_next_anim_tick()
            self._show_toast("FOCUS!", QColor("#FFCD75"))
        else:
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()
            self._show_toast("PAUSED", QColor("#8A9AF2"))

    def trigger_pet(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._show_toast("DND...", QColor("#8A9AF2"))
            return
        plugin = self._state_machine.cycle_tool()
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        badge = plugin.tool_id.upper()[:6] if plugin is not None else "TOOL"
        self._show_toast(badge, QColor("#73EFF7"))

    def trigger_select_tool(self, tool_id: str) -> None:
        if self._state_machine.select_tool(tool_id):
            self._show_toast(tool_id.upper()[:6], QColor("#73EFF7"))

    def trigger_drop_files(
        self,
        paths: Sequence[Path | str],
        *,
        mode: ForgeMode | None = None,
    ) -> bool:
        """Feed local files or folders into FileForge with eating animation."""
        if self._state_machine.submit_files(paths, mode=mode):
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            self._show_toast("FORGING..", QColor("#FFCD75"))
            return True
        forge = self._state_machine.forge_plugin
        if forge is not None and forge.busy:
            self._show_toast("BUSY!", QColor("#EF7D57"))
        return False

    @property
    def drop_wheel(self) -> RadialDropWheel | None:
        return self._drop_wheel

    def _on_wheel_option_dropped(
        self,
        paths: list[Path],
        option: ForgeWheelOption,
    ) -> None:
        self._hovered = False
        self._dispatch_drop(paths, option.mode)

    def _on_wheel_cancelled(self) -> None:
        self._hovered = False
        self._pending_web_source = None
        self._show_toast("SKIPPED", QColor("#8A9AF2"))

    def _dispatch_drop(self, paths: Sequence[Path], mode: ForgeMode | None) -> None:
        source, self._pending_web_source = self._pending_web_source, None
        if source is not None:
            self._start_web_fetch(source, mode or ForgeMode.PNG)
        elif paths:
            self.trigger_drop_files(paths, mode=mode)

    @staticmethod
    def _web_drop_source(mime) -> str | QImage | None:
        """First http(s)/data: image URL in a browser drag, else its raw image data."""
        if mime is None:
            return None
        if mime.hasUrls():
            for url in mime.urls():
                text = url.toString()
                if not url.isLocalFile() and is_web_image_source(text):
                    return text
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                return image
        return None

    @staticmethod
    def _web_drop_dir() -> Path:
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        for out_dir in (Path(downloads) / "ByteSprite" if downloads else None, Path.home() / ".bytesprite" / "drops"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                return out_dir
            except OSError:
                continue
        return Path.home() / ".bytesprite" / "drops"

    def _start_web_fetch(self, source: str | QImage, mode: ForgeMode) -> None:
        job = WebImageJob()
        job.finished.connect(lambda path, err, j=job, m=mode: self._on_web_fetch_done(j, path, err, m))
        self._web_jobs.append(job)
        job.start(source, self._web_drop_dir())
        self._anim.play_oneshot("eating")
        self._schedule_next_anim_tick()
        self._show_toast("FETCHING", QColor("#FFCD75"))

    def _on_web_fetch_done(self, job: WebImageJob, path: object, error: str, mode: ForgeMode) -> None:
        if job in self._web_jobs:
            self._web_jobs.remove(job)
        job.deleteLater()
        if self._is_shutting_down:
            return
        if not isinstance(path, Path):
            toast = {"not an image": "NOT IMAGE", "too large": "TOO BIG"}.get(error, "WEB ERR")
            self._show_toast(toast, QColor("#EF7D57"))
            return
        if mode == ForgeMode.PNG:
            self.recent.add(path)
            self._show_toast("SAVED PNG", QColor("#38B764"))
            return
        if self.trigger_drop_files([path], mode=mode):
            self._web_intermediate = path
        else:
            self.recent.add(path)  # forge busy: keep the clean PNG as the result

    def _collect_forge_outputs(self) -> None:
        forge = self._state_machine.forge_plugin
        outputs = forge.pop_finished_outputs() if forge is not None else None
        if outputs is None:
            return
        for out in reversed(outputs):
            self.recent.add(out)
        intermediate, self._web_intermediate = self._web_intermediate, None
        if intermediate is None:
            return
        if outputs and intermediate not in outputs:
            try:
                intermediate.unlink(missing_ok=True)
            except OSError:
                self.recent.add(intermediate)
        else:
            self.recent.add(intermediate)  # conversion failed: the clean PNG is still useful

    def reveal_recent(self, index: int) -> None:
        entries = self.recent.entries()
        if not 0 <= index < len(entries):
            self._show_toast("GONE", QColor("#EF7D57"))
            return
        try:
            # Windows paths cannot contain quotes, so wrapping is safe
            subprocess.Popen(f'explorer /select,"{entries[index][0]}"')
        except OSError:
            self._show_toast("OPEN ERR", QColor("#EF7D57"))

    def open_output_folder(self, folder: Path) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError:
            self._show_toast("OPEN ERR", QColor("#EF7D57"))

    @staticmethod
    def snips_dir() -> Path:
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        return Path(pictures) / "ByteSprite" if pictures else Path.home() / ".bytesprite" / "snips"

    def _spawn_drop_wheel(
        self,
        paths: list[Path],
        local_cursor: QPoint,
        options: Sequence[ForgeWheelOption] | None = None,
    ) -> None:
        if self._drop_wheel is None:
            self._drop_wheel = RadialDropWheel(
                on_option_dropped=self._on_wheel_option_dropped,
                on_cancelled=self._on_wheel_cancelled,
                always_on_top=self._always_on_top,
            )
        if options is None:
            options = get_wheel_options_for_paths(paths)
        sprite_global = self.mapToGlobal(self.sprite_center_point())
        ideal_x = sprite_global.x() - int(RadialDropWheel.CENTER_X)
        ideal_y = sprite_global.y() - int(RadialDropWheel.CENTER_Y)
        wx, wy = clamp_position_to_screens(
            ideal_x,
            ideal_y,
            RadialDropWheel.WHEEL_WIDTH,
            RadialDropWheel.WHEEL_HEIGHT,
            self.available_screen_rects(),
        )
        self._drop_wheel.open_for_paths(
            paths,
            options,
            window_top_left=QPoint(wx, wy),
            initial_global_cursor=self.mapToGlobal(local_cursor),
        )

    @property
    def tool_wheel(self) -> ToolWheel | None:
        return self._tool_wheel

    def tool_wheel_options(self) -> list[ToolWheelOption]:
        """Slices clockwise from 12 o'clock: everyday quick tools, then web shortcuts."""
        pomo = self._state_machine.pomodoro_plugin
        focus_sub = "pause" if pomo is not None and pomo.running else f"{pomo.work_minutes if pomo else 25}m"
        options = [
            ToolWheelOption("OCR", "copy text", "#41A6F6", "snip:text"),
            ToolWheelOption("Clips", "history", "#73EFF7", "pet:clips"),
            ToolWheelOption("Search", "clipboard", "#38B764", "pet:search"),
            ToolWheelOption("Focus", focus_sub, "#FFCD75", "pomodoro:toggle"),
        ]
        if sys.platform == "win32":
            options.append(ToolWheelOption("Mute", "speakers", "#8A9AF2", "pet:mute"))
        options.extend(
            ToolWheelOption(label, "open", color, f"web:{url}") for label, url, color in WEB_SHORTCUTS
        )
        return options

    def open_tool_wheel(self, global_cursor: QPoint | None = None) -> ToolWheel:
        if self._tool_wheel is None:
            self._tool_wheel = ToolWheel(
                on_selected=self._on_tool_wheel_selected,
                always_on_top=self._always_on_top,
            )
        sprite_global = self.mapToGlobal(self.sprite_center_point())
        wx, wy = clamp_position_to_screens(
            sprite_global.x() - int(ToolWheel.CENTER_X),
            sprite_global.y() - int(ToolWheel.CENTER_Y),
            ToolWheel.WHEEL_WIDTH,
            ToolWheel.WHEEL_HEIGHT,
            self.available_screen_rects(),
        )
        self._tool_wheel.open_tools(
            self.tool_wheel_options(),
            window_top_left=QPoint(wx, wy),
            initial_global_cursor=global_cursor,
        )
        return self._tool_wheel

    def _on_hold_timeout(self) -> None:
        if self._press_global_pos is None or self._did_drag:
            return
        self._wheel_holding = True
        self._click_timer.stop()
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        self.open_tool_wheel(QCursor.pos())

    def _on_tool_wheel_selected(self, option: ToolWheelOption) -> None:
        if option.callback_id == "pomodoro:toggle":
            self.trigger_play()
        else:
            self.trigger_tool_action(option.callback_id)

    def _open_clip_history(self, global_pos: QPoint) -> None:
        vault = self._state_machine.registry.get_plugin("clipvault")
        entries = list(getattr(vault, "entries", ()))
        pins = list(getattr(vault, "pins", ()))
        if not entries and not pins:
            self._show_toast("NO CLIPS", QColor("#EF7D57"))
            return
        menu = style_menu(QMenu(self))
        for idx, entry in enumerate(entries):
            act = menu.addAction(entry.preview.replace("&", "&&"))
            act.triggered.connect(lambda _c=False, i=idx: self.trigger_tool_action(f"clipvault:restore:{i}"))
        if pins:
            menu.addSeparator()
            for idx, entry in enumerate(pins):
                act = menu.addAction(f"Pinned: {entry.preview}".replace("&", "&&"))
                act.triggered.connect(lambda _c=False, i=idx: self.trigger_tool_action(f"clipvault:pinned:{i}"))
        menu.exec(global_pos)
        menu.deleteLater()

    def _clipboard_local_paths(self) -> list[Path]:
        app = QApplication.instance()
        if app is None:
            return []
        cb = app.clipboard()
        if cb is None:
            return []
        mime = cb.mimeData()
        if mime is not None and mime.hasUrls():
            urls = [
                Path(u.toLocalFile())
                for u in mime.urls()
                if u.isLocalFile() and u.toLocalFile()
            ]
            valid = [p for p in urls if p.exists()]
            if valid:
                return valid
        raw_txt = (cb.text() or "").strip().strip('"')
        if raw_txt and len(raw_txt) <= 1024:
            try:
                candidate = Path(raw_txt)
                if candidate.exists():
                    return [candidate]
            except OSError:
                pass
        return []

    # ------------------------------------------------------------------
    # Screen Snip / OCR / Color Pick
    # ------------------------------------------------------------------

    def trigger_snip(self, mode: str) -> None:
        if self._snip_session is not None:
            return
        # Hide ByteSprite and let the context menu fade so neither lands in the frozen grab
        self.hide()
        QTimer.singleShot(180, lambda: self._begin_snip(mode))

    def _begin_snip(self, mode: str) -> None:
        if self._is_shutting_down:
            return
        self._snip_session = SnipSession(mode, self._on_snip_result)

    def _on_snip_result(self, mode: str, result: object | None) -> None:
        self._snip_session = None
        self.show()
        if result is None:
            self._show_toast("CANCEL", QColor("#8A9AF2"))
            return
        cb = QGuiApplication.clipboard()
        if mode == "color" and isinstance(result, str):
            if cb is not None:
                cb.setText(result)
            self._show_toast(result, QColor("#73EFF7"))
            return
        if not isinstance(result, QImage):
            return
        if mode == "text":
            job = OcrJob(self)
            job.finished.connect(lambda text, j=job: self._on_ocr_finished(j, text))
            self._ocr_jobs.append(job)
            job.start(result)
            self._show_toast("READING", QColor("#FFCD75"))
            return
        if cb is not None:
            cb.setImage(result)
        saved = self._save_snip_png(result)
        if saved is not None:
            self.recent.add(saved)
        self._show_toast("SNIPPED" if saved else "COPIED", QColor("#38B764"))

    @staticmethod
    def _save_snip_png(image: QImage) -> Path | None:
        """Save to Pictures/ByteSprite, else ~/.bytesprite/snips; never raise into Qt handlers."""
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss_zzz")
        # OneDrive-redirected or Controlled Folder Access Pictures dirs can refuse writes
        for out_dir in (Path(pictures) / "ByteSprite" if pictures else None, Path.home() / ".bytesprite" / "snips"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            target = out_dir / f"snip_{stamp}.png"
            if image.save(str(target), "PNG"):
                return target
        return None

    def _on_ocr_finished(self, job: OcrJob, text: str) -> None:
        if job in self._ocr_jobs:
            self._ocr_jobs.remove(job)
        job.deleteLater()
        if self._is_shutting_down:
            return
        if not text:
            self._show_toast("NO TEXT", QColor("#EF7D57"))
            return
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self._show_toast("TEXT OK", QColor("#38B764"))

    def trigger_tool_action(self, callback_id: str) -> None:
        if callback_id.startswith("snip:"):
            self.trigger_snip(callback_id.split(":", 1)[1])
            return
        if callback_id == "forge:pick_files":
            selected, _flt = QFileDialog.getOpenFileNames(
                self,
                "ByteSprite File Forge - Select File(s)",
            )
            if selected:
                self.trigger_drop_files(selected)
            return
        if callback_id == "forge:pick_folder":
            folder = QFileDialog.getExistingDirectory(
                self,
                "ByteSprite File Forge - Select Folder to Compress",
            )
            if folder:
                self.trigger_drop_files([folder])
            return
        if callback_id.startswith("recent:"):
            key = callback_id.removeprefix("recent:")
            if key == "snips":
                self.open_output_folder(self.snips_dir())
            elif key == "drops":
                self.open_output_folder(self._web_drop_dir())
            elif key.isdigit():
                self.reveal_recent(int(key))
            return
        if callback_id.startswith("web:"):
            QDesktopServices.openUrl(QUrl(callback_id.removeprefix("web:")))
            return
        if callback_id == "pet:search":
            cb = QGuiApplication.clipboard()
            query = " ".join((cb.text() if cb is not None else "").split())[:500]
            if not query:
                self._show_toast("NO TEXT", QColor("#EF7D57"))
                return
            QDesktopServices.openUrl(QUrl(google_search_url(query)))
            self._show_toast("SEARCH", QColor("#38B764"))
            return
        if callback_id == "pet:mute":
            if sys.platform == "win32":
                u32 = ctypes.windll.user32
                u32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
                u32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
                self._show_toast("MUTE", QColor("#8A9AF2"))
            return
        if callback_id == "pet:clips":
            self._open_clip_history(QCursor.pos())
            return
        if callback_id == "project:pick":
            folder = QFileDialog.getExistingDirectory(self, "ByteSprite - Choose Project Folder")
            project = self._state_machine.registry.get_plugin("project")
            if folder and project is not None:
                project.set_repo(folder)
                self._show_toast(Path(folder).name[:10].upper(), QColor("#73EFF7"))
            return
        if callback_id == "forge:clipboard_files":
            clip_paths = self._clipboard_local_paths()
            if not clip_paths:
                self._show_toast("NO FILE", QColor("#EF7D57"))
                return
            self.trigger_drop_files(clip_paths)
            return

        handled, toast_text, color_hex = self._state_machine.execute_tool_action(callback_id)
        if handled and toast_text:
            self._show_toast(toast_text, QColor(color_hex or "#73EFF7"))

    def trigger_toggle_sleep(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._state_machine.wake()
            self._show_toast("ONLINE", QColor("#73EFF7"))
        else:
            self._state_machine.rest()
            self._show_toast("DND", QColor("#8A9AF2"))

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

    def hit_test_point(self, local_pos: QPoint) -> bool:
        """Return True if local_pos hits opaque sprite pixels or an active HUD card."""
        if self._is_hud_visible() and self._hud_rect.contains(local_pos):
            return True
        if not self._sprite_rect.contains(local_pos):
            return False

        frame = self._anim.current_frame()
        h = len(frame)
        w = len(frame[0]) if h > 0 else 0
        if w == 0 or h == 0:
            return False

        sx = (local_pos.x() - self._sprite_rect.left()) // self.SPRITE_SCALE
        sy = (local_pos.y() - self._sprite_rect.top()) // self.SPRITE_SCALE
        for ny in range(max(0, sy - 1), min(h, sy + 2)):
            for nx in range(max(0, sx - 1), min(w, sx + 2)):
                if frame[ny][nx][3] > 0:
                    return True
        return False

    def _is_interactive_hit(self, local_pos: QPoint) -> bool:
        return self.hit_test_point(local_pos)

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
            self._last_drag_global_pos = global_pos
            self._drag_offset = global_pos - self.frameGeometry().topLeft()
            self._did_drag = False
            self._walk_steps_remaining = 0
            self._hold_timer.start()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
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
            if self._wheel_holding:
                if self._tool_wheel is not None:
                    self._tool_wheel.update_hover_from_global(global_pos)
                event.accept()
                return
            delta = global_pos - self._press_global_pos
            if delta.manhattanLength() >= self.DRAG_THRESHOLD_PX:
                self._did_drag = True
                self._hold_timer.stop()
            if self._did_drag:
                step_dx = (
                    (global_pos.x() - self._last_drag_global_pos.x())
                    if self._last_drag_global_pos is not None
                    else delta.x()
                )
                if step_dx != 0:
                    self._anim.facing_left = step_dx < 0
                self._last_drag_global_pos = global_pos
                candidate = global_pos - self._drag_offset
                self.move_clamped(candidate.x(), candidate.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_global_pos is not None:
            self._hold_timer.stop()
            if self._wheel_holding:
                self._wheel_holding = False
                self._press_global_pos = None
                self._last_drag_global_pos = None
                self._drag_offset = None
                self._did_drag = False
                if self._tool_wheel is not None and self._tool_wheel.isVisible():
                    self._tool_wheel.release_at(event.globalPosition().toPoint())
                event.accept()
                return
            was_drag = self._did_drag
            self._press_global_pos = None
            self._last_drag_global_pos = None
            self._drag_offset = None
            self._did_drag = False
            if not was_drag:
                self._click_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._is_interactive_hit(
            event.position().toPoint()
        ):
            self._click_timer.stop()
            self.trigger_snip("image")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is None:
            return
        if not self._is_interactive_hit(event.pos()):
            event.ignore()
            return
        self._open_context_menu(event.globalPos())
        event.accept()

    @staticmethod
    def _extract_drop_paths(mime) -> list[Path]:
        if mime is None or not mime.hasUrls():
            return []
        # No exists() here: a stat per file stalls big drops, and File Forge filters missing paths
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is None:
            return
        mime = event.mimeData()
        paths = self._drag_paths = self._extract_drop_paths(mime)
        self._pending_web_source = None if paths else self._web_drop_source(mime)
        if paths or self._pending_web_source is not None:
            forge = self._state_machine.forge_plugin
            if forge is not None and forge.busy:
                self._pending_web_source = None
                self._show_toast("BUSY!", QColor("#EF7D57"))
                event.ignore()
                return
            self._hovered = True
            self._walk_steps_remaining = 0
            if self._drop_wheel is not None and self._drop_wheel.isVisible():
                pass  # cursor slid back off the wheel mid-drag; respawning restarts its open animation
            elif paths:
                self._spawn_drop_wheel(paths, event.position().toPoint())
            else:
                source = self._pending_web_source
                stem = web_image_stem(source) if isinstance(source, str) else "web_image"
                # Re-encoding slices only: archiving would pass the downloaded image through untouched
                options = [
                    o for o in get_wheel_options_for_paths([Path("web.png")])
                    if o.mode not in (ForgeMode.ZIP, ForgeMode.TAR_XZ)
                ]
                self._spawn_drop_wheel([Path(f"{stem}.png")], event.position().toPoint(), options)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent | None) -> None:
        if event is None:
            return
        if self._pending_web_source is not None or self._drag_paths:
            if self._drop_wheel is not None and self._drop_wheel.isVisible():
                self._drop_wheel.update_hover_from_global(
                    self.mapToGlobal(event.position().toPoint())
                )
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent | None) -> None:
        self._hovered = False
        self.update()
        if event is not None:
            super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is None:
            return
        paths = self._extract_drop_paths(event.mimeData())
        if self._drop_wheel is not None and self._drop_wheel.isVisible():
            chosen = self._drop_wheel.update_hover_from_global(
                self.mapToGlobal(event.position().toPoint())
            )
            self._drop_wheel.dismiss()
            if chosen is not None and (paths or self._pending_web_source is not None):
                self._dispatch_drop(paths, chosen.mode)
            else:
                self._on_wheel_cancelled()
            event.acceptProposedAction()
            return
        if paths or self._pending_web_source is not None:
            self._dispatch_drop(paths, None)
            event.acceptProposedAction()
            return
        event.ignore()

    def build_context_menu(self) -> PetContextMenu:
        return PetContextMenu(
            state_machine=self._state_machine,
            always_on_top=self._always_on_top,
            auto_wander=self._auto_wander,
            on_feed=self.trigger_feed,
            on_play=self.trigger_play,
            on_pet=self.trigger_pet,
            on_select_tool=self.trigger_select_tool,
            on_tool_action=self.trigger_tool_action,
            on_toggle_sleep=self.trigger_toggle_sleep,
            on_reset_position=self.reset_to_default_position,
            on_toggle_on_top=self.set_always_on_top,
            on_toggle_wander=self.set_auto_wander,
            on_quit=self.trigger_quit,
            recent_files=self.recent.entries(),
            parent=self,
        )

    def _open_context_menu(self, global_pos: QPoint) -> None:
        menu = self.build_context_menu()
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

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
        elif self._lyric_line:
            self._paint_lyric(painter)

        if self._toast_steps > 0 and self._toast_text:
            self._paint_toast(painter)

        painter.end()

    def _paint_hud(self, painter: QPainter) -> None:
        """Frosted-graphite card matching PetContextMenu: rounded panel, hairline, Segoe UI."""
        r = self._hud_rect
        hud = self._state_machine.get_hud_data()
        sleeping = self._state_machine.state == PetState.SLEEPING
        plugin = self._state_machine.active_plugin
        title = plugin.display_name if plugin is not None else hud.badge_left

        painter.save()
        # Smooth shapes only inside the card; the sprite keeps nearest-neighbor rendering
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(QPen(qcolor(HAIRLINE_RGBA), 1))
        painter.setBrush(qcolor(GLASS_RGBA))
        painter.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)

        # Header: name + colored status dot, like the menu header card
        header = QRect(r.left() + 10, r.top() + 5, r.width() - 20, 14)
        status_text = "DND" if sleeping else hud.badge_right
        # Short codes (OK, DND, ERR) stay upper case; words read like the menu's "Online"
        status = f"● {status_text if len(status_text) <= 3 else status_text.title()}"
        painter.setFont(ui_font(7))
        painter.setPen(QColor("#8A9AF2") if sleeping else QColor(hud.badge_right_color))
        painter.drawText(header, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, status)
        status_w = painter.fontMetrics().horizontalAdvance(status) + 6

        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        title_w = header.width() - status_w
        painter.drawText(
            QRect(header.left(), header.top(), title_w, header.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_w),
        )

        painter.setPen(QPen(qcolor(HAIRLINE_RGBA), 1))
        painter.drawLine(r.left() + 10, r.top() + 21, r.right() - 10, r.top() + 21)

        for idx, bar in enumerate(hud.bars[:3]):
            row = QRect(r.left() + 10, r.top() + 25 + idx * 11, r.width() - 20, 10)
            painter.setFont(ui_font(6.5))
            painter.setPen(QColor(TEXT_DIM_HEX))
            painter.drawText(
                QRect(row.left(), row.top(), 34, row.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                bar.label,
            )
            track = QRectF(row.left() + 36, row.center().y() - 1.5, row.width() - 68, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(HOVER_RGBA))
            painter.drawRoundedRect(track, 2, 2)
            fill_w = (bar.clamped_value / 100.0) * track.width()
            if fill_w > 0:
                painter.setBrush(QColor(bar.color_hex))
                painter.drawRoundedRect(QRectF(track.left(), track.top(), max(4.0, fill_w), track.height()), 2, 2)
            painter.setPen(QColor(TEXT_HEX))
            painter.drawText(
                QRect(row.right() - 28, row.top(), 28, row.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                bar.formatted_value.strip(),
            )

        if hud.detail:
            detail_rect = QRect(r.left() + 10, r.top() + 58, r.width() - 20, 11)
            painter.setFont(ui_font(6.5))
            painter.setPen(QColor(TEXT_DIM_HEX))
            painter.drawText(
                detail_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(hud.detail, Qt.TextElideMode.ElideRight, detail_rect.width()),
            )
        painter.restore()

    def _paint_lyric(self, painter: QPainter) -> None:
        """Speech bubble in the frosted-graphite style, bottom-anchored just above the sprite."""
        flags = Qt.TextFlag.TextWordWrap
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(ui_font(11, bold=True))
        max_w = self.width() - 16
        text_rect = painter.fontMetrics().boundingRect(QRect(0, 0, max_w - 16, 1000), flags, self._lyric_line)
        # Three lines at most keeps the bubble clear of the top of the window
        text_h = min(text_rect.height(), painter.fontMetrics().lineSpacing() * 3)
        bubble_w = max(40, min(max_w, text_rect.width() + 16))
        bubble = QRect((self.width() - bubble_w) // 2, self._sprite_rect.top() - 4 - (text_h + 12), bubble_w, text_h + 12)
        painter.setPen(QPen(qcolor(HAIRLINE_RGBA), 1))
        painter.setBrush(qcolor(GLASS_RGBA))
        painter.drawRoundedRect(QRectF(bubble).adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(bubble.adjusted(8, 6, -8, -6), int(Qt.AlignmentFlag.AlignCenter) | int(flags), self._lyric_line)
        painter.restore()

    def _paint_toast(self, painter: QPainter) -> None:
        progress = 1.0 - (self._toast_steps / max(1, self._toast_max_steps))
        rise_px = int(progress * 8)
        alpha = max(80, min(255, int(255 * (self._toast_steps / max(1, self._toast_max_steps)) * 1.35)))

        pill_w = max(68, min(self.width() - 12, len(self._toast_text) * 7 + 16))
        pill_h = 18
        pill_x = (self.width() - pill_w) // 2
        min_y = (self._hud_rect.bottom() + 3) if self._is_hud_visible() else 4
        pill_y = max(min_y, self._sprite_rect.top() - 22 - rise_px)
        pill = QRect(pill_x, pill_y, pill_w, pill_h)

        bg = QColor(24, 20, 37, min(240, alpha))
        painter.fillRect(pill.adjusted(1, 0, -1, 0), bg)
        painter.fillRect(pill.adjusted(0, 1, 0, -1), bg)

        border = QColor(self._toast_color)
        border.setAlpha(min(180, alpha))
        painter.setPen(QPen(border, 1))
        painter.drawRect(pill.adjusted(1, 1, -2, -2))

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
        self._is_shutting_down = True
        self._anim_timer.stop()
        self._ambient_timer.stop()
        self._topmost_timer.stop()
        self._overlay_timer.stop()
        if self._snip_session is not None:
            self._snip_session.close()
            self._snip_session = None
        if self._drop_wheel is not None:
            self._drop_wheel.dismiss()
            self._drop_wheel.close()
            self._drop_wheel.deleteLater()
            self._drop_wheel = None
        self._hold_timer.stop()
        if self._tool_wheel is not None:
            self._tool_wheel.dismiss()
            self._tool_wheel.close()
            self._tool_wheel.deleteLater()
            self._tool_wheel = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None  # type: ignore[assignment]
        if self._unsub_stats is not None:
            self._unsub_stats()
            self._unsub_stats = None  # type: ignore[assignment]
        self._state_machine.shutdown()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if not self._is_shutting_down:
            self.trigger_quit()
        else:
            self.shutdown()
        if event is not None:
            super().closeEvent(event)
