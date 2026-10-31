"""Offline context hints: suggest an existing tool from the foreground app and the clipboard.

No network and no model: a few fixed rules over the foreground process name, how long it has
been in front, and newly copied clipboard text. Each hint maps to a callback id the pet already runs.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem
from src.tools.clip_vault import decode_jwt
from src.tools.ports import process_name

CODE_APPS = frozenset({
    "code.exe", "cursor.exe", "windsurf.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
    "webstorm64.exe", "sublime_text.exe", "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
})
# Copies made while these are in front are never inspected, even after switching away
PRIVATE_APPS = frozenset({
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe", "dashlane.exe",
})
FOCUS_DWELL_S = 600.0  # a code app in front this long while Pomodoro is idle
COOLDOWN_S = {"focus": 1800.0}
DEFAULT_COOLDOWN_S = 120.0
MAX_CLIP_CHARS = 8192

_JWT_RE = re.compile(r"^eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*$")
_EPOCH_MIN, _EPOCH_MAX = 946_684_800, 4_102_444_800  # 2000..2100, so phone numbers and ids stay quiet


@dataclass(frozen=True)
class Suggestion:
    rule: str
    text: str
    action: str


def suggest(app: str, clip: str, dwell_s: float, pomodoro_idle: bool) -> Suggestion | None:
    """First matching rule for the foreground exe, new clipboard text ('' if unchanged) and dwell time."""
    clip = clip.strip()
    if clip and len(clip) <= MAX_CLIP_CHARS:
        if _JWT_RE.match(clip):
            try:
                decode_jwt(clip)
                return Suggestion("jwt", "Decode this JWT?", "clipvault:dev:jwt")
            except (ValueError, RecursionError):  # deeply nested JSON raises RecursionError
                pass
        if clip.isascii() and clip.isdigit() and len(clip) in (10, 13):  # '²'.isdigit() but int() rejects it
            seconds = int(clip) // (1000 if len(clip) == 13 else 1)
            if _EPOCH_MIN <= seconds <= _EPOCH_MAX:
                return Suggestion("time", "Convert this timestamp?", "clipvault:dev:time")
        if clip[0] in "{[" and "\n" not in clip and len(clip) >= 40:
            try:
                parsed = json.loads(clip)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)) and len(parsed) >= 2:
                return Suggestion("json", "Pretty-print this JSON?", "clipvault:json_toggle")
    if pomodoro_idle and app.lower() in CODE_APPS and dwell_s >= FOCUS_DWELL_S:
        return Suggestion("focus", "Start a focus session?", "pet:focus")
    return None


def foreground_app() -> str:
    """Exe name of the foreground window ('' for none, ByteSprite itself, or off Windows)."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in (0, os.getpid()):
        return ""
    return process_name(pid.value)


class HintsPlugin:
    """Rule state: dwell tracking, clipboard change detection, per-rule cooldowns and session mutes."""

    SUBMENU_NAME: str = "Hints"

    def __init__(self) -> None:
        self._enabled = True
        self._app = ""
        self._app_since = 0.0
        self._last_clip = ""
        self._next_ok: dict[str, float] = {}
        self._muted: set[str] = set()
        self.shown = self.used = self.skipped = 0

    @property
    def tool_id(self) -> str:
        return "hints"

    @property
    def display_name(self) -> str:
        return "Hints"

    @property
    def tick_interval_ms(self) -> int:
        return 2000

    @property
    def enabled(self) -> bool:
        return self._enabled

    def poll(self, app: str, clip: str, pomodoro_idle: bool, now: float | None = None) -> Suggestion | None:
        """Feed one sample; returns a hint to show now or None."""
        now = time.monotonic() if now is None else now
        new_clip = "" if clip == self._last_clip else clip
        self._last_clip = clip  # updated even when skipping, so a private copy is never offered later
        if not self._enabled or not app or app.lower() in PRIVATE_APPS:
            return None
        if app != self._app:
            self._app, self._app_since = app, now
        hint = suggest(app, new_clip, now - self._app_since, pomodoro_idle)
        if hint is None or hint.rule in self._muted or now < self._next_ok.get(hint.rule, 0.0):
            return None
        self._next_ok[hint.rule] = now + COOLDOWN_S.get(hint.rule, DEFAULT_COOLDOWN_S)
        if hint.rule == "focus":
            self._app_since = now  # the next focus hint needs another full dwell
        self.shown += 1
        return hint

    def mark_used(self) -> None:
        self.used += 1

    def dismiss(self, rule: str) -> None:
        """Right-click on a hint: never offer that rule again this session."""
        self._muted.add(rule)
        self.skipped += 1

    def tick(self) -> bool:
        return False

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        top = max(1, self.shown)
        return HudData(
            badge_left="HINTS",
            badge_right="ON" if self._enabled else "OFF",
            badge_right_color="#38B764" if self._enabled else "#8A9AF2",
            bars=(
                HudBar("Shown", min(100.0, self.shown * 10.0), "#73EFF7", f"{self.shown:3d}"),
                HudBar("Used", self.used * 100.0 / top, "#38B764", f"{self.used:3d}"),
                HudBar("Skipped", self.skipped * 100.0 / top, "#EF7D57", f"{self.skipped:3d}"),
            ),
            detail="Offline: active app + new clipboard text" if self._enabled else "Turn on from the Hints menu",
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        return [ToolMenuItem(label="Smart Suggestions", callback_id="hints:toggle", checkable=True,
                             checked=self._enabled, submenu=self.SUBMENU_NAME)]

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id != "hints:toggle":
            return (False, "", "")
        self._enabled = not self._enabled
        return (True, "HINTS ON" if self._enabled else "HINTS OFF", "#73EFF7")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self._enabled}

    def load_dict(self, data: dict[str, Any]) -> None:
        if isinstance(data, dict) and data.get("enabled") is False:
            self._enabled = False
