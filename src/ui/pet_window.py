"""Frameless translucent floating desktop window for Paperduck."""

from __future__ import annotations

import ctypes
import logging
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

from PyQt6.QtCore import QDateTime, QElapsedTimer, QPoint, QPointF, QProcess, QRect, QRectF, QStandardPaths, Qt, QTimer, QUrl
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
from src.graphics.duck import render_icon, render_pose
from src.tools.file_forge import ForgeMode, ForgeWheelOption, get_wheel_options_for_paths
from src.tools.ai_assist import AiAssistPlugin, AiJob, AiReply, fit_thinking, load_key, thinking_levels
from src.tools.ai_tools import skills_dir
from src.tools.errands import MAX_ERRAND_RUNS, ErrandsPlugin
from src.tools.macros import MacroBook
from src.tools.hints import HintsPlugin, Suggestion, foreground_app, foreground_title, idle_seconds, display_kept_on
from src.tools.watch import (ACTION_LABELS, EDITORS, LABELS as WATCH_LABELS, SOURCES as WATCH_SOURCES, WatchReply,
                              action_lenses, apply_moves, ask_context, editor_title_parts, editor_workspace,
                              folder_check, hotkey_lens, run_lens, undo_moves)
from src.tools import computer
from src.ui.ai_chat import AiChat
from src.ui.ai_tool_host import AiToolHost, grab_screen, show_quietly
from src.ui.screen_glow import ScreenGlow, play_screen_glow
from src.storage.recent_files import RecentFiles
from src.storage.storage_manager import log_text
from src.tools.web_drop import WebImageJob, html_image_src, is_web_image_source, strip_metadata, web_image_stem
from src.ui.context_menu import PetContextMenu, style_menu
from src.ui.drop_wheel import RadialDropWheel
from src.ui.macro_editor import MacroEditor
from src.ui.macro_runner import MacroRunner
from src.ui import perch
from src.ui.quick_tools import (HOTKEY_ACTIONS, OcrJob, SnipSession, default_hotkey, hotkey_label, open_folder_in_background,
                                 parse_hotkey)
from src.ui.sfx import play_cue, preload_tick_sounds, prewarm as prewarm_ticks
from src.ui.tool_wheel import WEB_SHORTCUTS, ToolWheel, ToolWheelOption, WheelLayout, google_search_url
from src.updater import APP_DIR, FROZEN, FROZEN_EXE, RELEASES_URL, UpdateJob, is_installed
from src.ui.theme import (DANGER_HEX, HAIRLINE_RGBA, HOVER_RGBA, SLEEP_HEX, TEXT_DIM_HEX, TEXT_HEX,
                          THEME_CHOICES, paint_panel, pet_accent, qcolor, save_theme, saved_theme, ui_font)


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


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def fullscreen_in_front(own: set[int], pet_hwnd: int) -> bool:
    """True while someone else's window fills the pet's whole monitor and has focus (a borderless game, a video,
    a slideshow). Those are rarely topmost, so yield_topmost misses them and the pet popped over them every tick."""
    fg = computer.foreground_window()
    if not fg or fg in own or computer.window_class(fg) in _SHELL_CLASSES:  # the desktop is monitor-sized too
        return False
    front, pet = computer.window_on_monitor(fg), computer.window_on_monitor(pet_hwnd)
    return front is not None and pet is not None and front[0] == pet[0] and front[1] == (0, 0, *front[2])


# What the duck acts out while the chat model runs a tool (body language); anything else reads as thinking
READING_TOOLS = frozenset({"list_files", "read_file", "find_files", "search_text", "read_clipboard", "read_screen",
                           "screen_map", "load_skill", "read_background", "pet_status"})
WORKING_TOOLS = frozenset({"run_command", "run_powershell", "start_background", "edit_file", "edit_files", "mouse_click", "type_text",
                           "press_key", "scroll", "computer_batch", "save_file"})
LOOKING_TOOLS = frozenset({"web_search", "fetch_page", "open_link", "find_mcp_servers"})


def activity_clip(tool: str) -> str:
    """The base clip for what the chat model does now: reading, typing, looking around, or waiting on the user."""
    if tool == "approval":
        return "wait"
    if tool in WORKING_TOOLS:
        return "type"
    if tool in LOOKING_TOOLS or "__" in tool:  # web tools and MCP servers (server__tool)
        return "scan"
    return "focus"  # reading tools and plain thinking: head over its work


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


def build_app_icon(size: int = 64) -> QIcon:
    """App and tray icon: the resting duck filling the icon square."""
    return QIcon(QPixmap.fromImage(render_icon(size)))


