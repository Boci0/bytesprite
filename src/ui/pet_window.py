"""Frameless translucent floating desktop window for ByteSprite."""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
import os
import random
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QDateTime, QPoint, QProcess, QRect, QRectF, QStandardPaths, Qt, QTimer, QUrl
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
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu, QToolTip, QWidget

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.graphics.sprite_matrix import FrameMatrix, get_sprite_frames
from src.tools.file_forge import ForgeMode, ForgeWheelOption, get_wheel_options_for_paths
from src.tools.ai_assist import AiAssistPlugin, AiJob, AiReply, fit_thinking, load_key, thinking_levels
from src.tools.ai_tools import skills_dir
from src.tools.macros import MacroBook
from src.tools.hints import HintsPlugin, Suggestion, foreground_app, foreground_title
from src.tools.watch import (ACTION_LABELS, EDITORS, LABELS as WATCH_LABELS, SOURCES as WATCH_SOURCES, WatchReply,
                              action_lenses, apply_moves, ask_context, editor_title_parts, editor_workspace,
                              folder_check, hotkey_lens, run_lens, undo_moves)
from src.tools import computer
from src.ui.ai_chat import AiChat
from src.ui.ai_tool_host import AiToolHost, grab_screen
from src.ui.screen_glow import ScreenGlow, play_screen_glow
from src.storage.recent_files import RecentFiles
from src.storage.storage_manager import log_text
from src.tools.web_drop import WebImageJob, html_image_src, is_web_image_source, web_image_stem
from src.ui.context_menu import PetContextMenu, style_menu
from src.ui.drop_wheel import RadialDropWheel
from src.ui.macro_editor import MacroEditor
from src.ui.macro_runner import MacroRunner
from src.ui.quick_tools import OcrJob, SnipSession, open_folder_in_background
from src.ui.sfx import play_cue, preload_tick_sounds, prewarm as prewarm_ticks
from src.ui.tool_wheel import WEB_SHORTCUTS, ToolWheel, ToolWheelOption, WheelLayout, google_search_url
from src.updater import APP_DIR, RELEASES_URL, UpdateJob, is_installed
from src.ui.theme import (ACCENT_HEX, GLASS_RGBA, HAIRLINE_RGBA, HOVER_RGBA, SLEEP_HEX, TEXT_DIM_HEX, TEXT_HEX,
                          paint_pixel_panel, pet_accent, qcolor, ui_font)


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


