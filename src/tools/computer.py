"""Mouse and keyboard for the AI chat's computer-use tools: Win32 SendInput on the primary monitor, in physical pixels
(the same pixels `screen_map` reports, because the app is per-monitor DPI aware). Windows only, no dependency."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

from src.tools.watch import is_private_title

MAX_TYPE_CHARS = 2000
MAX_SCROLL = 20

_VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
       "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
       "pageup": 0x21, "pagedown": 0x22, "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
_VK.update({f"f{n}": 0x6F + n for n in range(1, 13)})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B}
_MODIFIERS = {0x10, 0x11, 0x12, 0x5B}

_KEYUP, _EXTENDEDKEY, _UNICODE = 0x2, 0x1, 0x4
_MOUSE_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10)}  # (down, up)
_WHEEL = 0x800


def parse_keys(keys: object) -> list[int]:
    """'ctrl+shift+s' -> virtual-key codes in press order; ValueError names what is not allowed."""
    if not isinstance(keys, str) or not keys.strip():
        raise ValueError("give keys like enter, ctrl+s or alt+tab")
    codes: list[int] = []
    for part in keys.lower().replace(" ", "").split("+"):
        if part in _VK:
            codes.append(_VK[part])
        elif len(part) == 1 and part.isascii() and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key {part!r}")
    if len(codes) > 4 or codes[-1] in _MODIFIERS and len(codes) > 1:
        raise ValueError("use up to three modifiers and one key, e.g. ctrl+shift+s")
    return codes


class _Mouse(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", wintypes.WPARAM)]


class _Key(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", wintypes.WPARAM)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _Mouse), ("ki", _Key)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _send(*inputs: _Input) -> None:
    array = (_Input * len(inputs))(*inputs)
    if _user32().SendInput(len(inputs), array, ctypes.sizeof(_Input)) != len(inputs):
        raise OSError("Windows refused the input (a higher-privilege window may be in front)")


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(type=1, ki=_Key(vk, scan, flags, 0, 0))


def _mouse(flags: int, data: int = 0) -> _Input:
    return _Input(type=0, mi=_Mouse(0, 0, data & 0xFFFFFFFF, flags, 0, 0))


def screen_size() -> tuple[int, int]:
    user32 = _user32()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _move(x: int, y: int) -> None:
    width, height = screen_size()
    if not (-2 <= x < width + 2 and -2 <= y < height + 2):
        raise ValueError(f"({x}, {y}) is off the screen, which is {width}x{height}")
    # a hair past the edge (the taskbar's corner, a centre rounded up) lands on the last pixel instead of failing
    x, y = max(0, min(x, width - 1)), max(0, min(y, height - 1))
    _user32().SetCursorPos(x, y)
    time.sleep(0.05)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    if button not in _MOUSE_FLAGS:
        raise ValueError("button is left or right")
    _move(x, y)
    down, up = _MOUSE_FLAGS[button]
    for _ in range(2 if double else 1):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.06)


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    """Positive scrolls down, negative up (one notch each); the wheel acts where the cursor is."""
    if x is not None and y is not None:
        _move(x, y)
    _send(_mouse(_WHEEL, -max(-MAX_SCROLL, min(MAX_SCROLL, amount)) * 120))


def type_text(text: str, delay: float = 0.02) -> None:
    for char in text.replace("\r\n", "\n")[:MAX_TYPE_CHARS]:  # one Enter per line break, not two
        if char in "\r\n":
            _send(_key(0x0D), _key(0x0D, flags=_KEYUP))
        elif char == "\t":
            _send(_key(0x09), _key(0x09, flags=_KEYUP))
        else:
            data = char.encode("utf-16-le")
            for i in range(0, len(data), 2):  # a surrogate pair is two units
                unit = int.from_bytes(data[i:i + 2], "little")
                _send(_key(0, unit, _UNICODE), _key(0, unit, _UNICODE | _KEYUP))
        time.sleep(delay)


def press(codes: list[int]) -> None:
    downs = [_key(vk, flags=_EXTENDEDKEY if vk in _EXTENDED else 0) for vk in codes]
    ups = [_key(vk, flags=_KEYUP | (_EXTENDEDKEY if vk in _EXTENDED else 0)) for vk in reversed(codes)]
    _send(*downs, *ups)


# The "skeleton" of the front window: its clickable controls from Windows UI Automation, each with an exact rectangle
# (icons and unlabeled buttons that OCR cannot see). Windows PowerShell 5.1 ships the .NET UIAutomationClient.
_UIA_PS = r"""
param([long]$Hwnd)
$ErrorActionPreference = 'Stop'
# Per-monitor DPI aware, so rectangles are physical pixels like SendInput and the grab; unaware, they were scaled
# and drifted further off the further a control sat from the top-left (right and bottom edges missed most)
Add-Type -Namespace BS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr c);'
[void][BS.Dpi]::SetThreadDpiAwarenessContext([IntPtr]-4)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$root = $A::FromHandle([IntPtr]$Hwnd)
$types = @($CT::Button, $CT::CheckBox, $CT::ComboBox, $CT::Edit, $CT::Hyperlink, $CT::ListItem, $CT::MenuItem,
    $CT::RadioButton, $CT::TabItem, $CT::TreeItem, $CT::SplitButton, $CT::Slider, $CT::DataItem, $CT::Document)
