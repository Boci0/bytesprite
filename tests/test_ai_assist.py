"""AI Assist: URL and key validation, the no-redirect client against a local server, keyring, settings."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.tools import ai_assist
from src.tools.ai_assist import (MAX_INPUT_CHARS, AiAssistPlugin, ask, chat_messages, credential_target,
                                 list_models, thinking_params, validate_base_url, valid_key, valid_model)

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
        if _Api.mode == "no-vision" and any(isinstance(m["content"], list) for m in body["messages"]):
            self.send_response(400)
            self.end_headers()
            return
        if _Api.mode == "garbage":  # not an HTTP response at all
            self.wfile.write(b"NOT-HTTP\r\n\r\n")
            self.close_connection = True
            return
        if self.path.endswith("/messages"):  # Claude's native API: first answer pauses mid-search, then finishes
            paused = body["messages"][-1]["role"] != "assistant"
            payload = {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                                  "output_tokens_details": {"thinking_tokens": 2}},
                       "stop_reason": "pause_turn" if paused else "end_turn",
                       "content": [{"type": "thinking", "thinking": "hm"},
                                   {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}] if paused else
                                  [{"type": "text", "text": "Answer.", "citations": [
                                      {"type": "web_search_result_location", "url": "https://python.org", "title": "Py"}]}]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if _Api.mode == "tool-once" and "tools" in body and body["messages"][-1]["role"] != "tool":
            data = json.dumps({"usage": {"total_tokens": 5}, "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "web_search", "arguments": '{"query": "news"}'}}]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if _Api.mode == "slow":  # still answering after the user pressed Stop
            import time

            time.sleep(0.4)
        payload = {"choices": [{"message": {"content": "  It is a stack trace.  "}}], "usage": {"total_tokens": 42}}
        if _Api.mode == "thinking":  # DeepSeek's shape: served model, reasoning_content, reasoning token split
            payload = {"model": "deepseek-flash", "usage": {"prompt_tokens": 43, "completion_tokens": 20,
                                                            "total_tokens": 63,
                                                            "completion_tokens_details": {"reasoning_tokens": 18}},
                       "choices": [{"finish_reason": "stop", "message": {
                           "content": "391", "reasoning_content": "17*20=340, +51=391."}}]}
        if _Api.mode == "length":  # all budget spent thinking
            payload = {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "hm"}}]}
        if _Api.mode == "huge":
            payload["choices"][0]["message"]["content"] = "x" * (ai_assist.MAX_RESPONSE_BYTES + 10)
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        _Api.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
        data = json.dumps({"data": [
            {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "input_modalities": ["text"]},
            {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash", "input_modalities": ["text", "image"]},
            {"id": "models/gemini-x"}, {"id": "bad id with spaces"}, "junk", {"id": 5},
        ]}).encode()
        self.send_response(200)
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
    reply = ask(api, KEY, "m1", chat_messages([{"role": "user", "content": "trace"}]), thinking="low")
    assert (reply.text, reply.total_tokens, reply.model) == ("It is a stack trace.", 42, "")
    req = _Api.seen[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == f"Bearer {KEY}"
    assert req["body"]["model"] == "m1" and req["body"]["reasoning_effort"] == "low"
    assert req["body"]["max_tokens"] == ai_assist.MAX_THINKING_TOKENS and "thinking" not in req["body"]
    ask(api, KEY, "m1", T)
    assert "reasoning_effort" not in _Api.seen[1]["body"]
    assert _Api.seen[1]["body"]["max_tokens"] == ai_assist.MAX_REPLY_TOKENS


def test_thinking_params_per_provider() -> None:
    ds, oa = "https://api.deepseek.com/v1", "https://api.openai.com/v1"
    assert thinking_params(ds, "off") == {"thinking": {"type": "disabled"}}  # DeepSeek thinks unless told not to
    assert thinking_params(ds, "max") == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    assert thinking_params(oa, "off") == {} and thinking_params(oa, "max") == {"reasoning_effort": "high"}
    assert thinking_params(oa, "bogus") == {}


def test_reply_details_show_served_model_reasoning_and_tokens(api: str) -> None:
    from src.ui.ai_chat import reply_details

    _Api.mode = "thinking"
    reply = ask(api, KEY, "deepseek-chat", T, thinking="low")
    assert (reply.text, reply.model, reply.reasoning) == ("391", "deepseek-flash", "17*20=340, +51=391.")
    assert (reply.prompt_tokens, reply.completion_tokens, reply.reasoning_tokens) == (43, 20, 18)
    assert reply_details(reply, "deepseek-chat", "low") == (
        "deepseek-chat > deepseek-flash  -  thinking low  -  63 tokens (43 in, 18 thinking, 2 answer)")
    _Api.mode = "length"
    with pytest.raises(ValueError, match="cut off"):
        ask(api, KEY, "m", T, thinking="max")


def test_list_models_reads_names_and_vision_and_skips_junk(api: str) -> None:
    models = list_models(api, KEY)
    assert [(m.id, m.name, m.vision) for m in models] == [
        ("deepseek-flash", "DeepSeek-V4.1-Flash", True), ("deepseek-v4-pro", "DeepSeek-V4-Pro", False),
        ("gemini-x", "", None)]
    assert _Api.seen[-1]["path"] == "/v1/models" and _Api.seen[-1]["auth"] == f"Bearer {KEY}"


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


def test_images_go_as_parts_and_fall_back_to_text(api: str) -> None:
    url = "data:image/png;base64,AAAA"
    msgs = chat_messages([{"role": "user", "content": "help [Image: x]\nC6H12O6", "images": [url]}])
    assert msgs[1]["content"] == [{"type": "text", "text": "help [Image: x]\nC6H12O6"},
                                  {"type": "image_url", "image_url": {"url": url}}]
    assert ask(api, KEY, "vision", msgs).text == "It is a stack trace."
    assert _Api.seen[-1]["body"]["messages"][1]["content"][1]["image_url"]["url"] == url
    _Api.mode = "no-vision"  # text-only model: retried once without the image, then images are skipped
    assert ask(api, KEY, "text-only", msgs).text == "It is a stack trace."
    assert len(_Api.seen) == 3 and _Api.seen[-1]["body"]["messages"][1]["content"] == "help [Image: x]\nC6H12O6"
    ask(api, KEY, "text-only", msgs)
    assert len(_Api.seen) == 4


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
    assert plugin.model == "deepseek-flash" and plugin.host == "api.deepseek.com" and plugin.thinking == "off"
    plugin.set_provider("openrouter")
    plugin.thinking = "high"
    plugin.model = "deepseek/deepseek-chat"
    saved = plugin.to_dict()
    assert "key" not in json.dumps(saved).lower()
    restored = AiAssistPlugin()
    restored.load_dict(saved)
    assert restored.to_dict() == saved
    hostile = AiAssistPlugin()
    hostile.load_dict({"provider": "custom", "base_url": "http://evil.example/v1", "model": "x y", "thinking": "ultra"})
    assert hostile.base_url == ai_assist.PRESETS["deepseek"][1] and hostile.model == "deepseek-flash"
    assert hostile.thinking == "off"
    old = AiAssistPlugin()
    old.load_dict({"provider": "deepseek", "model": "deepseek-chat", "low_effort": True})  # saved before levels
    assert (old.model, old.thinking) == ("deepseek-chat", "low")
    labels = [i.label for i in plugin.get_menu_items()]
    assert labels[0].startswith("Open AI Chat") and "Remove API Key" in labels and "Remove Search Key" in labels
    gone = []
    monkeypatch.setattr(ai_assist, "delete_key", lambda url: gone.append(url) or True)
    assert plugin.execute_action("ai:forget_search")[1] == "KEY GONE" and gone == [ai_assist.BRAVE_URL]


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


def test_model_switcher_and_reasoning_toggle_work(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """The header lists the provider's real models, picks change the next request, and Show reasoning flips."""
    from PyQt6.QtWidgets import QMenu

    from src.ui import ai_chat

    app, plugin, chat = _chat(monkeypatch, api)
    menus: list[list[str]] = []
    wanted: list[str] = []

    def fake_exec(menu, *_args):
        menus.append([a.text() for a in menu.actions()])
        for action in menu.actions():
            if action.text() in wanted:
                action.trigger()

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    try:
        chat.show()  # the fetched list only pops up while the card is open
        wanted[:] = ["DeepSeek-V4.1-Flash  (deepseek-flash)  -  sees images"]
        chat._title.clicked.emit()
        _wait(app, lambda: menus)
        assert "m1" in menus[0] and "DeepSeek-V4-Pro  (deepseek-v4-pro)" in menus[0] and "Max" in menus[0]
        wanted[:] = ["High"]
        chat._title.clicked.emit()  # cached: no second GET /models
        assert len(menus) == 2 and sum(s["path"] == "/v1/models" for s in _Api.seen) == 1
        assert (plugin.model, plugin.thinking) == ("deepseek-flash", "high")
        _Api.mode = "thinking"
        chat.insert_text("17*23?")
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        body = _Api.seen[-1]["body"]
        assert body["model"] == "deepseek-flash" and body["reasoning_effort"] == "high"
        row = chat._thread_layout.itemAt(chat._thread_layout.count() - 1).widget()
        toggle = row.findChildren(ai_chat._ClickLabel)[0]
        reasoning = [w for w in row.findChildren(ai_chat.QLabel) if w.text() == "17*20=340, +51=391."][0]
        assert reasoning.isHidden() and toggle.text() == "Show reasoning (18 tokens)"
        toggle.clicked.emit()  # works while the card is hidden too (isVisible() would read False)
        assert not reasoning.isHidden() and toggle.text() == "Hide reasoning"
        toggle.clicked.emit()
        assert reasoning.isHidden()
    finally:
        chat.close()


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


