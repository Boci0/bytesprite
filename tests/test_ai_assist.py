"""AI Assist: URL and key validation, the no-redirect client against a local server, keyring, settings."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.tools import ai_assist
from src.tools.ai_assist import (MAX_INPUT_CHARS, AiAssistPlugin, ask, chat_messages, credential_target,
                                 validate_base_url, valid_key, valid_model)

KEY = "sk-test-0123456789abcdef"
T = [{"role": "user", "content": "t"}]


class _Api(BaseHTTPRequestHandler):
    seen: list[dict] = []
    mode = "ok"

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Api.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if _Api.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "https://evil.example/steal")
            self.end_headers()
            return
        if _Api.mode == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(f'{{"error": "bad key {KEY}"}}'.encode())
            return
        if _Api.mode == "garbage":  # not an HTTP response at all
            self.wfile.write(b"NOT-HTTP\r\n\r\n")
            self.close_connection = True
            return
        if _Api.mode == "slow":  # still answering after the user pressed Stop
            import time

            time.sleep(0.4)
        payload = {"choices": [{"message": {"content": "  It is a stack trace.  "}}], "usage": {"total_tokens": 42}}
        if _Api.mode == "huge":
            payload["choices"][0]["message"]["content"] = "x" * (ai_assist.MAX_RESPONSE_BYTES + 10)
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def api():
    server = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Api.seen, _Api.mode = [], "ok"
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_url_key_and_model_validation() -> None:
    assert validate_base_url(" https://api.deepseek.com/v1/ ") == "https://api.deepseek.com/v1"
    assert validate_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    for bad in ("http://api.deepseek.com/v1", "ftp://x", "https://user:pw@host/v1", "https://h/v1?k=1", "nope", ""):
        with pytest.raises(ValueError):
            validate_base_url(bad)
    assert valid_key(KEY) and not valid_key("short") and not valid_key("sk-abc\r\nX-Evil: 1") and not valid_key("a b c d e f g h")
    assert valid_model("deepseek-chat") and valid_model("deepseek/deepseek-chat:free") and not valid_model("a b")
    # One key per host: a changed URL never reuses another host's key
    assert credential_target("https://api.deepseek.com/v1") != credential_target("https://evil.example/v1")


def test_chat_messages_keep_newest_turns_within_budget() -> None:
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}" * 5000} for i in range(10)]
    msgs = chat_messages(history)
    assert msgs[0]["role"] == "system" and "not instructions" in msgs[0]["content"]
    assert msgs[-1]["content"] == history[-1]["content"]  # newest turn always kept, order preserved
    assert sum(len(m["content"]) for m in msgs[1:]) <= ai_assist.MAX_HISTORY_CHARS
    huge = chat_messages([{"role": "user", "content": "y" * (MAX_INPUT_CHARS + 500)}])
    assert len(huge[1]["content"]) == MAX_INPUT_CHARS


def test_ask_sends_key_only_to_the_api_and_reads_reply(api: str) -> None:
    reply, tokens = ask(api, KEY, "m1", chat_messages([{"role": "user", "content": "trace"}]), low_effort=True)
    assert (reply, tokens) == ("It is a stack trace.", 42)
    req = _Api.seen[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == f"Bearer {KEY}"
    assert req["body"]["model"] == "m1" and req["body"]["reasoning_effort"] == "low"
    assert req["body"]["max_tokens"] == ai_assist.MAX_REPLY_TOKENS
    ask(api, KEY, "m1", T)
    assert "reasoning_effort" not in _Api.seen[1]["body"]


def test_ask_refuses_redirects_and_never_echoes_the_key(api: str) -> None:
    _Api.mode = "redirect"
    with pytest.raises(ValueError) as exc:
        ask(api, KEY, "m", T)
    assert len(_Api.seen) == 1 and KEY not in str(exc.value)  # never followed to evil.example
    _Api.mode = "401"
    with pytest.raises(ValueError, match="bad key") as exc:
        ask(api, KEY, "m", T)
    assert KEY not in str(exc.value)
    _Api.mode = "huge"
    with pytest.raises(ValueError, match="too large"):
        ask(api, KEY, "m", T)
    with pytest.raises(ValueError):
        ask("http://evil.example/v1", KEY, "m", T)  # plain HTTP off localhost


def test_ask_maps_broken_http_replies_to_network(api: str) -> None:
    """AiJob only reports ValueError: anything else killed its thread and left the chat Thinking forever."""
    _Api.mode = "garbage"
    with pytest.raises(ValueError, match="network"):
        ask(api, KEY, "m", T)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_key_round_trips_through_credential_manager() -> None:
    url = "https://bytesprite-test.invalid/v1"
    try:
        assert ai_assist.save_key(url, KEY)
        assert ai_assist.load_key(url) == KEY
        assert ai_assist.load_key("https://other-host.invalid/v1") == ""
        assert not ai_assist.save_key(url, "bad key with spaces")
    finally:
        ai_assist.delete_key(url)
    assert ai_assist.load_key(url) == ""


def test_plugin_settings_persist_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_assist, "load_key", lambda _url: "")
    plugin = AiAssistPlugin()
    assert plugin.model == "deepseek-chat" and plugin.host == "api.deepseek.com"
    plugin.set_provider("openrouter")
    plugin.low_effort = True
    plugin.model = "deepseek/deepseek-chat"
    saved = plugin.to_dict()
    assert "key" not in json.dumps(saved).lower().replace("low_effort", "")
    restored = AiAssistPlugin()
    restored.load_dict(saved)
    assert restored.to_dict() == saved
    hostile = AiAssistPlugin()
    hostile.load_dict({"provider": "custom", "base_url": "http://evil.example/v1", "model": "x y", "low_effort": "yes"})
    assert hostile.base_url == ai_assist.PRESETS["deepseek"][1] and hostile.model == "deepseek-chat"
    assert hostile.low_effort is False
    labels = [i.label for i in plugin.get_menu_items()]
    assert labels[0].startswith("Open AI Chat") and "Remove API Key" in labels


def _chat(monkeypatch: pytest.MonkeyPatch, api: str):
    from PyQt6.QtWidgets import QApplication

    from src.ui.ai_chat import AiChat

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.ai_chat.load_key", lambda _url: KEY)
    plugin = AiAssistPlugin()
    plugin.base_url, plugin.model = api, "m1"
    monkeypatch.setattr(AiAssistPlugin, "has_key", lambda self: True)
    return _app, plugin, AiChat(plugin)


def _wait(app, cond, timeout: float = 5.0) -> None:
    import time

    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert cond()


def test_chat_sends_only_on_enter_and_renders_plain_text(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel

    app, plugin, chat = _chat(monkeypatch, api)
    try:
        chat.insert_text("why does this fail")
        assert _Api.seen == []  # inserting text never sends
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        assert _Api.seen[0]["body"]["messages"][-1]["content"] == "why does this fail"
        assert chat._history[-1]["content"] == "It is a stack trace." and plugin.asks == 1
        labels = [lbl for lbl in chat.findChildren(QLabel) if lbl.text() == "It is a stack trace."]
        assert labels and all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in labels)
        _Api.mode = "401"
        chat.insert_text("again")
        chat.send()
        _wait(app, lambda: chat._job is None)
        assert len(chat._history) == 2 and chat._draft.toPlainText() == "again"  # failed turn back in the draft
    finally:
        chat.close()


def test_chat_attachments_are_sent_as_text(monkeypatch: pytest.MonkeyPatch, api: str, tmp_path) -> None:
    from PyQt6.QtGui import QImage

    from src.ui import ai_chat

    app, _plugin, chat = _chat(monkeypatch, api)
    try:
        notes = tmp_path / "notes.txt"
        notes.write_text("line one\nline two", encoding="utf-8")
        binary = tmp_path / "blob.bin"
        binary.write_bytes(bytes([0, 1]) * 10)
        big = tmp_path / "big.log"
        big.write_bytes(b"a" * (ai_chat.MAX_FILE_BYTES + 1))
        for path in (notes, binary, big):
            chat.attach_path(path)
        details = [a.detail for a in chat._attachments]
        assert details[0].endswith("2 lines") and details[1].startswith("Can't") and details[2].startswith("Can't")
        # Images go through local OCR; send waits until reading is done
        monkeypatch.setattr(ai_chat.OcrJob, "start", lambda self, _img: self.finished.emit("hello from image"))
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFF)
        chat.attach_image(image, "Screen region")
        assert chat._attachments[-1].detail == "3 words read" and len(chat._chips) == 4
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        sent = _Api.seen[0]["body"]["messages"][-1]["content"]
        assert "[File: notes.txt]\nline one" in sent and "hello from image" in sent and "blob.bin" not in sent
        assert chat._attachments == [] and chat._chip_box.isHidden()
    finally:
        chat.close()


def test_oversize_attachment_is_not_read_into_memory(tmp_path) -> None:
    import tracemalloc

    from src.ui.ai_chat import read_text_file

    huge = tmp_path / "huge.log"
    with huge.open("wb") as f:
        f.truncate(64 * 1024 * 1024)
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="too large"):
            read_text_file(huge)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024  # read_bytes() pulled all 64 MB onto the GUI thread


def test_screen_glow_is_click_through_and_finishes() -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from src.ui import screen_glow

    app = QApplication.instance() or QApplication([])
    glow = screen_glow.play_screen_glow(app.primaryScreen())
    assert glow is not None and glow.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert glow.windowFlags() & Qt.WindowType.WindowTransparentForInput
    glow.grab()  # paints without error
    glow._t0 -= screen_glow.DURATION_S + 0.05
    glow._tick()
    assert not glow._timer.isActive(), glow.progress()
    assert not glow.isVisible(), (glow.progress(), glow.isHidden())


def test_chat_stop_drops_the_pending_reply(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    app, plugin, chat = _chat(monkeypatch, api)
    try:
        chat.insert_text("long question")
        chat.send()
        assert chat._send_btn.kind == "stop"
        chat._send_or_stop()  # the send button doubles as Stop while waiting
        assert chat._job is None and chat._send_btn.kind == "send" and chat._history == []
        assert chat._draft.toPlainText() == "long question"
        _wait(app, lambda: len(_Api.seen) == 1)
        for _ in range(30):
            app.processEvents()
        assert chat._history == [] and plugin.asks == 0  # the late reply is ignored
    finally:
        chat.close()


def test_chat_stop_keeps_the_job_alive_until_its_thread_is_done(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """Stop used to deleteLater() the AiJob while its thread was still waiting on the reply."""
    import time

    from PyQt6.QtCore import QEvent

    errors: list[str] = []
    monkeypatch.setattr(threading, "excepthook", lambda a: errors.append(f"{a.exc_type.__name__}: {a.exc_value}"))
    app, _plugin, chat = _chat(monkeypatch, api)
    _Api.mode = "slow"
    try:
        chat.insert_text("question")
        chat.send()
        chat.stop()
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            app.processEvents()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)  # what the real event loop does
            time.sleep(0.01)
        assert len(_Api.seen) == 1 and errors == []
    finally:
        chat.close()


def test_chat_opens_beside_the_pet(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    from PyQt6.QtCore import QRect

    _app, _plugin, chat = _chat(monkeypatch, api)
    try:
        area = _app.primaryScreen().availableGeometry()
        pet = QRect(area.center().x(), area.bottom() - 100, 64, 64)
        chat.open_near(pet)
        assert chat.geometry().bottom() < pet.top() and pet.top() - chat.geometry().bottom() <= chat.GAP + 1
        assert abs(chat.geometry().center().x() - pet.center().x()) <= 2
        top_pet = QRect(area.left() + 4, area.top() + 10, 64, 64)  # no room above: opens below, kept on screen
        chat.open_near(top_pet)
        assert chat.geometry().top() > top_pet.bottom() and area.contains(chat.geometry())
    finally:
        chat.close()