$any = [System.Windows.Automation.OrCondition]::new([System.Windows.Automation.Condition[]]@(
    $types | ForEach-Object { [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $_) }))
$visible = [System.Windows.Automation.PropertyCondition]::new($A::IsOffscreenProperty, $false)
$cond = [System.Windows.Automation.AndCondition]::new($any, $visible)
$count = 0
foreach ($e in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    try {
        $r = $e.Current.BoundingRectangle
        if ($r.IsEmpty -or $r.Width -lt 4 -or $r.Height -lt 4) { continue }
        $name = $e.Current.Name
        if (-not $name) { $name = $e.Current.AutomationId }
        if (-not $name) { $name = $e.Current.HelpText }
        $kind = $e.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        "$([int]$r.X),$([int]$r.Y),$([int]$r.Width),$([int]$r.Height)`t$kind`t$name"
        $count++
        if ($count -ge 400) { break }
    } catch { }
}
"""


def foreground_window() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def front_state() -> tuple[str, bool]:
    """(title, is a dialog) of the front window."""
    user32 = _user32()
    user32.GetForegroundWindow.restype = wintypes.HWND
    return window_state(user32.GetForegroundWindow())


def window_state(hwnd: int | None) -> tuple[str, bool]:
    """Modal dialogs are owned windows, so an owner means one is open."""
    if not hwnd:
        return "", False
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value, bool(user32.GetWindow(hwnd, 4))  # GW_OWNER


def front_note(before: tuple[str, bool], after: tuple[str, bool]) -> str:
    """What an action did to the front window, in a line the model can act on without another screen_map."""
    (was, _), (now, dialog) = before, after
    show = lambda t: repr("a private window" if is_private_title(t) else t[:80])  # noqa: E731
    note = f" Front window changed: {show(was)} -> {show(now)}." if now != was else f" Front window unchanged: {show(now)}."
    if dialog:
        note += " A dialog is open in front: answer it or press esc before anything else."
    return note


def _ours(user32, hwnd: int) -> bool:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value == os.getpid()


def front_app() -> int:
    """The window the user is working in: the foreground one, or when that is ours (the pet, its chat or menu),
    the first real app window under ours in the z-order (visible, not minimized, not cloaked, not a tool window,
    titled). 0 when there is none."""
    user32 = _user32()
    fg = foreground_window()
    if fg and not _ours(user32, fg):
        return fg
    user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    hwnd = user32.GetTopWindow(None)
    for _ in range(1000):  # a z-order walk; the bound guards against a window list that changes under us
        if not hwnd:
            return 0
        cloaked = wintypes.DWORD()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not cloaked.value
                and not user32.GetWindowLongW(hwnd, -20) & 0x80  # WS_EX_TOOLWINDOW
                and user32.GetWindowTextLengthW(hwnd) > 0 and not _ours(user32, hwnd)):
            return int(hwnd)
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return 0


def focus_front_app() -> int:
    """Give the keyboard back to the app under the pet and chat when one of ours has it (the chat does after the
    user presses Enter), so typed keys and the described 'front window' are that app. Returns its handle."""
    hwnd = front_app()
    if hwnd and hwnd != foreground_window():
        _user32().SetForegroundWindow(wintypes.HWND(hwnd))  # allowed: we are the foreground process
        time.sleep(0.05)
    return hwnd


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buf, 64)
    return buf.value


def is_topmost(hwnd: int) -> bool:
    user32 = _user32()
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    return bool(user32.GetWindowLongW(hwnd, -20) & 0x8)  # WS_EX_TOPMOST


def under_foreign_topmost(hwnd: int) -> bool:
    """True when a visible topmost window of another process sits above `hwnd` in the z-order (so re-raising it
    would change something); False means it is already on top and a SetWindowPos would be churn."""
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    above = user32.GetWindow(wintypes.HWND(hwnd), 3)  # GW_HWNDPREV
    for _ in range(1000):
        if not above:
            return False
        if user32.IsWindowVisible(above) and user32.GetWindowLongW(above, -20) & 0x8 and not _ours(user32, above):
            return True
        above = user32.GetWindow(above, 3)
    return True


def exclude_from_capture(hwnd: int, on: bool) -> bool:
    """WDA_EXCLUDEFROMCAPTURE: the window stays on screen but is left out of screen grabs (Windows 10 2004+).
    False when Windows refused, so the caller hides the window instead."""
    return bool(_user32().SetWindowDisplayAffinity(wintypes.HWND(hwnd), 0x11 if on else 0))


def click_through(hwnd: int, on: bool) -> bool:
    """Toggle WS_EX_TRANSPARENT on a layered window: clicks and the wheel fall to the window below while it stays
    drawn. False when the window is not layered (the style would not change hit-testing), so the caller hides it."""
    user32 = _user32()
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    style = user32.GetWindowLongW(hwnd, -20)
    if not style & 0x80000:  # WS_EX_LAYERED
        return False
    user32.SetWindowLongW(hwnd, -20, style | 0x20 if on else style & ~0x20)
    return True


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]


def window_on_monitor(hwnd: int) -> tuple[str, tuple[int, int, int, int], tuple[int, int]] | None:
    """(monitor device name, window rect inside that monitor, monitor size), all in physical pixels, or None.
    Uses the DWM frame bounds, which leave out the invisible resize border and shadow."""
    if not hwnd:
        return None
    user32 = _user32()
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    mon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    m = info.rcMonitor
    left, top = max(rect.left, m.left) - m.left, max(rect.top, m.top) - m.top
    right, bottom = min(rect.right, m.right) - m.left, min(rect.bottom, m.bottom) - m.top
    if right - left < 40 or bottom - top < 40:
        return None
    return info.szDevice, (left, top, right, bottom), (m.right - m.left, m.bottom - m.top)


def ui_elements(hwnd: int, timeout_s: float = 25.0) -> str:
    """Raw `x,y,w,h<TAB>Type<TAB>Name` lines for the controls of window `hwnd`; '' on any failure."""
    import subprocess
    import tempfile
    from pathlib import Path

    if not hwnd:
        return ""
    with tempfile.TemporaryDirectory(prefix="bytesprite_uia_") as tmp:
        script = Path(tmp) / "uia.ps1"
        script.write_text(_UIA_PS, encoding="utf-8")
        try:
            proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-File", str(script), str(hwnd)], capture_output=True, timeout=timeout_s,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""
