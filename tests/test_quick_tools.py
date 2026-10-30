"""Tests for screen snip overlays, Windows OCR bridge, and the human-speed typer."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from src.main import ByteSpriteApp
from src.storage.storage_manager import StorageManager
from src.ui.context_menu import QUICK_TOOL_ITEMS
from src.ui.quick_tools import HumanTyper, SnipOverlay, human_delay_ms, ocr_image_file


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def test_human_delay_is_bounded_and_pauses_on_punctuation() -> None:
    rng = random.Random(7)
    letters = [human_delay_ms("a", 60, rng) for _ in range(300)]
    stops = [human_delay_ms(".", 60, rng) for _ in range(300)]
    assert min(letters) >= 15
    assert sum(stops) / len(stops) > sum(letters) / len(letters) + 200
    # Faster wpm types faster on average
    fast = [human_delay_ms("a", 150, rng) for _ in range(300)]
    assert sum(fast) < sum(letters)


def test_human_typer_sends_every_char_and_stops_on_escape(qapp: QApplication) -> None:
    sent: list[str] = []
    reasons: list[str] = []
    typer = HumanTyper(
        on_finished=reasons.append, send=sent.append, escape_held=lambda: False,
        rng=random.Random(1),
    )
    typer.start("hi\r\nyo", wpm=200, start_delay_ms=0)
    deadline = time.monotonic() + 5
    while not reasons and time.monotonic() < deadline:
        qapp.processEvents()
    assert sent == list("hi\nyo")
    assert reasons == ["TYPED"]

    held = {"esc": False}
    sent.clear()
    reasons.clear()
    typer = HumanTyper(
        on_finished=reasons.append, send=sent.append, escape_held=lambda: held["esc"],
    )
    typer.start("long text", wpm=200, start_delay_ms=0)
    held["esc"] = True
    deadline = time.monotonic() + 2
    while not reasons and time.monotonic() < deadline:
        qapp.processEvents()
    assert reasons == ["STOPPED"]
    assert sent == []
    assert typer.running is False


def test_snip_overlay_crops_in_device_pixels_and_reads_colors(qapp: QApplication) -> None:
    img = QImage(200, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    img.setPixelColor(150, 50, QColor("#ABCDEF"))
    shot = QPixmap.fromImage(img)
    shot.setDevicePixelRatio(2.0)  # 100x50 logical, like a 200% scaled monitor
    results: list[object] = []
    overlay = SnipOverlay(shot, QRect(0, 0, 100, 50), "image", lambda _m, r: results.append(r))
    try:
        crop = overlay.crop(QRect(10, 10, 20, 15))
        assert (crop.width(), crop.height()) == (40, 30)
        assert overlay.pixel_color(QPoint(75, 25)).name().upper() == "#ABCDEF"
    finally:
        overlay.deleteLater()


def test_context_menu_exposes_quick_tools(qapp: QApplication, tmp_path: Path) -> None:
    controller = ByteSpriteApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        menu = controller.window.build_context_menu()
        for _label, cid in QUICK_TOOL_ITEMS:
            assert cid in menu.plugin_actions
        menu.deleteLater()
    finally:
        controller.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR only")
def test_windows_ocr_reads_rendered_text(tmp_path: Path) -> None:
    # Render in a child process: the offscreen QPA used by other test modules has no fonts
    png = tmp_path / "ocr.png"
    script = "; ".join([
        "import sys",
        "from PyQt6.QtGui import QColor, QFont, QImage, QPainter",
        "from PyQt6.QtWidgets import QApplication",
        "app = QApplication([])",
        "img = QImage(520, 120, QImage.Format.Format_RGB32)",
        "img.fill(QColor('white'))",
        "p = QPainter(img)",
        "p.setFont(QFont('Arial', 36))",
        "p.setPen(QColor('black'))",
        "p.drawText(20, 80, 'Hello Sprite')",
        "p.end()",
        "img.save(sys.argv[1], 'PNG')",
    ])
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    subprocess.run([sys.executable, "-c", script, str(png)], check=True, env=env, timeout=60)
    assert "Hello" in ocr_image_file(png)
