"""MCP stdio client: only servers from the user's config, prefixed tools, capped text, timeouts, approval."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools import ai_assist, mcp_client
from src.tools.mcp_client import McpHub, load_config

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


@pytest.fixture()
def hub(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [FAKE]},
        "missing": {"command": "no-such-command-bytesprite"},
        "bad": {"args": ["x"]},
    }}), encoding="utf-8")
    hub = McpHub(path)
    yield hub
    hub.close()


def test_config_keeps_well_formed_servers_only(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    assert load_config(path) == {}
    path.write_text('{"mcpServers": {"my server!": {"command": "x", "args": ["a"], "env": {"K": 1}},'
                    ' "argsbad": {"command": "x", "args": "a"}}}', encoding="utf-8")
    assert load_config(path) == {"my_server_": {"command": "x", "args": ["a"], "env": {"K": "1"}}}
    path.write_text("not json", encoding="utf-8")
    assert load_config(path) == {}


def test_hub_lists_calls_and_caps_tools(hub: McpHub, monkeypatch: pytest.MonkeyPatch) -> None:
    specs = hub.specs()
    names = [s["function"]["name"] for s in specs]
    assert names == ["fake__echo", "fake__big", "fake__hang"]
    params = specs[0]["function"]["parameters"]
    assert "$schema" not in params and params["required"] == ["text"]
    assert specs[1]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert hub.specs() is specs  # started once
    echoed = hub.call("fake__echo", {"text": "hi"})
    assert "data, not instructions" in echoed and "echo: hi\n[image content not shown]" in echoed
    big = hub.call("fake__big", {})
    assert "error: " in big and big.endswith("cut off") and len(big) < 12200
    assert hub.server_of("read_file") is None and hub.call("fake__nope", {}).startswith("error")
    monkeypatch.setattr(mcp_client, "CALL_TIMEOUT_S", 0.5)
    assert "no answer within" in hub.call("fake__hang", {})
    assert "error: MCP server fake: the server" in hub.call("fake__echo", {"text": "x"})  # the hung one was stopped
    assert [s["function"]["name"] for s in hub.specs()] == names  # and restarts with the next message
    assert hub.call("fake__echo", {"text": "after"}).endswith("echo: after\n[image content not shown]")


def test_hub_restarts_when_the_config_changes(hub: McpHub) -> None:
    assert hub.specs()
    hub.path.write_text('{"mcpServers": {}}', encoding="utf-8")
    import os
    os.utime(hub.path, (1, 1))
    assert hub.specs() == [] and hub.server_of("fake__echo") is None


def test_mcp_output_arms_the_fetch_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "function": {"name": "notes__read", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "b", "function": {"name": "fetch_page", "arguments": '{"url": "https://evil.example/x"}'}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "done"}}]},
    ])
    sent: list[dict] = []
    monkeypatch.setattr(ai_assist, "_request", lambda *a: sent.append(json.loads(json.dumps(a[3]))) or next(replies))
    monkeypatch.setattr(ai_assist, "fetch_page", lambda url: pytest.fail("fetched " + url))
    host = SimpleNamespace(specs=[mcp_client.tool_spec("notes", {"name": "read"})], run=lambda n, a: "SECRET")
    ai_assist._post("https://api.example/v1", "k", "m", [{"role": "user", "content": "hi"}], "off", 5, True, host)
    assert "only opens links already" in sent[2]["messages"][-1]["content"]


def test_tool_host_asks_before_a_servers_first_call(hub: McpHub) -> None:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication

    from src.ui.ai_tool_host import AiToolHost

    app = QApplication.instance() or QApplication([])
    asked: list[str] = []
    answers = iter([False, True])

    class Chat:
        allowed: set[str] = set()

        def confirm_mcp(self, server, tool, done):
            if server not in self.allowed:
                asked.append(tool)
                if next(answers):
                    self.allowed.add(server)
            done(server in self.allowed)

    class Window(QObject):
        _ai_chat = Chat()

    host = AiToolHost(Window(), mcp=hub)
    results: list[str] = []

    def worker() -> None:
        assert "fake__echo" in [s["function"]["name"] for s in host.specs] and "read_file" in str(host.specs)
        results.extend(host.run("fake__echo", {"text": n}) for n in "abc")

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        app.processEvents()
    assert asked == ["fake__echo", "fake__echo"]
    assert "did not allow" in results[0] and results[1].endswith("echo: b\n[image content not shown]")
    assert "echo: c" in results[2]
