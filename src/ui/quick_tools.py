"""Screen snipping (image / OCR text / color pick) overlays and global snip hotkeys."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

SNIP_MODES: tuple[str, ...] = ("image", "text", "color")

# ---------------------------------------------------------------------------
# Snip overlay (one frozen-screenshot overlay per monitor)
# ---------------------------------------------------------------------------


class SnipOverlay(QWidget):
    """Frameless topmost overlay showing a frozen screen grab for rubber-band selection."""

    def __init__(
        self,
        shot: QPixmap,
        geometry: QRect,
        mode: str,
        on_done: Callable[[str, object | None], None],
    ) -> None:
        super().__init__(None)
        self._shot = shot
        # Converted once: per-move toImage() on a 4K grab made the color picker stutter
        self._image = shot.toImage()
        self._mode = mode
        self._on_done = on_done
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._hover = QPoint(-1, -1)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setGeometry(geometry)

    def selection_rect(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def _to_device(self, rect: QRect) -> QRect:
        dpr = self._shot.devicePixelRatio() or 1.0
        return QRect(
            int(rect.x() * dpr),
            int(rect.y() * dpr),
            max(1, int(rect.width() * dpr)),
            max(1, int(rect.height() * dpr)),
        )

    def pixel_color(self, pos: QPoint) -> QColor:
        dev = self._to_device(QRect(pos, pos))
        return self._image.pixelColor(dev.x(), dev.y())

    def crop(self, rect: QRect) -> QImage:
        return self._shot.copy(self._to_device(rect)).toImage()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._shot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        sel = self.selection_rect()
        if not sel.isEmpty():
            painter.drawPixmap(sel, self._shot, self._to_device(sel))
            painter.setPen(QPen(QColor("#73EFF7"), 1))
            painter.drawRect(sel.adjusted(0, 0, -1, -1))
        hint = {
            "image": "Drag to snip and copy image  |  Esc or right-click cancels",
            "text": "Drag over text to copy it  |  Esc or right-click cancels",
            "color": "Click a pixel to copy its HEX  |  Esc or right-click cancels",
            "ask": "Drag over a region to add it to the AI chat  |  Esc or right-click cancels",
        }.get(self._mode, "")
        if self._mode == "color" and self.rect().contains(self._hover):
            hint = f"{self.pixel_color(self._hover).name().upper()}  |  {hint}"
            self._paint_loupe(painter)
        painter.setPen(QColor("#F0F7F4"))
        painter.drawText(QRect(0, 12, self.width(), 24), Qt.AlignmentFlag.AlignHCenter, hint)
        painter.end()

    LOUPE_CELLS: int = 11
    LOUPE_ZOOM: int = 10

    def _paint_loupe(self, painter: QPainter) -> None:
        """Nearest-neighbor zoom of the pixels around the cursor, center cell outlined."""
        cells, zoom = self.LOUPE_CELLS, self.LOUPE_ZOOM
        size = cells * zoom
        center = self._to_device(QRect(self._hover, self._hover)).topLeft()
        source = QRect(center.x() - cells // 2, center.y() - cells // 2, cells, cells)
        x = self._hover.x() + 20
        y = self._hover.y() + 20
        if x + size > self.width():
            x = self._hover.x() - 20 - size
        if y + size + 16 > self.height():
            y = self._hover.y() - 20 - size - 16
        target = QRect(x, y, size, size)
        painter.fillRect(target, QColor(0, 0, 0))
        painter.drawImage(target, self._image, source)
        painter.setPen(QPen(QColor("#F0F7F4"), 1))
        painter.drawRect(target.adjusted(0, 0, -1, -1))
        mid = (cells // 2) * zoom
        painter.setPen(QPen(QColor("#73EFF7"), 1))
        painter.drawRect(QRect(x + mid, y + mid, zoom - 1, zoom - 1))
        painter.fillRect(QRect(x, y + size, size, 16), QColor(24, 24, 28, 230))
        painter.setPen(QColor("#F0F7F4"))
        painter.drawText(
            QRect(x, y + size, size, 16),
            Qt.AlignmentFlag.AlignCenter,
            self.pixel_color(self._hover).name().upper(),
        )

    def _finish(self, result: object | None) -> None:
        self._on_done(self._mode, result)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._finish(None)
            return
        if self._mode == "color":
            self._finish(self.pixel_color(event.position().toPoint()).name().upper())
            return
        self._origin = event.position().toPoint()
        self._current = self._origin

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        self._hover = event.position().toPoint()
        if self._origin is not None:
            self._current = self._hover
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._origin is None:
            return
        self._current = event.position().toPoint()
        sel = self.selection_rect()
        self._finish(self.crop(sel) if sel.width() >= 3 and sel.height() >= 3 else None)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is not None and event.key() == Qt.Key.Key_Escape:
            self._finish(None)


class SnipSession:
    """Spans one SnipOverlay over every monitor and reports a single result."""

    def __init__(self, mode: str, on_result: Callable[[str, object | None], None]) -> None:
        self._on_result = on_result
        self._overlays: list[SnipOverlay] = []
        self._done = False
        for screen in QGuiApplication.screens():
            overlay = SnipOverlay(screen.grabWindow(0), screen.geometry(), mode, self._handle_done)
            self._overlays.append(overlay)
            overlay.show()
            overlay.raise_()
            overlay.activateWindow()

    @property
    def overlays(self) -> list[SnipOverlay]:
        return list(self._overlays)

    def _handle_done(self, mode: str, result: object | None) -> None:
        if self._done:
            return
        self._done = True
        self.close()
        self._on_result(mode, result)

    def close(self) -> None:
        for overlay in self._overlays:
            overlay.close()
        self._overlays.clear()


# ---------------------------------------------------------------------------
# Offline OCR via the built-in Windows.Media.Ocr WinRT engine (no new dependency)
# ---------------------------------------------------------------------------

OCR_PS_SCRIPT = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()
$result.Lines | ForEach-Object { $_.Text }
"""


