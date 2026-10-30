"""Tests for browser image drops: validation, sanitizing re-encode, and PetWindow wiring."""

from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QBuffer, QIODevice, QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QColor, QDragEnterEvent, QImage
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.tools.file_forge import ForgeMode
from src.tools.web_drop import (
    html_image_src,
    WebImageJob,
    decode_image_bytes,
    fetch_image_bytes,
    save_clean_png,
    sniff_image_format,
    web_image_stem,
)
from src.ui.pet_window import PetWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _png_bytes(qapp: QApplication) -> bytes:
    img = QImage(4, 3, QImage.Format.Format_ARGB32)
    img.fill(QColor("#41A6F6"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, final_url: str) -> None:
        super().__init__(data)
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url


def _opener(data: bytes, final_url: str = "https://example.com/cat.png"):
    return lambda req, timeout: _FakeResponse(data, final_url)


def test_sniff_and_stem() -> None:
    assert sniff_image_format(b"\x89PNG\r\n\x1a\n....") == "png"
    assert sniff_image_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert sniff_image_format(b"MZ\x90\x00\x03") is None  # Windows executable
    assert web_image_stem("https://x.com/a/My%20Cat.exe?x=1") == "My_Cat"
    assert web_image_stem("data:image/png;base64,AAAA") == "web_image"


def test_fetch_rejects_disguised_and_unsafe_sources(qapp: QApplication) -> None:
    png = _png_bytes(qapp)
    assert fetch_image_bytes("https://example.com/cat.png", opener=_opener(png)) == png

    exe = b"MZ\x90\x00" + b"\x00" * 64  # an .exe served as cat.png
    with pytest.raises(ValueError, match="not an image"):
        fetch_image_bytes("https://example.com/cat.png", opener=_opener(exe))
    with pytest.raises(ValueError, match="too large"):
        fetch_image_bytes("https://example.com/cat.png", max_bytes=10, opener=_opener(png))
    with pytest.raises(ValueError, match="redirected"):
        fetch_image_bytes("https://example.com/cat.png", opener=_opener(png, "file:///C:/Windows/notepad.exe"))
    with pytest.raises(ValueError, match="only http"):
        fetch_image_bytes("file:///C:/Windows/notepad.exe")
    with pytest.raises(ValueError, match="unsupported data"):
        fetch_image_bytes("data:text/html;base64,PHNjcmlwdD4=")


def test_fake_png_header_fails_decode(qapp: QApplication) -> None:
    with pytest.raises(ValueError):
        decode_image_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 20)


def test_save_clean_png_reencodes(qapp: QApplication, tmp_path: Path) -> None:
    payload = _png_bytes(qapp) + b"MZ-hidden-trailing-payload"
    out = save_clean_png(decode_image_bytes(payload), tmp_path, "cat")
    assert out.name == "cat.png"
    assert b"hidden-trailing-payload" not in out.read_bytes()
    assert save_clean_png(decode_image_bytes(payload), tmp_path, "cat").name == "cat_1.png"


def test_web_job_data_url_end_to_end(qapp: QApplication, tmp_path: Path) -> None:
    url = "data:image/png;base64," + base64.b64encode(_png_bytes(qapp)).decode()
    results: list[tuple[object, str]] = []
    job = WebImageJob()
    job.finished.connect(lambda p, e: results.append((p, e)))
    job.start(url, tmp_path)
    deadline = time.monotonic() + 5
    while not results and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    path, err = results[0]
    assert err == "" and isinstance(path, Path) and path.suffix == ".png"


def test_browser_drag_opens_reencode_only_wheel(qapp: QApplication, tmp_path: Path) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    fetched: list[tuple[object, ForgeMode]] = []
    win._start_web_fetch = lambda src, mode: fetched.append((src, mode))  # type: ignore[method-assign]
    try:
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/img/cat.jpg")])
        event = QDragEnterEvent(
            win.sprite_center_point(), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        win.dragEnterEvent(event)
        wheel = win.drop_wheel
        assert wheel is not None and wheel.isVisible()
        modes = {o.mode for o in wheel.options}
        assert ForgeMode.PNG in modes and ForgeMode.WEBP in modes
        assert ForgeMode.ZIP not in modes and ForgeMode.TAR_XZ not in modes

        webp = next(o for o in wheel.options if o.mode == ForgeMode.WEBP)
        win._on_wheel_option_dropped([Path("cat.png")], webp)
        assert fetched == [("https://example.com/img/cat.jpg", ForgeMode.WEBP)]
        assert win._pending_web_source is None
    finally:
        win.shutdown()
        win.close()


def test_recent_files_order_dedupe_cap_and_persist(tmp_path: Path) -> None:
    from src.storage.recent_files import RecentFiles, format_age

    recent = RecentFiles()
    files = []
    for i in range(RecentFiles.MAX_ENTRIES + 3):
        f = tmp_path / f"f{i}.png"
        f.write_bytes(b"x")
        files.append(f)
        recent.add(f, when=1000.0 + i)
    recent.add(files[5], when=2000.0)  # re-adding moves it to the front
    names = [p.name for p, _ in recent.entries()]
    assert names[0] == "f5.png" and names.count("f5.png") == 1
    assert len(names) == RecentFiles.MAX_ENTRIES

    files[12].unlink()  # deleted on disk -> hidden
    assert "f12.png" not in [p.name for p, _ in recent.entries()]

    restored = RecentFiles()
    restored.load_dict(recent.to_dict())
    assert restored.entries() == recent.entries()
    restored.load_dict({"files": [["bad"], [3, 4], "x"]})
    assert restored.entries() == []
    assert (format_age(5), format_age(300), format_age(7200), format_age(200000)) == (
        "just now", "5m ago", "2h ago", "2d ago")


def test_web_conversion_removes_intermediate_png(qapp: QApplication, tmp_path: Path) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    forge = sm.forge_plugin
    try:
        clean = tmp_path / "cat.png"
        clean.write_bytes(b"png")
        webp = tmp_path / "cat.webp"
        webp.write_bytes(b"webp")

        win._web_intermediate = clean
        forge._finished_outputs = (webp,)
        win._collect_forge_outputs()
        assert not clean.exists()
        assert [p for p, _ in win.recent.entries()] == [webp]

        # Failed conversion keeps the clean PNG and lists it instead
        clean.write_bytes(b"png")
        win._web_intermediate = clean
        forge._finished_outputs = ()
        win._collect_forge_outputs()
        assert clean.exists()
        assert win.recent.entries()[0][0] == clean

        menu = win.build_context_menu()
        assert "recent:0" in menu.plugin_actions and "recent:drops" in menu.plugin_actions
        assert menu.plugin_actions["recent:0"].text().startswith("cat.png")
        menu.deleteLater()
    finally:
        win.shutdown()
        win.close()


def test_linked_image_drag_uses_img_src_not_page_url() -> None:
    markup = ('<a href="https://www.youtube.com/@Box2_"><img alt="Box2" '
              'src="https://yt3.ggpht.com/abc=s88-c-k?x=1&amp;y=2"></a>')
    assert html_image_src(markup) == "https://yt3.ggpht.com/abc=s88-c-k?x=1&y=2"
    assert html_image_src('<img src="blob:https://web.whatsapp.com/1">') is None
    assert html_image_src("") is None
    mime = QMimeData()
    mime.setUrls([QUrl("https://www.youtube.com/@Box2_")])
    mime.setHtml(markup)
    assert PetWindow._web_drop_source(mime) == "https://yt3.ggpht.com/abc=s88-c-k?x=1&y=2"