def test_chat_attachments_are_sent_as_text_and_images(monkeypatch: pytest.MonkeyPatch, api: str, tmp_path) -> None:
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
        # Images go as images plus local OCR text; send waits until reading is done
        monkeypatch.setattr(ai_chat.OcrJob, "start", lambda self, _img: self.finished.emit("hello from image"))
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFF)
        chat.attach_image(image, "Screen region")
        assert chat._attachments[-1].detail == "3 words read" and len(chat._chips) == 4
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        text_part, image_part = _Api.seen[0]["body"]["messages"][-1]["content"]
        sent = text_part["text"]
        assert "[File: notes.txt]\nline one" in sent and "hello from image" in sent and "blob.bin" not in sent
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
        assert chat._attachments == [] and chat._chip_box.isHidden()
    finally:
        chat.close()


def test_chip_x_removes_the_attachment_and_it_is_not_sent(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """clicked(bool) used to land in on_remove's `it=item` default, so the X removed nothing."""
    from PyQt6.QtGui import QImage

    from src.ui import ai_chat

    app, _plugin, chat = _chat(monkeypatch, api)
    try:
        monkeypatch.setattr(ai_chat.OcrJob, "start", lambda self, _img: self.finished.emit("secret region text"))
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFF)
        chat.attach_image(image, "Screen region")
        chat._chips[0].findChild(ai_chat.IconButton).click()  # a real click, as the user does
        assert chat._attachments == [] and chat._chips == [] and chat._chip_box.isHidden()
        chat.insert_text("hi")
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        assert "secret region text" not in _Api.seen[0]["body"]["messages"][-1]["content"]
    finally:
        chat.close()