def export_windows_ico(
    output_path: Path | str,
    sizes: Sequence[int] = (16, 32, 48, 64, 128, 256),
) -> Path:
    """Write a multi-resolution Windows .ico file, each size rendered sharp from the vector duck."""
    from PyQt6.QtCore import QBuffer, QIODevice

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    png_blobs: list[tuple[int, bytes]] = []
    for sz in sizes:
        scaled = render_icon(sz)
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
    """Borderless translucent desktop widget rendering Paperduck and hover HUD."""

    WINDOW_WIDTH: int = 176
    WINDOW_HEIGHT: int = 222
    SPRITE_SCALE: int = 4  # the largest size, 32x32 * 4 = 128x128; WINDOW_HEIGHT is laid out for it
    PET_SCALES: dict[int, str] = {2: "Small", 3: "Medium", 4: "Large"}  # whole numbers keep the pixels crisp
    DEFAULT_PET_SCALE: int = 2  # Small until the user picks another size (a saved size wins)
    DRAG_THRESHOLD_PX: int = 5
    HOLD_TO_WHEEL_MS: int = 450
    WHEEL_RELEASE_GRACE_MS: int = 90
    # First-run tips on the hint card: (title, how-to line), one at a time, TIP_GAP_S apart
    TIPS: tuple[tuple[str, str], ...] = (
        ("Hold me for tools", "Press, wait, slide, release"),
        ("Drop files on me", "Convert, zip or extract them"),
        ("Right-click for more", "Settings, macros, AI chat"),
    )
    TIP_FIRST_S: float = 8.0
    TIP_GAP_S: float = 30.0
    SUCCESS_HEX: str = "#38B764"  # the green every tool uses for a finished job
    QUIET_NOTICES: frozenset[str] = frozenset({"COMMITTED", "PUSHED"})  # toast and react, no tray balloon
    BIG_WINS: frozenset[str] = frozenset({"FOCUS DONE", "PUSHED"})  # cheer plus the pixel-star burst
    LONG_TASK_S: float = 30.0  # an AI answer that took this long cheers when it lands
    AWAY_AFTER_S: float = 300.0  # no keyboard or mouse input this long: the idle clip becomes a nap
    PERCH_TICK_MS: int = 16  # perch follow and fall frame time (about 60 fps)
    PERCH_COVER_EVERY: int = 6  # perch ticks between checks that no other window covers the duck's footing
    FALL_ACCEL: float = 2400.0  # px/s^2: a flutter, a bit gentler than a drop
    PREWARM_DELAY_MS: int = 150  # a drag has usually passed DRAG_THRESHOLD_PX by now; still before the wheel opens

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

        self.pet_scale = self.DEFAULT_PET_SCALE  # Settings > Pet Size; persisted by PaperduckApp
        self._layout_sprite()
        self._hud_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 72)

        # Rendered frames keyed by (clip_name, frame_index, facing_left, device px)
        self._image_cache: dict[tuple[str, int, bool, int], QImage] = {}

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

        # Gentle autonomous walk steps
        self._walk_dx: int = 0
        self._walk_steps_remaining: int = 0
        self._perch: tuple[int, int] | None = None  # (hwnd, sprite centre x minus the edge's left) while sitting
        self._fall_to: int | None = None  # window y a perch fall ends at
        self._perch_ticks = 0
        self._fall_x, self._fall_y, self._fall_v = 0, 0.0, 0.0  # x, exact y, speed in px/s
        # Perch follow and falls run at display rate (the 90ms overlay tick looked choppy); only while needed
        self._perch_timer = QTimer(self)
        self._perch_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._perch_timer.setInterval(self.PERCH_TICK_MS)
        self._perch_timer.timeout.connect(self._on_perch_tick)
        self._perch_clock = QElapsedTimer()
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
        # A release that ends a wheel hold waits this long: a press inside it (a quick re-press, or a worn switch
        # bouncing) keeps the same hold, instead of closing the wheel and opening a second one plus a double-click
        self._wheel_release_timer = QTimer(self)
        self._wheel_release_timer.setSingleShot(True)
        self._wheel_release_timer.setInterval(self.WHEEL_RELEASE_GRACE_MS)
        self._wheel_release_timer.timeout.connect(self._finish_wheel_release)
        self._wheel_release_pos = QPoint()
        self._ocr_jobs: list[OcrJob] = []
        self._ai_chat: AiChat | None = None  # built hidden soon after launch (_prebuild_ai_chat) or on first open
        QTimer.singleShot(self.PREBUILD_CHAT_MS, self._prebuild_ai_chat)
        self._ai_thinking: bool = False
        self._ai_since: float = 0.0  # when the pending AI answer started
        self._ai_activity = ""  # the tool the chat model runs now (body language), "approval" while one waits
        self._errand_running = False  # the chat's pending answer is a fired errand
        self._away: bool = False  # no input for AWAY_AFTER_S: idle naps (visual only, never DND)
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
        self._anim_clock = QElapsedTimer()  # real time since the last tick: 45ms shine frames ran on a 60ms floor
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
        self.hotkeys: dict[str, str] = {}  # mode -> combo text overriding a default; persisted by PaperduckApp
        self.on_hotkeys_changed: Callable[[], None] | None = None  # set by PaperduckApp: re-register them
        self.tips_seen: int = 0  # persisted by PaperduckApp; a tip counts as seen once shown
        self._tip_due: float = time.monotonic() + self.TIP_FIRST_S
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
        self.setWindowTitle("Paperduck")
        self.setWindowIcon(build_app_icon(64))

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
            show_quietly(self)

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
        set_pos = ctypes.windll.user32.SetWindowPos
        set_pos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        if fullscreen_in_front(own, int(self.winId())):
            # Skipping the re-raise is not enough: a topmost pet stays above a normal fullscreen window. Placing it
            # right after that window drops its topmost bit, so it sits behind until the fullscreen app goes.
            fg = computer.foreground_window()
            for w in (self, self._ai_chat):
                if w is not None and w.isVisible() and computer.is_topmost(int(w.winId())):
                    set_pos(int(w.winId()), fg, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            return
        if yield_topmost(own):  # the next tick tries again
            return
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE: re-raise without stealing focus
        raised = False
        pet = int(self.winId())
        if not wheels and (not computer.is_topmost(pet) or computer.under_foreign_topmost(pet)):  # else no churn
            set_pos(int(self.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            raised = True
        for wheel in wheels:
            wheel.restack_topmost()  # wheels stay above the pet but below an in-flight drag image
        chat = self._ai_chat
        if chat is not None and chat.isVisible() and (raised or not computer.is_topmost(int(chat.winId()))
                                                      or computer.under_foreign_topmost(int(chat.winId()))):
            set_pos(int(chat.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # the pet never covers its chat

    def set_always_on_top(self, enabled: bool) -> None:
        if self._always_on_top == bool(enabled):
            return
        self._always_on_top = bool(enabled)
        self._apply_window_flags(re_show=True)
        for wheel in (self._drop_wheel, self._tool_wheel):
            if wheel is not None:
                was_open = wheel.isVisible()
                wheel._always_on_top = self._always_on_top
                wheel._configure_window()  # setWindowFlags hides an open wheel
                if was_open:
                    show_quietly(wheel)

    def set_auto_wander(self, enabled: bool) -> None:
        self._auto_wander = bool(enabled)
        if not self._auto_wander and self._walk_steps_remaining > 0:
            self._walk_steps_remaining = 0
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()
            self.update()

    def _layout_sprite(self) -> None:
        """Size the window to the sprite: same width (HUD and cards keep their size), shorter for smaller pets."""
        size = 32 * self.pet_scale
        height = self.WINDOW_HEIGHT - 32 * self.SPRITE_SCALE + size
        self._sprite_size_px = size
        self._sprite_rect = QRect((self.WINDOW_WIDTH - size) // 2, height - size - 6, size, size)
        self.setFixedSize(self.WINDOW_WIDTH, height)

    def _edit_hotkey(self, mode: str) -> None:
        if mode not in HOTKEY_ACTIONS:
            return
        current = self.hotkeys.get(mode) or default_hotkey(mode)
        text, ok = QInputDialog.getText(
            self, f"Paperduck - {HOTKEY_ACTIONS[mode]} Hotkey",
            f"Type a combo like Ctrl+Alt+K (A-Z, 0-9, F1-F24).\nEmpty resets to {default_hotkey(mode)}.", text=current)
        if not ok:
            return
        if not text.strip():
            self.hotkeys.pop(mode, None)
        else:
            try:
                label = hotkey_label(*parse_hotkey(text))
            except ValueError as exc:
                self._show_toast("BAD KEYS", QColor("#E05A47"))
                if self._on_notification is not None:
                    self._on_notification(f"Hotkey not changed: {exc}")
                return
            if label in (self.hotkeys.get(m) or default_hotkey(m) for m in HOTKEY_ACTIONS if m != mode):
                self._show_toast("IN USE", QColor("#E05A47"))
                return
            self.hotkeys[mode] = label
        self._show_toast("HOTKEY SET", QColor("#38B764"))
        if self.on_hotkeys_changed is not None:
            self.on_hotkeys_changed()

    def set_pet_scale(self, scale: int) -> None:
        if scale not in self.PET_SCALES or scale == self.pet_scale:
            return
        old_height = self.height()
        self.pet_scale = scale
        self._layout_sprite()
        # Keep the pet standing on the same spot: the window grows or shrinks upward
        self.move_clamped(self.x(), self.y() + old_height - self.height())
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
        self._perch = self._fall_to = None
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
        if self._did_drag and self._press_global_pos is not None:
            return "dangle"  # held in the air, whatever the state
        clip = mapping.get(state, "idle")
        if self._ai_thinking and clip == "idle":
            return activity_clip(self._ai_activity)
        return "sleep" if clip == "idle" and self._away else clip

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
        clip = self._base_clip(self._state_machine.state)
        if self._walk_steps_remaining == 0 and self._anim.base_state != clip:
            self._anim.set_state(clip)  # a song started or stopped
            self._schedule_next_anim_tick()
            self.update()
        notifications = self._state_machine.pop_notifications()
        if notifications:
            toast_text, color_hex = notifications[-1]
            # FOCUS DONE is ice-blue, but a finished focus session is the biggest win there is
            self._react(self.SUCCESS_HEX if toast_text == "FOCUS DONE" else color_hex or self.SUCCESS_HEX,
                        big=toast_text in self.BIG_WINS)
            self._show_toast(toast_text, QColor(color_hex or "#38B764"))
            if self._on_notification is not None and toast_text not in self.QUIET_NOTICES:
                self._on_notification(toast_text)
            return
        if self._is_hud_visible():
            self.update()

    def _schedule_next_anim_tick(self) -> None:
        duration = max(60, self._anim.current_frame_duration_ms())
        self._anim_timer.start(duration)
        self._anim_clock.start()

    def _on_animation_tick(self) -> None:
        # measured, capped so a stall (sleep, a busy Qt thread) skips at most a second of frames
        elapsed = min(1000, self._anim_clock.elapsed()) if self._anim_clock.isValid() else self._anim_timer.interval()
        changed = self._anim.advance(elapsed)
        self._schedule_next_anim_tick()
        if changed:
            self.update()

    def _on_ambient_tick(self) -> None:
        if self._press_global_pos is not None:
            return
        if self._open_wheels() or self._walk_steps_remaining > 0 or self._fall_to is not None:
            return  # a hop or blink must not move the pet out from under an open wheel, or interrupt a walk
        if self._state_machine.state != PetState.IDLE or self._away:
            return

        roll = random.random()
        if roll < 0.08 and self._anim.base_state == "idle":  # no yawning mid-song
            self._anim.play_oneshot("yawn")
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.18:
            self._anim.play_oneshot("shine")  # a glint runs along the wire
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
            if self._perch is not None:
                moved_on = self._follow_perch(self._walk_dx)  # a hop along the edge; False: a fall replaced it
            else:
                moved_on = True
                self.move_clamped(before.x() + self._walk_dx, before.y())
            if moved_on and (self.pos() == before or self._walk_steps_remaining == 0):
                self._walk_steps_remaining = 0
                self._sync_animation_to_pet_state(self._state_machine.state)  # a walk may run in any state
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

    # ------------------------------------------------------------------
    # Perching on other windows (src/ui/perch.py)
    # ------------------------------------------------------------------

    def _feet_local(self) -> tuple[int, int]:
        """(sprite centre x, feet y) in window coordinates."""
        r = self._sprite_rect
        return r.center().x(), r.top() + r.height() * 53 // 64  # lowest opaque row of the standing poses

    def _clamp_feet(self, x: int, y: int) -> tuple[int, int]:
        """clamp_position_to_screens, but the window may hang below the work area by the gap under the feet, so
        they touch the taskbar instead of hovering over it."""
        gap = self.height() - self._feet_local()[1]
        screens = [ScreenRect(s.x, s.y, s.width, s.height + gap) for s in self.available_screen_rects()]
        return clamp_position_to_screens(x, y, self.width(), self.height(), screens)

    @staticmethod
    def _qt_screens() -> list[tuple[perch.Rect, float]]:
        out = []
        for scr in QGuiApplication.screens():
            g = scr.geometry()
            out.append(((g.x(), g.y(), g.x() + g.width(), g.y() + g.height()), scr.devicePixelRatio()))
        return out

    def _try_perch(self) -> None:
        """On release: sit on the window (or taskbar) whose top edge is within reach of the feet."""
        if QGuiApplication.platformName() != "windows":
            return  # offscreen tests: never perch on the real desktop's windows
        hwnds, rects = self._perch_candidates()
        sx, fy = self._feet_local()
        i = perch.pick_perch(rects, self.x() + sx, self.y() + fy)
        if i is None:
            return
        self._perch = (hwnds[i], self.x() + sx - rects[i][0])
        self._perch_ticks = 0
        if self._follow_perch(fall=False):
            self._start_perch_timer()

    def _perch_candidates(self) -> tuple[list[int], list[perch.Rect]]:
        """Windows the duck could sit on, topmost first, in logical px."""
        screens = self._qt_screens()
        wins = perch.top_level_windows()
        return [h for h, _ in wins], [perch.to_logical(r, screens) for _, r in wins]

    def _check_perch_cover(self) -> None:
        """Another window came up over the spot the duck sits on (alt-tab, a click on another app): hop onto that
        window's top edge when it is right at the feet, else fall to the floor."""
        assert self._perch is not None
        hwnds, rects = self._perch_candidates()
        sx, fy = self._feet_local()
        fx = self.x() + sx
        index = hwnds.index(self._perch[0]) if self._perch[0] in hwnds else None
        if not perch.perch_covered(rects, index, fx):
            return
        j = perch.pick_perch(rects, fx, self.y() + fy)
        if j is not None:
            self._perch = (hwnds[j], fx - rects[j][0])
            self._follow_perch()  # falls itself when there is no room up there
            return
        self._perch = None
        self._start_fall()

    def _start_perch_timer(self) -> None:
        if not self._perch_timer.isActive():
            self._perch_clock.start()
            self._perch_timer.start()

    def _on_perch_tick(self) -> None:
        dt = min(0.1, self._perch_clock.restart() / 1000.0)  # measured; a stall never teleports the fall
        if self._fall_to is not None:
            self._fall_v += self.FALL_ACCEL * dt
            self._fall_y = min(float(self._fall_to), self._fall_y + self._fall_v * dt)
            self.move(self._fall_x, round(self._fall_y))
            if self.y() >= self._fall_to:
                self._land_fall()
        elif self._perch is not None:
            self._perch_ticks += 1
            if self._perch_ticks % self.PERCH_COVER_EVERY == 0:
                self._check_perch_cover()  # a full window scan (~0.7ms), so not every frame
            if self._perch is not None and self._walk_steps_remaining <= 0:
                self._follow_perch()  # a hop steps along the edge on the overlay tick instead
        if self._perch is None and self._fall_to is None:
            self._perch_timer.stop()

    def _follow_perch(self, dx: int = 0, *, fall: bool = True) -> bool:
        """Stand on the perch's top edge (dx: a wander step along it). False when it is gone, then fall
        (unless `fall` is off: a drop where there is no room to sit stays where it was dropped)."""
        assert self._perch is not None
        hwnd, offset = self._perch
        screens = self._qt_screens()
        phys = perch.window_rect(hwnd)
        rect = perch.to_logical(phys, screens) if phys is not None else None
        sx, fy = self._feet_local()
        lost = rect is None or perch.perch_lost(rect, [g for g, _ in screens], rect[0] + offset)
        if rect is not None and not lost:
            x, y = perch.perch_position(rect, rect[0] + offset + dx, sx, fy, self._sprite_rect.width() // 4)
            cx, cy = self._clamp_feet(x, y)
            lost = cy != y  # pushed off the edge (window dragged to the screen top, maximized)
        if rect is None or lost:
            self._perch = None
            if fall:
                self._start_fall()
            return False
        if (cx, cy) != (self.x(), self.y()):
            self.move(cx, cy)
        self._perch = (hwnd, cx + sx - rect[0])
        return True

    def _start_fall(self) -> None:
        """Flutter down (dangle clip, accelerating) to the work-area floor under the duck."""
        sx, fy = self._feet_local()
        scr = QGuiApplication.screenAt(QPoint(self.x() + sx, self.y() + fy)) or self.screen()
        floor = scr.availableGeometry().bottom() + 1
        self._fall_x, fall_to = self._clamp_feet(self.x(), floor - fy)
        self._fall_y, self._fall_v = float(self.y()), 0.0
        self._walk_steps_remaining = 0
        if fall_to <= self.y():
            self.move(self._fall_x, fall_to)
            self._land_fall()
            return
        self._fall_to = fall_to
        self._anim.set_state("dangle")
        self._schedule_next_anim_tick()
        self._start_perch_timer()

    def _land_fall(self) -> None:
        self._fall_to = None
        self._sync_animation_to_pet_state(self._state_machine.state)
        self._anim.play_oneshot("land")
        self._schedule_next_anim_tick()
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

    def _check_away(self) -> None:
        away = idle_seconds() >= self.AWAY_AFTER_S and not display_kept_on()  # watching a video is not away
        if away == self._away:
            return
        self._away = away
        self._sync_animation_to_pet_state(self._state_machine.state)
        if not away and self._anim.base_state == "idle":
            self._anim.play_oneshot("yawn")  # waking up as the user comes back
        self._schedule_next_anim_tick()
        self.update()

    def _poll_hint(self) -> None:
        self._check_away()
        self._poll_errands()
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
        if (self._state_machine.state == PetState.SLEEPING or self._open_wheels()
                or self._press_global_pos is not None or self._snip_session is not None):
            return
        if self.tips_seen < len(self.TIPS) and time.monotonic() >= self._tip_due:
            text, how = self.TIPS[self.tips_seen]
            self.tips_seen += 1
            self._hint, self._hint_until = Suggestion("tip", text, "", how), time.monotonic() + 12.0
            self._tip_due = self._hint_until + self.TIP_GAP_S
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            self.update()
            return
        hints = self._hints_plugin()
        if hints is None or not hints.enabled:
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
        if hint is None or hint.rule == "tip":  # a tip's click just closes it
            return
        if hint.rule.startswith("errand"):
            if hint.rule == "errand_ready":
                self._run_errand()
            self.ensure_ai_chat()
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
        if self._watch_open and self._ai_chat is None:
            self._make_ai_chat(ai)  # the host's approval cards live in the chat
        # Tools only after a click: a pre-fetch must never pop approvals the user did not ask for
        host = self._ai_chat._tool_host if self._watch_open and self._ai_chat is not None else None
        project = str(getattr(self._state_machine.registry.get_plugin("project"), "repo", None) or "")

        def run() -> AiReply:
            text = AiToolHost._ocr(image) if image is not None else data
            reply = run_lens(lens, text, url, key, model, thinking=thinking, host=host, project=project)
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
        if hint is not None and hint.rule.startswith("errand"):
            return  # closes the card; an errand_ready one shows again on the next tick
        if hint is not None and hint.rule == "tip":
            self.tips_seen = len(self.TIPS)
            self._show_toast("TIPS OFF", QColor("#8A9AF2"))
            return
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
        for out_dir in (Path(downloads) / "Paperduck" if downloads else None, Path.home() / ".paperduck" / "drops"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                return out_dir
            except OSError:
                continue
        return Path.home() / ".paperduck" / "drops"

    def check_for_updates(self, *, install: bool = True) -> None:
        """Ask GitHub for a newer release; `install` also applies it (installed copies only)."""
        job = UpdateJob()
        job.finished.connect(lambda status, tag, j=job, i=install: self._on_update_done(j, status, tag, i))
        if install:  # a slow mirror takes minutes for the 38 MB standalone bundle: show it moving
            job.progress.connect(lambda pct: self._show_toast(f"GET {pct}%", QColor("#8A9AF2")))
        self._update_jobs.append(job)
        job.start(install=install)
        if install:
            self._show_toast("CHECKING", QColor("#8A9AF2"))

    def _on_update_done(self, job: UpdateJob | None, status: str, tag: str, install: bool) -> None:
        if job in self._update_jobs:
            self._update_jobs.remove(job)
        if status == "installed":
            # A frozen build is only staged: the swap after restart can still fail, and the old build then says so
            self._show_toast("RESTARTING" if FROZEN else "UPDATED", QColor("#38B764"))
            if self._on_notification is not None:
                self._on_notification(f"{'Installing' if FROZEN else 'Updated to'} {tag}, restarting")
            QTimer.singleShot(1500, self.restart)
        elif status == "ready":  # the startup check already downloaded and verified it
            self._show_toast(f"NEW {tag}", QColor("#FFCD75"))
            if self._on_notification is not None:
                self._on_notification(f"Paperduck {tag} is downloaded: Settings > Check for Updates installs it")
        elif status == "available":
            self._show_toast(f"NEW {tag}", QColor("#FFCD75"))
            if self._on_notification is not None:
                self._on_notification(f"Paperduck {tag} is available: Settings > Check for Updates")
            if install and not is_installed():
                QDesktopServices.openUrl(QUrl(RELEASES_URL))  # git checkout: update by hand
        elif install:  # the silent startup check never toasts "up to date" or errors
            # On error `tag` carries the reason; signature failures mean the release was refused
            refused = {"bad signature": "BAD SIG", "unsigned release": "UNSIGNED",
                       "version mismatch": "BAD VERSION"}.get(tag)
            self._show_toast("UP TO DATE" if status == "current" else refused or "UPDATE ERR",
                             QColor("#38B764" if status == "current" else "#E05A47"))
            if refused and self._on_notification is not None:
                self._on_notification(f"Update refused ({tag}): it is not a release signed by the Paperduck key")
            elif status == "error" and tag != "needs installer" and self._on_notification is not None:
                self._on_notification(f"Update failed: {tag}")  # the reason, also in the log (Settings > Copy Log)
            if tag == "needs installer":
                self._show_toast("RUN INSTALLER", QColor("#FFCD75"))
                if self._on_notification is not None:
                    self._on_notification("This update changes libraries: run install.ps1 from the releases page")
                QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def restart(self) -> None:
        self.trigger_quit()  # saves state.json before the new copy loads it
        staged = APP_DIR.with_name(APP_DIR.name + ".new") / FROZEN_EXE
        if FROZEN and staged.is_file():  # the staged build swaps itself in once this process has exited
            QProcess.startDetached(str(staged), ["--finish-update", str(os.getpid())], str(APP_DIR.parent))
        elif FROZEN:
            QProcess.startDetached(sys.executable, [], str(APP_DIR.parent))
        else:
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
        return Path(pictures) / "Paperduck" if pictures else Path.home() / ".paperduck" / "snips"

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
            label, ok = QInputDialog.getText(self, "Paperduck - Add Website", "Slice name:")
            url, ok2 = (QInputDialog.getText(self, "Paperduck - Add Website", "URL:", text="https://")
                        if ok and label.strip() else ("", False))
            self._wheel_toast(self.wheel.add_custom(label, url) if ok2 else "CANCEL")
        elif key == "add_app":
            path, _flt = QFileDialog.getOpenFileName(self, "Paperduck - Add App or File to Wheel")
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
        # Hide Paperduck and let the context menu fade so neither lands in the frozen grab
        self.hide()
        QTimer.singleShot(180, lambda: self._begin_snip(mode))

    def _begin_snip(self, mode: str) -> None:
        if self._is_shutting_down:
            return
        if self._snip_session is not None:  # a macro's region snip opened during the 180ms hide delay
            show_quietly(self)
            return
        self._snip_session = SnipSession(mode, self._on_snip_result)

    def _on_snip_result(self, mode: str, result: object | None) -> None:
        self._snip_session = None
        show_quietly(self)
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
        """Save to Pictures/Paperduck, else ~/.paperduck/snips; never raise into Qt handlers."""
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss_zzz")
        # OneDrive-redirected or Controlled Folder Access Pictures dirs can refuse writes
        for out_dir in (Path(pictures) / "Paperduck" if pictures else None, Path.home() / ".paperduck" / "snips"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            target = out_dir / f"snip_{stamp}.png"
            if strip_metadata(image).save(str(target), "PNG"):
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
        started = time.perf_counter()
        if self._ai_chat is None:
            self._make_ai_chat(plugin)
        elif self._ai_chat.isVisible():
            self._ai_chat.hide()
            return
        sprite = self._sprite_global_rect()
        play_cue("open")
        self._ai_chat.open_near(sprite)
        # the effects start after the card's layout pass, so their first frames are not frozen by it
        glow = play_screen_glow(self.screen(), sprite.center())
        if glow is not None:
            self._glows.append(glow)
            glow.destroyed.connect(lambda *_a, g=glow: self._glows.remove(g) if g in self._glows else None)
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        took = (time.perf_counter() - started) * 1000
        if took > 150:
            logging.getLogger(__name__).info("ai chat open took %.0f ms", took)

    PREBUILD_CHAT_MS = 4000  # after launch settles: the first open then skips about 0.65s of widget setup and first show

    def _prebuild_ai_chat(self) -> None:
        plugin = self._state_machine.registry.get_plugin("ai")
        if self._ai_chat is None and not self._is_shutting_down and isinstance(plugin, AiAssistPlugin):
            self._make_ai_chat(plugin)
            self._ai_chat.warm_up()

    def _make_ai_chat(self, plugin: AiAssistPlugin) -> None:
        """Build the chat card hidden (about 0.4s of widget setup on the Qt thread)."""
        host = AiToolHost(self)
        self._ai_chat = AiChat(plugin, on_attach_screen=lambda: self.trigger_snip("ask"), tool_host=host)
        host.activity.connect(self._on_ai_activity)
        if (errands := self._errands()) is not None:
            errands.probes.bg = host.procs.peek
        self._ai_chat.thinking_changed.connect(self._on_ai_thinking)
        self._ai_chat.answered.connect(self._on_ai_answered)

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

    def _react(self, color_hex: str, *, big: bool = False) -> None:
        """One-shot reaction to a notice by its colour: green success cheers, coral failure flinches (in any
        state), anything else (aborted, info) spins while idle. A `big` win also bursts pixel stars round the pet."""
        if big and self.isVisible():
            play_screen_glow(self.screen(), self._sprite_global_rect().center())
        if QColor(color_hex) == QColor(self.SUCCESS_HEX):
            clip = "cheer"
        elif pet_accent(color_hex) == QColor(DANGER_HEX):
            clip = "flinch"
        elif self._state_machine.state == PetState.IDLE:
            clip = "spin"
        else:
            return
        self._anim.play_oneshot(clip)
        self._schedule_next_anim_tick()

    def _on_ai_activity(self, name: str) -> None:
        """Act out what the chat model does: a new base clip per tool, and facing the chat while it waits."""
        self._ai_activity = name
        if name == "approval" and self._ai_chat is not None and self._ai_chat.isVisible():
            self._anim.facing_left = self._ai_chat.geometry().center().x() < self._sprite_global_rect().center().x()
        if not self._is_shutting_down and self._walk_steps_remaining == 0:
            clip = self._base_clip(self._state_machine.state)
            if self._anim.base_state != clip:
                self._anim.set_state(clip)
                self._schedule_next_anim_tick()
                self.update()

    # Errands: the plugin checks triggers on the hint tick; a fired one runs in the chat that set it

    def _errands(self) -> ErrandsPlugin | None:
        plugin = self._state_machine.registry.get_plugin("errands")
        return plugin if isinstance(plugin, ErrandsPlugin) else None

    def _poll_errands(self) -> None:
        errands = self._errands()
        if errands is None:
            return
        if errands.poll():
            self.update()  # the tag count
        if not errands.fired or self._is_shutting_down:
            return
        if self._ai_chat is None:
            self._prebuild_ai_chat()  # a next_startup errand can fire before the chat is built
        chat = self._ai_chat
        if chat is None or chat._job is not None or (self._hint is not None and self._hint.rule == "errand_ready"):
            return
        errand, _evidence = errands.fired[0]
        procs = getattr(chat._tool_host, "procs", None)
        if chat._sid != errand.sid and procs is not None and procs.snapshot():
            # switching chats would kill this chat's background commands: the user decides
            self._show_errand_hint("errand_ready", "Errand ready. Click to run it", errand.instruction)
            return
        self._run_errand()

    def _run_errand(self) -> None:
        errands, chat = self._errands(), self._ai_chat
        if errands is None or chat is None or not errands.fired or chat._job is not None:
            return
        errand, evidence = errands.fired.pop(0)
        if not errands.take_run():
            chat.add_note(f"Errand {errand.id} fired, but today's {MAX_ERRAND_RUNS} errand runs are used up.")
            self._show_toast("ERRAND CAP", QColor("#EF7D57"))
            return
        if chat.run_errand(errand, evidence):
            self._errand_running = True
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            if not chat.isVisible():
                self._show_errand_hint("errand_busy", "Errand: working on it", errand.instruction)
            self.update()

    def _show_errand_hint(self, rule: str, text: str, detail: str) -> None:
        self._drop_watch()
        self._hint = Suggestion(rule, text, "", detail[:60])
        self._hint_until = time.monotonic() + (20.0 if rule == "errand_done" else 3600.0)
        self.update()

    def _on_ai_answered(self, ok: bool) -> None:
        if self._errand_running:
            self._errand_running = False
            if (errands := self._errands()) is not None and ok:
                errands.done += 1
            if self._hint is not None and self._hint.rule == "errand_busy":
                self._clear_hint()
            if self._ai_chat is not None and not self._ai_chat.isVisible():
                self._show_errand_hint("errand_done", "Errand done. Click to see" if ok else "Errand failed. Click",
                                   "Right-click closes")
            self._react(self.SUCCESS_HEX if ok else "#EF7D57", big=ok)
            return
        # A quick answer is just a chat; only a long job (an agentic task) earns a cheer
        if not ok:
            self._react("#EF7D57")
        elif time.monotonic() - self._ai_since >= self.LONG_TASK_S:
            self._react(self.SUCCESS_HEX, big=True)

    def _on_ai_thinking(self, thinking: bool) -> None:
        if thinking and not self._ai_thinking:
            self._ai_since = time.monotonic()
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
        if callback_id.startswith("hotkey:"):
            self._edit_hotkey(callback_id.removeprefix("hotkey:"))
            return
        if callback_id.startswith("size:"):
            self.set_pet_scale(int(callback_id[5:]) if callback_id[5:].isdigit() else 0)
            return
        if callback_id.startswith("theme:"):
            choice = callback_id.removeprefix("theme:")
            if choice in THEME_CHOICES and choice != saved_theme():
                save_theme(choice)
                self.restart()  # every surface takes its colours at import, so a new scheme needs a fresh start
            return
        if callback_id.startswith("snip:"):
            self.trigger_snip(callback_id.split(":", 1)[1])
            return
        if callback_id == "forge:pick_files":
            selected, _flt = QFileDialog.getOpenFileNames(
                self,
                "Paperduck File Forge - Select File(s)",
            )
            if selected:
                self.trigger_drop_files(selected)
            return
        if callback_id == "forge:pick_folder":
            folder = QFileDialog.getExistingDirectory(
                self,
                "Paperduck File Forge - Select Folder to Compress",
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
            folder = QFileDialog.getExistingDirectory(self, "Paperduck - Choose Project Folder")
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

        # Rendered alpha decides; a 3x3 probe a few px apart forgives thin parts like the beak tip
        image = self._current_qimage()
        k = image.width() / max(1, self._sprite_rect.width())
        lx, ly = local_pos.x() - self._sprite_rect.left(), local_pos.y() - self._sprite_rect.top()
        for oy in (-3, 0, 3):
            for ox in (-3, 0, 3):
                ix, iy = int((lx + ox) * k), int((ly + oy) * k)
                if 0 <= ix < image.width() and 0 <= iy < image.height() and image.pixelColor(ix, iy).alpha() > 0:
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
        if event.button() == Qt.MouseButton.LeftButton and self._wheel_release_timer.isActive():
            self._wheel_release_timer.stop()  # pressed again right away: the same hold goes on
            event.accept()
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
            self._perch = self._fall_to = None  # picked up
            self._wheel_holding = False  # a hold whose release never arrived (wheel watchdog closed it)
            self._walk_steps_remaining = 0
            self._hold_timer.start()
            # Resume audio during the hold delay, not as the tool wheel appears. Waits PREWARM_DELAY_MS and skips
            # drags (they stop the hold timer): the blocking ~175ms resume froze the start of every drag.
            QTimer.singleShot(self.PREWARM_DELAY_MS, lambda: self._hold_timer.isActive() and prewarm_ticks())
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
            if not self._did_drag and delta.manhattanLength() >= self.DRAG_THRESHOLD_PX:
                self._did_drag = True
                self._hold_timer.stop()
                self._sync_animation_to_pet_state(self._state_machine.state)  # picks up "dangle"
                self._schedule_next_anim_tick()
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
                self._wheel_release_pos = event.globalPosition().toPoint()
                self._wheel_release_timer.start()
                event.accept()
                return
            was_drag = self._did_drag
            self._press_global_pos = None
            self._last_drag_global_pos = None
            self._drag_offset = None
            self._did_drag = False
            if not was_drag:
                self._click_timer.start()
            else:
                self._sync_animation_to_pet_state(self._state_machine.state)
                self._anim.play_oneshot("land")
                self._schedule_next_anim_tick()
                self._try_perch()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _finish_wheel_release(self) -> None:
        self._wheel_holding = False
        self._press_global_pos = None
        self._last_drag_global_pos = None
        self._drag_offset = None
        self._did_drag = False
        if self._tool_wheel is not None and self._tool_wheel.isVisible():
            self._tool_wheel.release_at(self._wheel_release_pos)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if self._wheel_holding:  # the second press of a bounce inside a wheel hold, not a double-click
            event.accept()
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
            pet_scale=self.pet_scale,
            hotkeys=[(label, self.hotkeys.get(mode) or default_hotkey(mode), mode)
                     for mode, label in HOTKEY_ACTIONS.items()],
            pet_scales=self.PET_SCALES,
            theme=saved_theme(),
            parent=self,
        )

    def _open_context_menu(self, global_pos: QPoint) -> None:
        menu = self.build_context_menu()
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    # ------------------------------------------------------------------
    # Painting (Vector Duck + HUD)
    # ------------------------------------------------------------------

    def _current_qimage(self) -> QImage:
        """The current pose rendered at the sprite's device pixel size (sharp at any pet size and DPI)."""
        dpr = self.devicePixelRatioF() or 1.0
        px = max(16, round(self._sprite_rect.width() * dpr))
        key = (self._anim.active_clip_name, self._anim.frame_index, self._anim.facing_left, px)
        cached = self._image_cache.get(key)
        if cached is None:
            if len(self._image_cache) >= 96:  # about 10 MB at 4x on a 125% display
                self._image_cache.clear()
            cached = render_pose(self._anim.current_frame(), px, mirrored=self._anim.facing_left)
            cached.setDevicePixelRatio(px / self._sprite_rect.width())
            self._image_cache[key] = cached
        return cached

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # Rendered at device pixels already, so this is a 1:1 blit
        painter.drawImage(self._sprite_rect.topLeft(), self._current_qimage())
        errands = self._errands()
        if errands is not None and errands.errands:
            self._paint_errand_tag(painter, len(errands.errands))

        if self._hint is not None:
            self._paint_hint(painter)
        elif self._is_hud_visible():
            self._paint_hud(painter)

        if self._toast_steps > 0 and self._toast_text:
            self._paint_toast(painter)

        painter.end()

    def _paint_hud(self, painter: QPainter) -> None:
        """Folded-paper card matching PetContextMenu: dog-eared panel, rounded bar tracks, Segoe UI text."""
        r = self._hud_rect
        hud = self._state_machine.get_hud_data()
        sleeping = self._state_machine.state == PetState.SLEEPING
        plugin = self._state_machine.active_plugin
        title = plugin.display_name if plugin is not None else hud.badge_left

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_panel(painter, r)

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
            # Rounded track with a filled run; any nonzero value shows at least a dot of fill
            track = QRectF(row.left() + 36, row.center().y() - 2, row.width() - 68, 4)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(HOVER_RGBA))
            painter.drawRoundedRect(track, 2, 2)
            if bar.clamped_value > 0:
                painter.setBrush(pet_accent(bar.color_hex))
                painter.drawRoundedRect(QRectF(track.left(), track.top(), max(4.0, track.width() * bar.clamped_value / 100.0), 4), 2, 2)
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

    def _paint_hint(self, painter: QPainter) -> None:
        """Hint card in the HUD style: the suggestion plus a dim how-to line."""
        r = self._hint_rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_panel(painter, r)
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 5, r.width() - 16, 16), center, self._hint.text)
        painter.setFont(ui_font(6.5))
        painter.setPen(QColor(TEXT_DIM_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 22, r.width() - 16, 12), center,
                         self._hint.data if self._hint.rule == "tip" or self._hint.rule.startswith("errand")
                         else "Click: do it   Right-click: mute")
        painter.restore()

    def _paint_errand_tag(self, painter: QPainter, count: int) -> None:
        """A small folded paper tag on a string at the duck's neck: errands it is carrying (the count)."""
        s = self._sprite_rect
        neck_x = s.left() + (s.width() * 0.36 if self._anim.facing_left else s.width() * 0.64)
        neck = QPointF(neck_x, s.top() + s.height() * 0.5)
        tag = QRectF(neck.x() - 8 + (-10 if self._anim.facing_left else 10), neck.y() + 6, 16, 13)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(TEXT_DIM_HEX), 1))
        painter.drawLine(neck, QPointF(tag.center().x(), tag.top()))
        paint_panel(painter, tag.toRect(), radius=3, fold=False)
        painter.setFont(ui_font(6.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, str(count))
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
        paint_panel(painter, pill, radius=pill_h / 2, fold=False)
        painter.setPen(pet_accent(self._toast_color))
        painter.setFont(ui_font(7.5, bold=True))
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
            or self._perch_timer.isActive()
        )

    def shutdown(self) -> None:
        """Stop all internal timers and detach state listeners cleanly."""
        self._is_shutting_down = True
        self._anim_timer.stop()
        self._ambient_timer.stop()
        self._topmost_timer.stop()
        self._overlay_timer.stop()
        self._perch_timer.stop()
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
