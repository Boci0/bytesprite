"""Screen snipping (image / OCR text / color pick) overlays and the human-speed typer."""

from __future__ import annotations

import ctypes
import random
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

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
        return self._shot.toImage().pixelColor(dev.x(), dev.y())

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
            "image": "Drag to snip and copy image  |  Esc cancels",
            "text": "Drag over text to copy it  |  Esc cancels",
            "color": "Click a pixel to copy its HEX  |  Esc cancels",
        }.get(self._mode, "")
        if self._mode == "color" and self.rect().contains(self._hover):
            hint = f"{self.pixel_color(self._hover).name().upper()}  |  {hint}"
        painter.setPen(QColor("#F0F7F4"))
        painter.drawText(QRect(0, 12, self.width(), 24), Qt.AlignmentFlag.AlignHCenter, hint)
        painter.end()

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
# Human-speed typer (Win32 SendInput with KEYEVENTF_UNICODE)
# ---------------------------------------------------------------------------


def human_delay_ms(ch: str, wpm: int, rng: random.Random) -> int:
    """Delay to wait after typing `ch` so output reads like a person at ~`wpm` words/min."""
    base = 60_000.0 / (max(5, wpm) * 5)
    delay = base * rng.uniform(0.55, 1.5)
    if ch == " ":
        delay *= 1.3
    elif ch in ".!?":
        delay += rng.uniform(250, 600)
    elif ch in ",;:":
        delay += rng.uniform(100, 250)
    elif ch == "\n":
        delay += rng.uniform(400, 900)
    if rng.random() < 0.03:
        delay += rng.uniform(400, 1200)  # occasional thinking pause
    return int(max(15.0, delay))


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_ESCAPE = 0x1B
_SPECIAL_VK: dict[str, int] = {"\n": 0x0D, "\t": 0x09}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def send_char(ch: str) -> None:
    """Inject one character into the focused window (Enter/Tab as real keys)."""
    if sys.platform != "win32":
        return
    events: list[_INPUT] = []
    if ch in _SPECIAL_VK:
        vk = _SPECIAL_VK[ch]
        for flags in (0, _KEYEVENTF_KEYUP):
            events.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0))))
    else:
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i:i + 2], "little")
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                events.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, unit, flags, 0, 0))))
    arr = (_INPUT * len(events))(*events)
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def _escape_held() -> bool:
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000)


class HumanTyper(QObject):
    """Types text one character at a time on a single-shot QTimer (no threads, no polling loop)."""

    def __init__(
        self,
        *,
        on_finished: Callable[[str], None],
        send: Callable[[str], None] = send_char,
        escape_held: Callable[[], bool] = _escape_held,
        rng: random.Random | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_finished = on_finished
        self._send = send
        self._escape_held = escape_held
        self._rng = rng or random.Random()
        self._text = ""
        self._index = 0
        self._wpm = 65
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._step)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self, text: str, wpm: int, start_delay_ms: int = 3000) -> None:
        self._text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._index = 0
        self._wpm = wpm
        self._timer.start(start_delay_ms)

    def stop(self, reason: str = "STOPPED") -> None:
        if self._timer.isActive() or self._index < len(self._text):
            self._timer.stop()
            self._text = ""
            self._index = 0
            self._on_finished(reason)

    def _step(self) -> None:
        if self._escape_held():
            self.stop("STOPPED")
            return
        if self._index >= len(self._text):
            self._text = ""
            self._on_finished("TYPED")
            return
        ch = self._text[self._index]
        self._send(ch)
        self._index += 1
        self._timer.start(human_delay_ms(ch, self._wpm, self._rng))


TYPER_STYLESHEET = """
QDialog { background-color: #181425; color: #F0F7F4; }
QLabel { color: #8B9BB4; font-family: "Consolas", "Segoe UI"; font-size: 12px; }
QPlainTextEdit, QSpinBox {
    background-color: #262B44; color: #F0F7F4; border: 1px solid #3A4466;
    font-family: "Consolas", "Cascadia Mono", monospace; font-size: 12px;
}
QPushButton {
    background-color: #263859; color: #73EFF7; border: 1px solid #3A4466;
    padding: 5px 14px; font-family: "Consolas", "Segoe UI";
}
QPushButton:hover { background-color: #3A4466; }
"""


class TyperDialog(QDialog):
    """Small input box: paste text, pick a speed, then click into the target field."""

    def __init__(self, parent: QWidget | None = None, wpm: int = 65) -> None:
        super().__init__(parent)
        self.setWindowTitle("ByteSprite Human Typer")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(TYPER_STYLESHEET)
        self.resize(420, 260)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText("Text to type at human speed...")
        self.wpm_box = QSpinBox(self)
        self.wpm_box.setRange(10, 200)
        self.wpm_box.setValue(wpm)
        self.wpm_box.setSuffix(" wpm")
        start_btn = QPushButton("Start (3s)", self)
        start_btn.setDefault(True)
        start_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel", self)
        cancel_btn.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(QLabel("Speed", self))
        row.addWidget(self.wpm_box)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(start_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("After Start you have 3s to click where the text goes. Hold Esc to stop.", self)
        )
        layout.addWidget(self.editor)
        layout.addLayout(row)

    @property
    def text(self) -> str:
        return self.editor.toPlainText()

    @property
    def wpm(self) -> int:
        return self.wpm_box.value()