def ocr_image_file(png_path: Path, timeout_s: float = 30.0) -> str:
    """Return text recognized in a PNG via Windows OCR, or '' on any failure."""
    script = png_path.with_suffix(".ps1")
    script.write_text(OCR_PS_SCRIPT, encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script), str(png_path),
            ],
            capture_output=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


# Prints nothing when the folder already has an Explorer window; otherwise opens a hidden
# ShellBrowserWindow (plain ShellExecute ignores SW_SHOWNOACTIVATE and steals focus) and prints its HWND
_OPEN_FOLDER_PS = r"""
$target = $env:BYTESPRITE_FOLDER.TrimEnd('')
$shell = New-Object -ComObject Shell.Application
foreach ($w in $shell.Windows()) {
    try { if ($w.Document.Folder.Self.Path.TrimEnd('') -eq $target) { exit } } catch {}
}
$win = [Activator]::CreateInstance([Type]::GetTypeFromCLSID('C08AFD90-F2A1-11D1-8455-00A0C91F3880'))
$win.Navigate2($target)
$win.HWND
"""


def open_folder_in_background(folder: Path) -> None:
    """Show `folder` in Explorer behind the active window, without focus; no-op if already open."""
    threading.Thread(target=_open_folder_worker, args=(folder,), daemon=True).start()


def _open_folder_worker(folder: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OPEN_FOLDER_PS],
            capture_output=True, timeout=20, text=True,
            env={**os.environ, "BYTESPRITE_FOLDER": str(folder)},  # env var: no quoting of odd names
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        hwnd = int(proc.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    fg = user32.GetForegroundWindow()
    # Slot directly under the active window; under a topmost one that would make Explorer topmost too
    topmost = fg and user32.GetWindowLongW(fg, -20) & 0x8  # GWL_EXSTYLE, WS_EX_TOPMOST
    after = 1 if (not fg or topmost) else fg  # 1 = HWND_BOTTOM
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE


class OcrJob(QObject):
    """Runs OCR on a worker thread and delivers the text back on the Qt main thread."""

    finished = pyqtSignal(str)

    def start(self, image: QImage) -> None:
        # Windows OCR is weak on small UI text, so upscale short crops before recognition
        if image.height() < 200:
            image = image.scaled(
                image.width() * 2, image.height() * 2,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        tmp_dir = Path(tempfile.mkdtemp(prefix="bytesprite_ocr_"))
        png = tmp_dir / "snip.png"
        image.save(str(png), "PNG")
        threading.Thread(target=self._run, args=(png,), daemon=True).start()

    def _run(self, png: Path) -> None:
        text = ocr_image_file(png)
        for leftover in (png, png.with_suffix(".ps1")):
            leftover.unlink(missing_ok=True)
        png.parent.rmdir()
        self.finished.emit(text)


# ---------------------------------------------------------------------------
# Global snip hotkeys (RegisterHotKey on a worker thread, no nativeEvent)
# ---------------------------------------------------------------------------

_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012
HOTKEY_LABEL = "Ctrl+Shift+Alt"
HOTKEY_MODES: dict[int, tuple[str, str]] = {1: ("S", "image"), 2: ("T", "text"), 3: ("C", "color"), 4: ("A", "ai")}


class GlobalHotkeys(QObject):
    """Ctrl+Shift+Alt+S/T/C/A system-wide hotkeys; `triggered` delivers the snip mode ("ai" for A) on the Qt thread.

    Thread-level hotkeys (hWnd NULL) post WM_HOTKEY to the registering thread's queue, so a
    plain GetMessageW loop on a worker thread receives them without touching nativeEvent.
    """

    triggered = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered: list[str] = []

    def start(self) -> None:
        if sys.platform != "win32":
            return
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(2.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the queue before close() can post
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        mods = _MOD_CONTROL | _MOD_SHIFT | _MOD_ALT | _MOD_NOREPEAT
        for hk_id, (key, mode) in HOTKEY_MODES.items():
            # Combos already owned by another app fail here and are skipped
            if user32.RegisterHotKey(None, hk_id, mods, ord(key)):
                self.registered.append(mode)
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam in HOTKEY_MODES:
                    self.triggered.emit(HOTKEY_MODES[msg.wParam][1])
        finally:
            for hk_id in HOTKEY_MODES:
                user32.UnregisterHotKey(None, hk_id)

    def close(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
            self._thread_id = 0