def test_new_chat_clears_thread_draft_and_attachments_even_while_waiting(
        monkeypatch: pytest.MonkeyPatch, api: str, tmp_path) -> None:
    app, _plugin, chat = _chat(monkeypatch, api)
    _Api.mode = "slow"
    try:
        chat.insert_text("question")
        chat.send()
        notes = tmp_path / "notes.txt"
        notes.write_text("x", encoding="utf-8")
        chat.attach_path(notes)
        chat.insert_text("half typed")
        chat._new_btn.click()  # used to do nothing while a reply was pending, and never cleared draft or chips
        assert chat._job is None and chat._history == [] and chat._draft.toPlainText() == ""
        assert chat._attachments == [] and chat._chip_box.isHidden() and chat._panel.isHidden()
        import time

        end = time.monotonic() + 0.8  # outlast the slow reply so its thread is done before chat is destroyed
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert len(_Api.seen) == 1 and chat._history == []  # the late reply never lands in the fresh chat
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


def test_chat_card_hugs_content_and_keeps_its_side_through_save_and_test(
        monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """The card stayed tall (stretched header and input) after Setup closed, and Save and test made it
    jump across the pet as the status line changed its height."""
    from PyQt6.QtCore import QRect

    app, plugin, chat = _chat(monkeypatch, api)
    plugin.provider = "custom"  # Save and test keeps the fake API's URL
    try:
        chat.open_near(QRect(0, 0, 64, 64))
        closed_h = chat.height()
        chat.show_setup()
        setup_h = chat.height()
        assert setup_h > closed_h
        area = app.primaryScreen().availableGeometry()
        # Room above fits the closed card but not Setup, Setup fits below: the old rule flipped sides
        pet = QRect(area.center().x(), area.top() + chat.GAP + closed_h + 20, 64, 64)
        assert area.bottom() - pet.bottom() - chat.GAP > setup_h + 40
        chat.hide()
        chat.new_chat()
        chat.open_near(pet)
        edges: list[int] = []

        def check() -> None:
            app.processEvents()
            assert chat.height() == chat.sizeHint().height()
            edges.append(chat.geometry().top())  # below the pet: its top is the pet-side edge

        check()
        chat.show_setup()
        check()
        chat._save_and_test()
        check()
        _wait(app, lambda: chat._job is None)
        check()
        assert "Connected" in chat._status.text()
        chat._leave_setup_if_ok()
        check()
        assert chat.height() == closed_h
        assert all(abs(edge - (pet.bottom() + 1 + chat.GAP)) <= 1 for edge in edges), edges
    finally:
        chat.close()


def test_web_search_tool_loop_and_fallback(api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    orig = _Api.do_POST

    def tool_then_answer(self) -> None:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] > 1 or "tools" not in json.loads(self.rfile.peek(65536)):
            return orig(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Api.seen.append({"path": self.path, "body": body})
        data = json.dumps({"usage": {"total_tokens": 10}, "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "web_search", "arguments": '{"query": "python 3.14"}'}}]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    monkeypatch.setattr(_Api, "do_POST", tool_then_answer)
    monkeypatch.setattr(ai_assist, "web_search", lambda q: f"result for {q}")
    reply = ask(api, KEY, "m-tools", T)
    assert reply.text == "It is a stack trace." and reply.total_tokens == 52  # both rounds counted
    follow = _Api.seen[1]["body"]["messages"]
    assert follow[-1] == {"role": "tool", "tool_call_id": "c1", "content": "result for python 3.14"}
    assert follow[-2]["tool_calls"][0]["id"] == "c1"


def test_run_tool_rejects_bad_calls_and_anthropic_params() -> None:
    assert ai_assist._run_tool("rm_rf", "{}").startswith("error")
    assert ai_assist._run_tool("web_search", "not json").startswith("error")
    assert ai_assist.PRESETS["anthropic"][1] == "https://api.anthropic.com/v1"


def test_duckduckgo_bot_check_is_an_error_not_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(ai_assist, "load_key", lambda _u: "")
    monkeypatch.setattr(ai_assist._OPENER, "open", lambda *_a, **_k: io.BytesIO(b'<form id="challenge-form">anomaly</form>'))
    assert "Brave Search key" in ai_assist._run_tool("web_search", '{"query": "x"}')


def test_deepseek_search_used_first_when_its_key_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(ai_assist, "load_key", lambda u: "ds-key" if u == ai_assist.PRESETS["deepseek"][1] else "brave")
    monkeypatch.setattr(ai_assist, "_request", lambda *a: seen.append(a) or {"results": [
        {"title": "Py 3.14", "url": "https://python.org", "summary": "Released", "snippet": "short"}, "junk"]})
    assert ai_assist.web_search("py 3.14") == "Py 3.14\nhttps://python.org\nReleased"
    assert seen[0][1:4] == ("ds-key", "/tools/search", {"query": "py 3.14", "count": 5})


def test_brave_search_used_when_key_saved(api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class Brave(BaseHTTPRequestHandler):
        seen: list = []

        def do_GET(self) -> None:  # noqa: N802
            Brave.seen.append((self.path, self.headers.get("X-Subscription-Token")))
            data = json.dumps({"web": {"results": [{"title": "<b>Py</b> 3.14", "url": "https://python.org",
                                                    "description": "Released &amp; out"}, "junk"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Brave)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ai_assist, "BRAVE_URL", f"http://127.0.0.1:{server.server_port}/search")
    monkeypatch.setattr(ai_assist, "load_key", lambda u: "brave-key-123" if u == ai_assist.BRAVE_URL else "")
    try:
        assert ai_assist.web_search("py 3.14") == "Py 3.14\nhttps://python.org\nReleased & out"
    finally:
        server.shutdown()
    assert Brave.seen == [("/search?count=5&q=py+3.14", "brave-key-123")]


def test_chat_answers_after_a_web_search_and_setup_saves_the_search_key(
        monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    app, plugin, chat = _chat(monkeypatch, api)
    saved = []
    monkeypatch.setattr("src.ui.ai_chat.save_key", lambda url, key: saved.append((url, key)) or True)
    monkeypatch.setattr(ai_assist, "web_search", lambda q: f"result for {q}")
    try:
        _Api.mode = "tool-once"
        chat.insert_text("what is the news?")
        chat.send()
        _wait(app, lambda: chat._job is None and len(chat._history) == 2)
        assert chat._history[-1]["content"] == "It is a stack trace."
        assert _Api.seen[-1]["body"]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "result for news"}
        chat.show_setup()
        chat._search_key.setText("brave-key-123")
        chat._save_and_test()
        assert saved[0] == (ai_chat_brave(), "brave-key-123") and chat._search_key.text() == ""
        _wait(app, lambda: chat._job is None)  # the Save and test request finishes before the widget is closed
    finally:
        chat.close()


def ai_chat_brave() -> str:
    from src.ui.ai_chat import BRAVE_URL

    return BRAVE_URL


def test_fetch_page_refuses_private_targets_and_extracts_text() -> None:
    for bad in ("http://example.com/", "https://127.0.0.1/", "https://localhost/admin", "https://[::1]/",
                "https://169.254.169.254/latest/meta-data", "https://192.168.0.1/", "https://u:p@example.com/",
                "file:///C:/Windows/win.ini"):
        with pytest.raises(ValueError):
            ai_assist.check_public_https(bad)
    assert ai_assist._run_tool("fetch_page", '{"url": "https://127.0.0.1/"}').startswith("error: fetch_page failed")
    assert ai_assist._run_tool("fetch_page", "{}").startswith("error")
    page = "<head><title>t</title></head><style>p{}</style><script>evil()</script><p>Hello&nbsp;<b>world</b></p>\n\n<p>2nd</p>"
    assert ai_assist.page_text(page) == "Hello world 2nd"
    assert len(ai_assist.page_text("x " * 20000)) <= ai_assist.MAX_PAGE_CHARS


def test_claude_native_request_pause_turn_sources_and_thinking(api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_assist, "is_anthropic", lambda _u: True)
    url = "data:image/png;base64,AAAA"
    msgs = chat_messages([{"role": "user", "content": "hi", "images": [url]}])
    reply = ask(api, KEY, "claude-sonnet-5-5", msgs, thinking="high")
    first, second = _Api.seen[0], _Api.seen[1]
    assert first["path"] == "/v1/messages" and first["auth"] is None  # x-api-key, never a Bearer header
    body = first["body"]
    assert body["system"].startswith("You are ByteSprite") and body["messages"][0]["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        "cache_control": {"type": "ephemeral"}}
    assert [t["name"] for t in body["tools"]] == ["web_search", "web_fetch"]
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"} and body["output_config"] == {"effort": "high"}
    assert second["body"]["messages"][-1]["role"] == "assistant"  # the paused turn is sent back to continue
    assert reply.text == "Answer.\n\nSources:\n- Py: https://python.org"
    assert (reply.model, reply.reasoning, reply.prompt_tokens, reply.total_tokens, reply.reasoning_tokens) == (
        "claude-sonnet-5-5", "hm", 20, 30, 4)
    assert body["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}  # prompt cache breakpoint
    ask(api, KEY, "claude-sonnet-5-5", chat_messages([{"role": "user", "content": "plain"}]))
    assert _Api.seen[2]["body"]["messages"][-1]["content"] == [
        {"type": "text", "text": "plain", "cache_control": {"type": "ephemeral"}}]
    assert "thinking" not in _Api.seen[2]["body"] and "output_config" not in _Api.seen[2]["body"]


def test_setup_remove_buttons_delete_only_their_key(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    _app, plugin, chat = _chat(monkeypatch, api)
    saved = {api: KEY, ai_assist.BRAVE_URL: KEY}
    monkeypatch.setattr("src.ui.ai_chat.load_key", lambda url: saved.get(url, ""))
    monkeypatch.setattr("src.ui.ai_chat.delete_key", lambda url: saved.pop(url, None) is not None)
    chat._load_setup()
    assert chat._remove_key.isEnabled() and chat._remove_search.isEnabled()
    chat._remove_search.click()
    assert list(saved) == [api] and not chat._remove_search.isEnabled() and chat._remove_key.isEnabled()
    assert chat._search_key.placeholderText() == "Optional"
    chat._remove_key.click()
    assert saved == {} and chat._key.placeholderText() == "Paste API key"


def test_header_and_reply_line_name_the_version_behind_an_alias(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    from src.ui.ai_chat import reply_details

    _app, plugin, chat = _chat(monkeypatch, api)
    plugin.model = "deepseek-flash"
    chat._models[plugin.base_url] = [ai_assist.ModelInfo("deepseek-flash", "DeepSeek-V4.1-Flash")]
    chat._refresh_title()
    assert chat._title.text().startswith("DeepSeek-V4.1-Flash  ") and "requested as deepseek-flash" in chat._title.toolTip()
    reply = ai_assist.AiReply(text="hi", model="deepseek-flash", total_tokens=3, prompt_tokens=1, completion_tokens=2)
    line = reply_details(reply, "deepseek-flash", "off", name=chat._model_name("deepseek-flash"))
    assert line.startswith("deepseek-flash (DeepSeek-V4.1-Flash)  -  3 tokens")