def yield_topmost(own: set[int], u32: object | None = None) -> bool:
    """True while re-raising the pet would cover something the user is working with.

    That is: a mouse button is held (dragging a window into a snap zone), or the foreground window or the
    top-level window under the cursor is someone else's topmost window (Snap Assist, Alt+Tab, Task View,
    Start, the snap layouts flyout, other apps' always-on-top windows).
    """
    if u32 is None:
        u32 = ctypes.windll.user32
        u32.WindowFromPoint.argtypes = [wintypes.POINT]
        for fn in (u32.WindowFromPoint, u32.GetForegroundWindow, u32.GetAncestor):
            fn.restype = ctypes.c_void_p
        u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    if any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04)):  # left, right, middle button
        return True
    pt = wintypes.POINT()
    under = u32.GetAncestor(u32.WindowFromPoint(pt), 2) if u32.GetCursorPos(ctypes.byref(pt)) else None  # GA_ROOT
    return any(hwnd and hwnd not in own and u32.GetWindowLongW(hwnd, -20) & 0x8  # GWL_EXSTYLE, WS_EX_TOPMOST
               for hwnd in (u32.GetForegroundWindow(), under))


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
        # Lyric card: `_lyric_shown` survives blank LRC lines (dimmed) so the card never blinks out
        # mid-song; lines crossfade in place while the card height eases from where it currently is
        self._lyric_shown: str = ""
        self._lyric_prev: str = ""
        self._lyric_changed_ms: float = 0.0
        self._lyric_dim_ms: float = 0.0
        self._lyric_visible: bool = False
        self._lyric_vis_ms: float = 0.0
        self._lyric_from_h: float = 0.0
        self._lyric_cur_h: float = 0.0
        self._lyric_title: str = ""
        self._lyric_fit_cache: dict[str, tuple[float, str, int]] = {}
        self._lyric_timer = QTimer(self)
        self._lyric_timer.setInterval(40)  # 25fps, only while the card animates or the waiting dots bounce
        self._lyric_timer.timeout.connect(self._on_lyric_frame)

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
        self._ai_chat: AiChat | None = None  # built on first open
        self._ai_thinking: bool = False
        self._glows: list[ScreenGlow] = []
        # Image dragged out of a browser (URL or raw image), fetched only once a slice is chosen
        self._pending_web_source: str | QImage | None = None
        # Raw path strings cached at drag-enter: re-reading or parsing thousands per event lags big drops
        self._drag_paths: list[str] = []
        self._web_jobs: list[WebImageJob] = []
        self._update_jobs: list[UpdateJob] = []
        # Clean PNG from a web drop, deleted once File Forge converts it to the chosen format
        self._web_intermediate: Path | None = None
        self.recent = RecentFiles()
        self.wheel = WheelLayout()  # Settings > Customize Wheel, persisted as tools.wheel
        self.macros = MacroBook()  # right-click > Macros, persisted as tools.macros
        self._macro_runner = MacroRunner(self)
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

        # Offline context hints (Hints tool): a clickable card over the sprite, polled every 3s
        self._hint: Suggestion | None = None
        self._hint_until: float = 0.0
        self._hint_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 40)
        self._hint_timer = QTimer(self)
        self._hint_timer.setInterval(3000)
        self._hint_timer.timeout.connect(self._poll_hint)
        self._hint_timer.start()
        # Watch mode: one lens answer at a time, for the hint on the card
        self._watch_job: AiJob | None = None
        self._watch_reply: AiReply | None = None
        self._watch_open = False  # the user clicked: open the chat when the answer lands
        self._folder_job: AiJob | None = None
        self._tidy_asks = 0  # a fresh approval key per plan, so "Allow" never carries over to the next one

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
        if QApplication.activePopupWidget() is not None or QToolTip.isVisible():  # raising would cover it
            return
        wheels = self._open_wheels()
        own = {int(w.winId()) for w in (self, *wheels, self._ai_chat) if w is not None and w.isVisible()}
        if yield_topmost(own):  # the next tick tries again
            return
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE: re-raise without stealing focus
        set_pos = ctypes.windll.user32.SetWindowPos
        set_pos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        if not wheels:
            set_pos(int(self.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        for wheel in wheels:
            wheel.restack_topmost()  # wheels stay above the pet but below an in-flight drag image
        if self._ai_chat is not None and self._ai_chat.isVisible():  # the pet never covers its own chat
            set_pos(int(self._ai_chat.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

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

    def _base_clip(self, state: PetState) -> str:
        mapping = {
            PetState.IDLE: "idle",
            PetState.SLEEPING: "sleep",
            PetState.PLAYING: "focus",
            PetState.HUNGRY: "hungry",
        }
        clip = mapping.get(state, "idle")
        if self._ai_thinking and clip == "idle":
            return "focus"
        return "music" if clip == "idle" and self._state_machine.music_playing else clip

    def _sync_animation_to_pet_state(self, state: PetState) -> None:
        self._anim.set_state(self._base_clip(state))

    def _handle_state_change(
        self, _old_state: PetState, new_state: PetState, _stats: PetStats
    ) -> None:
        self._walk_steps_remaining = 0
        self._sync_animation_to_pet_state(new_state)
        # No HUD pin here: RAM/CPU hovering near the 90% alert line flips state on its own,
        # which popped the card up out of nowhere; the clip change already shows the state
        self._schedule_next_anim_tick()
        self.update()

    def _handle_stats_change(self, _stats: PetStats) -> None:
        self._collect_forge_outputs()
        self._sync_lyric_card()
        clip = self._base_clip(self._state_machine.state)
        if self._walk_steps_remaining == 0 and self._anim.base_state != clip:
            self._anim.set_state(clip)  # a song started or stopped
            self._schedule_next_anim_tick()
            self.update()
        notifications = self._state_machine.pop_notifications()
        if notifications:
            toast_text, color_hex = notifications[-1]
            if toast_text == "FOCUS DONE":
                self._anim.play_oneshot("cheer")
                self._schedule_next_anim_tick()
            elif self._state_machine.state == PetState.IDLE:
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
        if roll < 0.08 and self._anim.base_state == "idle":  # no yawning mid-song
            self._anim.play_oneshot("yawn")
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.48:
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
                    self._anim.set_state(self._base_clip(PetState.IDLE))
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
        # Toasts alone never open the HUD card (background notices would pop it); view switches pin it
        if not self._overlay_timer.isActive():
            self._overlay_timer.start()
        self.update()

    def _is_hud_visible(self) -> bool:
        if self._open_wheels() or self._hint is not None:
            return False  # the HUD would cover wheel slices or the hint card being clicked
        return self._hovered or self._hud_pinned_until_ticks > 0

    def _hints_plugin(self) -> HintsPlugin | None:
        plugin = self._state_machine.registry.get_plugin("hints")
        if not isinstance(plugin, HintsPlugin):
            return None
        ai = self._state_machine.registry.get_plugin("ai")
        if isinstance(ai, AiAssistPlugin):
            plugin.levels = lambda: thinking_levels(ai.base_url, ai.model)
        return plugin

    def _poll_hint(self) -> None:
        app = foreground_app()  # ~0.03ms per 3s tick
        if (hints := self._hints_plugin()) is not None:
            hints.note_app(app)
        if self._hint is not None:
            if time.monotonic() >= self._hint_until and not self._watch_open:
                hints = self._hints_plugin()
                if (self._watch_reply is not None or self._watch_job is not None) and hints is not None:
                    hints.ignored_ready()  # a pre-fetch the user let expire, finished or not
                self._clear_hint()
            return
        hints = self._hints_plugin()
        if (hints is None or not hints.enabled or self._state_machine.state == PetState.SLEEPING
                or self._open_wheels() or self._press_global_pos is not None or self._snip_session is not None):
            return
        cb = QGuiApplication.clipboard()
        pomo = self._state_machine.pomodoro_plugin
        hint = hints.poll(app, cb.text() if cb is not None else "",
                          pomo is not None and not pomo.running, title=foreground_title(),
                          watch_ok=hints.watch and self._watch_ready())
        if hint is not None and hint.action == "watch:tidy" and not hint.data:
            self._check_folder(hint, foreground_title())  # the card shows only if the folder is messy
            return
        if hint is not None:
            self._hint, self._hint_until = hint, time.monotonic() + 12.0
            if hint.action.startswith("watch:") and hints.can_prefetch(hint):
                self._start_watch(hint, hint.data)
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            self.update()

    def _clear_hint(self) -> None:
        self._hint = None
        self._drop_watch()
        self.update()

    def _run_hint(self) -> None:
        hint, hints = self._hint, self._hints_plugin()
        self._hint = None  # not _clear_hint: a running Watch answer is kept for the click
        self.update()
        if hint is None:
            return
        if hints is not None:
            hints.mark_used()
        if hint.action.startswith("watch:"):
            self._run_watch(hint)
            return
        if hint.action == "pet:focus":
            self.trigger_play()
        else:
            self.trigger_tool_action(hint.action)

    # Watch mode: the model is called on a click, or ahead of it when hints.can_prefetch allows

    def _watch_ready(self) -> bool:
        """A key and a model are set, the chat is not mid-answer and no lens answer is in flight."""
        ai = self._state_machine.registry.get_plugin("ai")
        return (isinstance(ai, AiAssistPlugin) and bool(ai.model) and self._watch_job is None
                and (self._ai_chat is None or self._ai_chat._job is None) and ai.has_key())

    def _start_watch(self, hint: Suggestion, data: str | QImage | None) -> None:
        """`data` is the clipboard text, or the screen grab for screen lenses."""
        hints, ai = self._hints_plugin(), self._state_machine.registry.get_plugin("ai")
        if hints is None or not isinstance(ai, AiAssistPlugin) or data is None or data == "":
            self._clear_hint()
            return
        if hints.budget_left() <= 0 and hint.rule != "watch_now":  # the hotkey is the user asking, like the chat
            self._show_toast("AI BUDGET USED", QColor("#EF7D57"))
            self._clear_hint()
            return
        lens = hint.action.removeprefix("watch:")
        url, key, model, thinking = ai.base_url, load_key(ai.base_url), ai.model, fit_thinking(ai.base_url, hints.watch_thinking, ai.model)
        image = data if not isinstance(data, str) else None
        hints.record_ai(0)  # counted at the start, so a failing provider cannot loop past the budget

        def run() -> AiReply:
            text = AiToolHost._ocr(image) if image is not None else data
            reply = run_lens(lens, text, url, key, model, thinking=thinking)
            reply.tools = [*reply.tools, (WATCH_SOURCES.get(lens, "read_clipboard()"), text)]
            return reply

        job = self._watch_job = AiJob(self)
        job.finished.connect(lambda reply, error, h=hint: self._on_watch_done(job, h, reply, error))
        job.start(run)
        QTimer.singleShot(60, lambda: self._warm_watch(ai))  # after the card repaints

    def _warm_watch(self, ai: AiAssistPlugin) -> None:
        """While the model works: wake the audio output (a ~0.2s blocking resume) and build the chat, so the
        answer opens without those hitches. The output stays awake IDLE_SUSPEND_MS (20s)."""
        if self._watch_job is None:
            return
        prewarm_ticks()
        if self._ai_chat is None:
            self._make_ai_chat(ai)

    def _on_watch_done(self, job: AiJob, hint: Suggestion, reply: AiReply | None, error: str) -> None:
        job.deleteLater()
        if job is not self._watch_job:
            return  # dismissed or replaced meanwhile
        self._watch_job = None
        hints = self._hints_plugin()
        if hints is not None and reply is not None:
            hints.ai_tokens += max(0, reply.total_tokens)
        if error or reply is None:
            self._watch_open = False
            self._show_toast("AI ERROR", QColor("#EF7D57"))
            self._clear_hint()
            return
        self._watch_reply = reply
        if self._watch_open:
            self._show_watch(hint)
        elif self._hint is hint:
            self._hint = Suggestion(hint.rule, "Answer ready. Click to see it", hint.action, hint.data)
            self._hint_until = time.monotonic() + 20.0
            self.update()

    def _run_watch(self, hint: Suggestion) -> None:
        if self._watch_reply is not None:
            self._show_watch(hint)
            return
        self._hint, self._hint_until = Suggestion(hint.rule, "Working on it...", hint.action, hint.data), 0.0
        self._watch_open = True
        self.update()
        if self._watch_job is not None:
            return
        if not self._watch_ready():
            self._clear_hint()
            self.open_ai_chat()  # shows Setup when no key or model is set
            return
        if hint.data:
            self._start_watch(hint, hint.data)
        else:  # screen lenses read the screen only now, after the click
            grab_screen(self, lambda image, h=hint: self._start_watch(h, image) if self._watch_open else None,
                    window=True)

    def watch_now(self) -> None:
        """Alt+`, Ctrl+Shift+Alt+W or the menu: ask about the front window. The chat opens with that window
        attached, the app and title as context, the Project Folder set from an editor or File Explorer window, and
        quick actions (Enter on an empty draft runs the best fit). A typed question goes to the full agent.
        Works with Watch mode off; actions are counted but not stopped by the daily budget (an explicit ask)."""
        if self._watch_job is not None or self._watch_open:
            self._show_toast("ALREADY ON IT", QColor("#8A9AF2"))
            return
        if not self._watch_ready():
            self.open_ai_chat()  # Setup when no key or model; a chat mid-answer is shown as is
            return
        app, title = foreground_app(), foreground_title()
        lens = hotkey_lens(app, title)
        project = editor_workspace(app, title)
        grab_screen(self, lambda image: self._open_ask(app, title, lens, project, image), window=True)

    def _open_ask(self, app: str, title: str, lens: str, project: str, image: QImage | None) -> None:
        chat = self.ensure_ai_chat()
        if chat is None or chat._job is not None:
            return
        chat.clear_ask()
        label = EDITORS.get(app.lower(), ("", ""))[1] or app.removesuffix(".exe").removesuffix(".EXE") or "Screen"
        if image is not None and not image.isNull():
            chat.attach_image(image, f"{label} window")
        if project:
            self._use_project(project, f"from {label}")
        file = editor_title_parts(title)[0] if project else ""

        def actions(folder: str = "") -> list[tuple[str, Callable[[], None]]]:
            out = []
            for each in action_lenses(lens):
                if each == "tidy" and not folder:
                    continue  # tidy needs the folder path; offered once File Explorer answers
                data = folder if each == "tidy" else image
                out.append((ACTION_LABELS.get(each, each), lambda l=each, d=data: self._run_action(l, d)))
            return out

        chat.set_ask(ask_context(app, title, project, file), actions())
        if app.lower() == "explorer.exe":  # resolve the folder (about 0.3s) for Tidy and the Project Folder
            job = self._folder_job = AiJob(self)

            def done(found: tuple[str, int] | None, _error: str) -> None:
                job.deleteLater()
                if job is not self._folder_job or found is None or chat._actions == []:
                    return
                self._folder_job = None
                self._use_project(found[0], "from File Explorer")
                chat.set_ask(ask_context(app, title, found[0]), actions(found[0]))

            job.finished.connect(done)
            job.start(lambda: folder_check(computer.foreground_window(), title, min_files=1))

    def _use_project(self, path: str, why: str) -> None:
        """Point the chat's file tools at `path` (the folder the user is looking at), with a visible note."""
        project = self._state_machine.registry.get_plugin("project")
        if project is None or str(getattr(project, "repo", "") or "") == path:
            return
        project.set_repo(path)
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Project Folder: {path} ({why}). The AI can read files there and edit them "
                                   "only with your OK.")

    def _run_action(self, lens: str, data: str | QImage | None) -> None:
        """A quick action from the ask card: the lens answer lands in the chat like a Watch answer."""
        hint = Suggestion("watch_now", "Working on it...", f"watch:{lens}", data if isinstance(data, str) else "")
        self._hint, self._hint_until, self._watch_open = hint, 0.0, True
        self.update()
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Working on: {ACTION_LABELS.get(lens, lens)}...")
        self._start_watch(hint, data)

    def _check_folder(self, hint: Suggestion, title: str) -> None:
        """Resolve the front Explorer window's folder on a worker thread (about 0.3s of PowerShell); the offered
        tidy hint shows only for a messy folder."""
        hwnd = computer.foreground_window()
        job = self._folder_job = AiJob(self)

        def done(found: tuple[str, int] | None, _error: str) -> None:
            job.deleteLater()
            if job is not self._folder_job:
                return
            self._folder_job = None
            if found is not None and self._hint is None and foreground_title() == title:
                self._hint = Suggestion(hint.rule, f"Tidy this folder? ({found[1]} files)", hint.action, found[0])
                self._hint_until = time.monotonic() + 12.0
                self._anim.play_oneshot("spin")
                self._schedule_next_anim_tick()
                self.update()

        job.finished.connect(done)
        job.start(lambda: folder_check(hwnd, title))

    def _show_watch(self, hint: Suggestion) -> None:
        reply = self._watch_reply
        self._clear_hint()
        chat = self.ensure_ai_chat()
        if reply is None or chat is None:
            return
        if not chat.add_watch_answer(WATCH_LABELS.get(hint.action.removeprefix("watch:"), "Watch"), reply):
            self._show_toast("CHAT BUSY", QColor("#EF7D57"))
            return
        if not isinstance(reply, WatchReply):
            return
        hints = self._hints_plugin()
        cb = QGuiApplication.clipboard()
        if reply.copy_text and cb is not None:
            cb.setText(reply.copy_text)  # copied, never typed or sent: the user pastes it where they want
            if hints is not None:
                hints.note_own_copy(reply.copy_text)
            self._show_toast("REPLY COPIED", QColor("#38B764"))
        if reply.moves:
            folder, moves = reply.folder, reply.moves
            n = sum(len(f) for f in moves.values())
            plan = "\n".join(f"{name}/  <-  " + ", ".join(files) for name, files in moves.items())
            self._tidy_asks += 1
            chat.confirm(f"tidy:{self._tidy_asks}", f"Move {n} files into {len(moves)} folders inside {folder}? "
                         "Nothing is deleted or overwritten.\n\n" + plan[:4000], "Tidy folder",
                         lambda ok: self._apply_tidy(folder, moves) if ok else None, once=False)

    def _apply_tidy(self, folder: str, moves: dict[str, list[str]]) -> None:
        done, created, skipped = apply_moves(folder, moves)
        hints = self._hints_plugin()
        if hints is not None and done:
            hints.last_tidy = (done, created)
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Moved {len(done)} files into {len(moves)} folders"
                                   + (f", skipped {skipped} (name taken or file gone)" if skipped else "")
                                   + ". Undo: right-click the pet, Hints, Undo Last Tidy.")
        self._show_toast(f"MOVED {len(done)}", QColor("#38B764"))

    def _undo_tidy(self) -> None:
        hints = self._hints_plugin()
        if hints is None or hints.last_tidy is None:
            return
        back = undo_moves(*hints.last_tidy)
        hints.last_tidy = None
        self._show_toast(f"PUT BACK {back}", QColor("#8A9AF2"))

    def _drop_watch(self) -> None:
        self._watch_job = None  # its late answer is ignored by _on_watch_done
        self._watch_reply = None
        self._watch_open = False

    def _dismiss_hint(self) -> None:
        hint, hints = self._hint, self._hints_plugin()
        self._clear_hint()
        if hint is not None and hints is not None:
            hints.dismiss(hint.rule)
        self._show_toast("MUTED", QColor("#8A9AF2"))

    # ------------------------------------------------------------------
    # Interactive Multi-Tool Actions
    # ------------------------------------------------------------------

    def trigger_feed(self) -> None:
        if self._state_machine.refresh_metrics():
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            cpu = int(round(self._state_machine.stats.cpu_usage))
            self._show_toast(f"CPU {cpu}%", QColor("#38B764"))
            self._pin_hud(36)

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
        self._pin_hud(36)

    def trigger_select_tool(self, tool_id: str) -> None:
        if self._state_machine.select_tool(tool_id):
            self._show_toast(tool_id.upper()[:6], QColor("#73EFF7"))
            self._pin_hud(36)

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
        paths: list[Path | str],
        option: ForgeWheelOption,
    ) -> None:
        self._hovered = False
        self._drag_paths = []
        self._dispatch_drop(paths, option.mode)

    def _on_wheel_cancelled(self) -> None:
        self._hovered = False
        self._drag_paths = []
        self._pending_web_source = None
        self._show_toast("SKIPPED", QColor("#8A9AF2"))

    def _dispatch_drop(self, paths: Sequence[Path | str], mode: ForgeMode | None) -> None:
        source, self._pending_web_source = self._pending_web_source, None
        if source is not None:
            self._start_web_fetch(source, mode or ForgeMode.PNG)
        elif paths:
            self.trigger_drop_files(paths, mode=mode)

    @staticmethod
    def _web_drop_source(mime) -> str | QImage | None:
        """The dragged <img>'s URL, else raw image data, else the first http(s)/data: URL."""
        if mime is None:
            return None
        # A linked image (YouTube avatars, thumbnails) drags the page URL first; the <img> is the picture
        src = html_image_src(mime.html()) if mime.hasHtml() else None
        if src is not None:
            return src
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

    def check_for_updates(self, *, install: bool = True) -> None:
        """Ask GitHub for a newer release; `install` also applies it (installed copies only)."""
        job = UpdateJob()
        job.finished.connect(lambda status, tag, j=job, i=install: self._on_update_done(j, status, tag, i))
        self._update_jobs.append(job)
        job.start(install=install)
        if install:
            self._show_toast("CHECKING", QColor("#8A9AF2"))

    def _on_update_done(self, job: UpdateJob, status: str, tag: str, install: bool) -> None:
        if job in self._update_jobs:
            self._update_jobs.remove(job)
        if status == "installed":
            self._show_toast("UPDATED", QColor("#38B764"))
            if self._on_notification is not None:
                self._on_notification(f"Updated to {tag}, restarting")
            QTimer.singleShot(1500, self.restart)
        elif status == "available":
            self._show_toast(f"NEW {tag}", QColor("#FFCD75"))
            if self._on_notification is not None:
                self._on_notification(f"ByteSprite {tag} is available: Settings > Check for Updates")
            if install and not is_installed():
                QDesktopServices.openUrl(QUrl(RELEASES_URL))  # git checkout: update by hand
        elif install:  # the silent startup check never toasts "up to date" or errors
            # On error `tag` carries the reason; signature failures mean the release was refused
            refused = {"bad signature": "BAD SIG", "unsigned release": "UNSIGNED",
                       "version mismatch": "BAD VERSION"}.get(tag)
            self._show_toast("UP TO DATE" if status == "current" else refused or "UPDATE ERR",
                             QColor("#38B764" if status == "current" else "#E05A47"))
            if refused and self._on_notification is not None:
                self._on_notification(f"Update refused ({tag}): it is not a release signed by the ByteSprite key")
            elif status == "error" and tag != "needs installer" and self._on_notification is not None:
                self._on_notification(f"Update failed: {tag}")  # the reason, also in the log (Settings > Copy Log)
            if tag == "needs installer":
                self._show_toast("RUN INSTALLER", QColor("#FFCD75"))
                if self._on_notification is not None:
                    self._on_notification("This update changes libraries: run install.ps1 from the releases page")
                QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def restart(self) -> None:
        self.trigger_quit()  # saves state.json before the new copy loads it
        QProcess.startDetached(sys.executable, ["-m", "src.main"], str(APP_DIR))

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
            self._show_drop_folder(path.parent)
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
        if outputs:
            self._show_drop_folder(outputs[0].parent)
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

    def _show_drop_folder(self, folder: Path) -> None:
        """Reveal where a drop's output landed, unless that folder is the visible desktop."""
        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        if desktop and os.path.normcase(str(folder)) == os.path.normcase(str(Path(desktop))):
            return
        open_folder_in_background(folder)

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
        paths: Sequence[Path | str],
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
        """Slices the user chose in Settings > Customize Wheel."""
        return self.wheel.visible(self.all_tool_wheel_options())

    def _wheel_toast(self, text: str) -> None:
        good = text in ("SHOWN", "ADDED", "REMOVED")
        self._show_toast(text, QColor("#38B764" if good else "#8A9AF2" if text in ("HIDDEN", "CANCEL") else "#EF7D57"))

    def _customize_wheel(self, key: str) -> None:
        if key.startswith("toggle:"):
            self._wheel_toast(self.wheel.toggle(key.removeprefix("toggle:"), self.all_tool_wheel_options()))
        elif key == "add_web":
            label, ok = QInputDialog.getText(self, "ByteSprite - Add Website", "Slice name:")
            url, ok2 = (QInputDialog.getText(self, "ByteSprite - Add Website", "URL:", text="https://")
                        if ok and label.strip() else ("", False))
            self._wheel_toast(self.wheel.add_custom(label, url) if ok2 else "CANCEL")
        elif key == "add_app":
            path, _flt = QFileDialog.getOpenFileName(self, "ByteSprite - Add App or File to Wheel")
            self._wheel_toast(self.wheel.add_custom(Path(path).stem, path) if path else "CANCEL")
        elif key.startswith("remove:") and key.removeprefix("remove:").isdigit():
            self._wheel_toast("REMOVED" if self.wheel.remove_custom(int(key.removeprefix("remove:"))) else "GONE")

    def all_tool_wheel_options(self) -> list[ToolWheelOption]:
        """Slices clockwise from 12 o'clock: everyday quick tools, then web shortcuts."""
        self.wheel.macros = [m.name for m in self.macros.macros]  # every wheel lookup passes through here
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
        options.append(ToolWheelOption("Ask AI", "chat", "#B48EF2", "ai:chat"))
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
        if mode == "ai":  # Ctrl+Shift+Alt+A opens the chat bar
            self.open_ai_chat()
            return
        if mode == "watch":  # Alt+` or Ctrl+Shift+Alt+W: Watch lens on the screen now
            self.watch_now()
            return
        if self._snip_session is not None:
            return
        if mode == "ask" and self._ai_chat is not None:
            self._ai_chat.hide()  # keep the bar out of the frozen grab
        # Hide ByteSprite and let the context menu fade so neither lands in the frozen grab
        self.hide()
        QTimer.singleShot(180, lambda: self._begin_snip(mode))

    def _begin_snip(self, mode: str) -> None:
        if self._is_shutting_down:
            return
        if self._snip_session is not None:  # a macro's region snip opened during the 180ms hide delay
            self.show()
            return
        self._snip_session = SnipSession(mode, self._on_snip_result)

    def _on_snip_result(self, mode: str, result: object | None) -> None:
        self._snip_session = None
        self.show()
        if mode == "ask":  # the chat's "Screen region" attachment
            if self._ai_chat is not None:
                if isinstance(result, QImage):
                    self._ai_chat.attach_image(result, "Screen region")
                self._ai_chat.open_near(self._sprite_global_rect())
            return
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

    # ------------------------------------------------------------------
    # AI Assist (manual only: nothing is sent until the user presses Send, or runs a macro they built with an `ask` step)
    # ------------------------------------------------------------------

    def open_ai_chat(self) -> None:
        """Toggle the chat card beside the pet; opening plays the pixel sparkle effect and a chime."""
        plugin = self._state_machine.registry.get_plugin("ai")
        if not isinstance(plugin, AiAssistPlugin):
            return
        if self._ai_chat is None:
            self._make_ai_chat(plugin)
        elif self._ai_chat.isVisible():
            self._ai_chat.hide()
            return
        sprite = self._sprite_global_rect()
        glow = play_screen_glow(self.screen(), sprite.center())
        if glow is not None:
            self._glows.append(glow)
            glow.destroyed.connect(lambda *_a, g=glow: self._glows.remove(g) if g in self._glows else None)
        play_cue("open")
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        self._ai_chat.open_near(sprite)

    def _make_ai_chat(self, plugin: AiAssistPlugin) -> None:
        """Build the chat card hidden (about 0.4s of widget setup on the Qt thread)."""
        self._ai_chat = AiChat(plugin, on_attach_screen=lambda: self.trigger_snip("ask"), tool_host=AiToolHost(self))
        self._ai_chat.thinking_changed.connect(self._on_ai_thinking)

    def ensure_ai_chat(self) -> AiChat | None:
        """Open the chat if it is closed (never toggles it shut, unlike open_ai_chat)."""
        if self._ai_chat is None or not self._ai_chat.isVisible():
            self.open_ai_chat()
        return self._ai_chat

    def _edit_macro(self, old_name: str = "") -> None:
        old = self.macros.find(old_name) if old_name else None
        number = self.macros.macros.index(old) + 1 if old else len(self.macros.macros) + 1
        editor = MacroEditor(old.name if old else "", old.recipe if old else "",
                             bool(old) and self.macros.double_click == old.name, number if number <= 9 else 0)
        if not editor.exec():
            return
        try:
            toast = self.macros.save(editor.name.text(), editor.recipe.text(), old.name if old else "")
        except ValueError:
            toast = "BAD STEP"
        name = " ".join(editor.name.text().split())
        if toast in ("ADDED", "SAVED"):
            if editor.double_click.isChecked():
                self.macros.double_click = name
            elif self.macros.double_click == name:
                self.macros.double_click = ""
        self._show_toast(toast, QColor("#38B764" if toast in ("ADDED", "SAVED") else "#EF7D57"))

    def _sprite_global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(self._sprite_rect.topLeft()), self._sprite_rect.size())

    def _on_ai_thinking(self, thinking: bool) -> None:
        self._ai_thinking = thinking
        if not self._is_shutting_down:
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()

    def trigger_tool_action(self, callback_id: str) -> None:
        if callback_id == "watch:now":
            self.watch_now()
            return
        if callback_id == "watch:undo_tidy":
            self._undo_tidy()
            return
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
        if callback_id == "ai:chat":
            self.open_ai_chat()
            return
        if callback_id == "ai:skills":
            self.open_output_folder(skills_dir())
            return
        if callback_id.startswith(("macro:", "macrokey:")):  # by name (wheel, menu) or number (hotkey)
            key = callback_id.partition(":")[2]
            macro = self.macros.find(key) if callback_id.startswith("macro:") else self.macros.numbered(key)
            if macro is None:
                self._show_toast("NO MACRO", QColor("#8A9AF2"))
            else:
                self._macro_runner.run(macro)
            return
        if callback_id.startswith("macros:"):
            action, _sep, name = callback_id.removeprefix("macros:").partition(":")
            if action == "new":
                self._edit_macro()
            elif action == "edit":
                self._edit_macro(name)
            elif action == "remove" and self.macros.remove(name):
                self._show_toast("REMOVED", QColor("#8A9AF2"))
            elif action == "dbl":
                self.macros.double_click = name if self.macros.find(name) else ""
                self._show_toast("DOUBLE-CLICK", QColor("#38B764"))
            return
        if callback_id.startswith("wheel:"):
            self._customize_wheel(callback_id.removeprefix("wheel:"))
            return
        if callback_id.startswith("open:"):  # a custom wheel slice: an app, file or folder the user picked
            try:
                os.startfile(callback_id.removeprefix("open:"))  # type: ignore[attr-defined]
                self._show_toast("OPEN", QColor("#38B764"))
            except OSError:
                self._show_toast("OPEN ERR", QColor("#EF7D57"))
            return
        if callback_id == "update:check":
            self.check_for_updates()
            return
        if callback_id == "log:copy":
            text, cb = log_text(), QGuiApplication.clipboard()
            if not text or cb is None:
                self._show_toast("NO LOG", QColor("#EF7D57"))
                return
            cb.setText(text)
            self._show_toast("LOG COPIED", QColor("#38B764"))
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
        if self._hint is not None and self._hint_rect.contains(local_pos):
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
        if (self._hint is not None and event.button() == Qt.MouseButton.LeftButton
                and self._hint_rect.contains(local_pos)):
            self._run_hint()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            self._press_global_pos = global_pos
            self._last_drag_global_pos = global_pos
            self._drag_offset = global_pos - self.frameGeometry().topLeft()
            self._did_drag = False
            self._wheel_holding = False  # a hold whose release never arrived (wheel watchdog closed it)
            self._walk_steps_remaining = 0
            self._hold_timer.start()
            prewarm_ticks()  # resume audio during the hold delay, not as the tool wheel appears
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
            macro = self.macros.find(self.macros.double_click) if self.macros.double_click else None
            if macro is not None:
                self._macro_runner.run(macro)
            else:
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
        if self._hint is not None and self._hint_rect.contains(event.pos()):
            self._dismiss_hint()
            event.accept()
            return
        self._open_context_menu(event.globalPos())
        event.accept()

    @staticmethod
    def _local_drop_files(mime) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        # Raw strings, no exists(): per-file Path/stat work stalls big drops, and File Forge
        # converts and filters missing paths on its worker thread. Non-file URLs give "".
        return [f for u in mime.urls() if (f := u.toLocalFile())]

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is None:
            return
        if self._drop_wheel is not None and self._drop_wheel.isVisible():
            # Cursor slid back off the wheel mid-drag: keep its paths and open animation as they are
            event.acceptProposedAction()
            return
        mime = event.mimeData()
        paths = self._drag_paths = self._local_drop_files(mime)
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
            if paths:
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
        # Same drag as the enter: reuse its paths instead of re-reading thousands of URLs
        paths, self._drag_paths = self._drag_paths or self._local_drop_files(event.mimeData()), []
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
            wheel_slices=[(o.label, o.callback_id, on) for o, on in self.wheel.catalog(self.all_tool_wheel_options())],
            wheel_custom=[label for label, _target in self.wheel.custom],
            macros=[m.name for m in self.macros.macros],
            double_click_macro=self.macros.double_click,
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

        # Crisp nearest-neighbor 4x pixel-art character
        painter.drawImage(self._sprite_rect, self._current_qimage())

        if self._hint is not None:
            self._paint_hint(painter)
        elif self._is_hud_visible():
            self._paint_hud(painter)
        elif self._lyric_visible or self._lyric_animating():
            self._paint_lyric(painter)

        if self._toast_steps > 0 and self._toast_text:
            self._paint_toast(painter)

        painter.end()

    def _paint_hud(self, painter: QPainter) -> None:
        """Pixel-frame card matching PetContextMenu: stepped panel, segmented bars, Segoe UI text."""
        r = self._hud_rect
        hud = self._state_machine.get_hud_data()
        sleeping = self._state_machine.state == PetState.SLEEPING
        plugin = self._state_machine.active_plugin
        title = plugin.display_name if plugin is not None else hud.badge_left

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_pixel_panel(painter, r)

        # Header: name + colored status dot, like the menu header card
        header = QRect(r.left() + 10, r.top() + 5, r.width() - 20, 14)
        status_text = "DND" if sleeping else hud.badge_right
        # Short codes (OK, DND, ERR) stay upper case; words read like the menu's "Online"
        status = f"● {status_text if len(status_text) <= 3 else status_text.title()}"
        painter.setFont(ui_font(7))
        painter.setPen(QColor(SLEEP_HEX) if sleeping else pet_accent(hud.badge_right_color))
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
            # Chunky 4px cells like the sprite's own pixels; any nonzero value lights the first cell
            cells = (row.width() - 68 + 1) // 5
            lit = math.ceil(bar.clamped_value / 100.0 * cells)
            for c in range(cells):
                cell = QRect(row.left() + 36 + c * 5, row.center().y() - 2, 4, 4)
                painter.fillRect(cell, pet_accent(bar.color_hex) if c < lit else qcolor(HOVER_RGBA))
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

    _LYRIC_FADE_MS = 240.0
    _LYRIC_DOT = QColor(ACCENT_HEX)

    def _lyric_waiting(self) -> bool:
        lyrics = self._state_machine.lyrics_plugin
        return lyrics is not None and lyrics.loading

    def _sync_lyric_card(self) -> None:
        """Turn plugin state into card transitions; called on every stats change."""
        now = time.monotonic() * 1000
        lyrics = self._state_machine.lyrics_plugin
        line = self._state_machine.lyric_line
        visible = lyrics is not None and (lyrics.has_lyrics or lyrics.loading)
        changed = False
        title = lyrics.title if lyrics is not None else ""
        if visible and title and title != self._lyric_title:
            # A new song opens with its title (dim, like a blank line) until the first lyric
            self._lyric_title = title
            if not line:
                line_or_title = "\u266a " + title
                self._lyric_prev, self._lyric_shown = self._lyric_shown, line_or_title
                self._lyric_changed_ms, self._lyric_from_h = now, self._lyric_cur_h
                changed = True
        if line and line != self._lyric_shown:
            self._lyric_prev, self._lyric_shown = self._lyric_shown, line
            self._lyric_changed_ms, self._lyric_from_h = now, self._lyric_cur_h
            changed = True
        if bool(line) != bool(self._lyric_line):
            self._lyric_dim_ms = now  # entering or leaving a blank (instrumental) line
            changed = True
        self._lyric_line = line
        if visible != self._lyric_visible:
            self._lyric_visible, self._lyric_vis_ms = visible, now
            changed = True
        if (changed or self._lyric_waiting()) and not self._lyric_timer.isActive():
            self._lyric_timer.start()
        if changed:
            self.update()

    def _lyric_animating(self) -> bool:
        now = time.monotonic() * 1000
        return any(now - ms < self._LYRIC_FADE_MS
                   for ms in (self._lyric_changed_ms, self._lyric_dim_ms, self._lyric_vis_ms))

    def _on_lyric_frame(self) -> None:
        if not (self._lyric_animating() or self._lyric_waiting()):
            if not self._lyric_visible:  # card gone: the next song starts fresh
                self._lyric_shown = self._lyric_prev = self._lyric_title = ""
                self._lyric_cur_h = self._lyric_from_h = 0.0
            self._lyric_timer.stop()  # a settled card is static
        self.update()

    def _fit_lyric(self, text: str) -> tuple[float, str, int]:
        """(point size, text, height) fitting the card body in 3 lines: shrink 9 -> 7pt, then trim."""
        cached = self._lyric_fit_cache.get(text)
        if cached is not None:
            return cached
        width, flags = self._hud_rect.width() - 20, int(Qt.TextFlag.TextWordWrap)
        if text.startswith("♪ "):  # the opening song title stays on one elided line
            fm = QFontMetrics(ui_font(8.5, bold=True))
            return 8.5, fm.elidedText(text, Qt.TextElideMode.ElideRight, width), fm.lineSpacing()

        def height(pt: float, s: str) -> tuple[int, int]:
            fm = QFontMetrics(ui_font(pt, bold=True))
            return fm.boundingRect(QRect(0, 0, width, 1000), flags, s).height(), fm.lineSpacing() * 3

        fit = None
        for pt in (9.0, 8.0, 7.0):
            h, limit = height(pt, text)
            if h <= limit:
                fit = (pt, text, h)
                break
        if fit is None:
            words, trimmed = text.split(), text
            while len(words) > 1:
                words.pop()
                trimmed = " ".join(words) + "\u2026"
                h, limit = height(7.0, trimmed)
                if h <= limit:
                    break
            fit = (7.0, trimmed, min(h, limit))
        if len(self._lyric_fit_cache) > 64:
            self._lyric_fit_cache.clear()
        self._lyric_fit_cache[text] = fit
        return fit

    def _paint_lyric(self, painter: QPainter) -> None:
        """Lyric card styled like the HUD and PetContextMenu, anchored just above the sprite.

        A song opens with its dim title (plus bouncing dots while lyrics are being found), then
        each line crossfades in place (no sliding) while the card height eases; the card fades in
        and out with a 4px drift. Blank LRC lines dim the last line instead of hiding the card.
        No per-word highlight: synced lyric timing is too loose for it. Long lines shrink to fit.
        """
        now = time.monotonic() * 1000

        def eased(ms: float) -> float:
            t = min(1.0, max(0.0, (now - ms) / self._LYRIC_FADE_MS))
            return 1 - (1 - t) ** 3

        vis = eased(self._lyric_vis_ms)
        alpha = vis if self._lyric_visible else 1 - vis
        if alpha <= 0.0:
            return
        waiting = not self._lyric_line and self._lyric_waiting()
        center = int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap)
        r = self._hud_rect
        dots_h = 14 if waiting else 0

        if self._lyric_shown:
            pt, shown, text_h = self._fit_lyric(self._lyric_shown)
        else:
            pt, shown, text_h = 9.0, "", 0
        target_h = text_h + dots_h + 14
        change = eased(self._lyric_changed_ms)
        h = target_h if not self._lyric_from_h else self._lyric_from_h + (target_h - self._lyric_from_h) * change
        self._lyric_cur_h = h

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setOpacity(alpha)
        card = QRectF(r.left(), self._sprite_rect.top() - 4 - h + 4 * (1 - alpha), r.width(), h)
        paint_pixel_panel(painter, card.toRect())

        body = QRectF(card.left() + 10, card.top() + 7, card.width() - 20, h - 14 - dots_h)
        if self._lyric_prev and change < 1.0:
            prev_pt, prev_text, _ = self._fit_lyric(self._lyric_prev)
            painter.setOpacity(alpha * (1 - change))
            painter.setFont(ui_font(prev_pt, bold=True))
            painter.setPen(QColor(TEXT_HEX))
            painter.drawText(body, center, prev_text)
        if shown:
            # Blank LRC lines (instrumental gaps) fade the line to the dim text color
            dim = eased(self._lyric_dim_ms)
            dim = dim if not self._lyric_line else 1 - dim
            bright, faint = QColor(TEXT_HEX), QColor(TEXT_DIM_HEX)
            color = QColor(
                round(bright.red() + (faint.red() - bright.red()) * dim),
                round(bright.green() + (faint.green() - bright.green()) * dim),
                round(bright.blue() + (faint.blue() - bright.blue()) * dim),
            )
            painter.setOpacity(alpha * (change if self._lyric_prev else 1.0))
            painter.setFont(ui_font(pt, bold=True))
            painter.setPen(color)
            painter.drawText(body, center, shown)
        if waiting:
            painter.setOpacity(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            cx, cy = body.center().x(), body.bottom() + 8 if shown else body.center().y()
            lit = int(now / 480) % 3
            for i in range(3):
                bounce = max(0.0, math.sin(now / 160 - i * 0.9)) * 4
                painter.setBrush(self._LYRIC_DOT if i == lit else QColor(TEXT_DIM_HEX))
                painter.drawRect(QRectF(cx - 10 + i * 8, cy - 2 - round(bounce), 4, 4))
        painter.restore()

    def _paint_hint(self, painter: QPainter) -> None:
        """Hint card in the HUD style: the suggestion plus a dim how-to line."""
        r = self._hint_rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_pixel_panel(painter, r)
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 5, r.width() - 16, 16), center, self._hint.text)
        painter.setFont(ui_font(6.5))
        painter.setPen(QColor(TEXT_DIM_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 22, r.width() - 16, 12), center,
                         "Click: do it   Right-click: mute")
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

        painter.save()
        painter.setOpacity(alpha / 255)
        paint_pixel_panel(painter, pill)
        painter.setPen(pet_accent(self._toast_color))
        toast_font = QFont("Consolas", 8, QFont.Weight.Bold)
        painter.setFont(toast_font)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self._toast_text)
        painter.restore()

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
        self._lyric_timer.stop()
        self._hint_timer.stop()
        if self._ai_chat is not None:
            self._ai_chat.close()
            self._ai_chat = None
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
