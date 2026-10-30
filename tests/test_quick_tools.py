"""Tests for screen snip overlays, the Windows OCR bridge, and global snip hotkeys."""

from __future__ import annotations

import os
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
from src.ui.quick_tools import (
    GlobalHotkeys,
    SnipOverlay,
    ocr_image_file,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


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


def test_snip_save_falls_back_when_pictures_dir_refuses_writes(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui import pet_window

    blocker = tmp_path / "pictures_is_a_file"
    blocker.write_text("x")  # mkdir under a file raises OSError, like a refused OneDrive dir
    monkeypatch.setattr(pet_window.QStandardPaths, "writableLocation", lambda _loc: str(blocker))
    monkeypatch.setattr(pet_window.Path, "home", lambda: tmp_path / "home")
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    saved = pet_window.PetWindow._save_snip_png(img)
    assert saved is not None and saved.parent == tmp_path / "home" / ".bytesprite" / "snips"
    assert saved.exists()


def test_submenu_titles_render_literal_ampersand(qapp: QApplication, tmp_path: Path) -> None:
    controller = ByteSpriteApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        menu = controller.window.build_context_menu()
        titles = [sub.title() for sub in menu.plugin_submenus.values()] + [menu.quick_menu.title()]
        # "&&" is how Qt shows a literal "&"; a lone "&" would be swallowed as a mnemonic
        assert "Clip Vault && Quick Transforms" in titles
        assert all("&" not in t.replace("&&", "") for t in titles)
        menu.deleteLater()
    finally:
        controller.shutdown()


def test_closing_a_dialog_does_not_quit_the_app(qapp: QApplication, tmp_path: Path) -> None:
    # Pet windows are Qt.Tool, so Qt would treat a closed file dialog as the last window
    controller = ByteSpriteApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        assert qapp.quitOnLastWindowClosed() is False
    finally:
        controller.shutdown()


def test_color_loupe_paints_without_error(qapp: QApplication) -> None:
    img = QImage(200, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    overlay = SnipOverlay(QPixmap.fromImage(img), QRect(0, 0, 200, 100), "color", lambda _m, _r: None)
    try:
        for hover in (QPoint(5, 5), QPoint(195, 95)):  # loupe flips near the far edges
            overlay._hover = hover
            overlay.grab()
    finally:
        overlay.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 hotkeys")
def test_global_hotkeys_start_and_close_cleanly(qapp: QApplication) -> None:
    hotkeys = GlobalHotkeys()
    hotkeys.start()
    assert hotkeys._thread_id != 0
    hotkeys.close()
    time.sleep(0.2)  # let the worker leave its loop and unregister
    # Combos are released, so a second instance can register them again
    again = GlobalHotkeys()
    again.start()
    assert set(again.registered) >= set(hotkeys.registered)
    again.close()
