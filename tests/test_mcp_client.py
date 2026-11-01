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


def test_registry_search_gives_stdio_commands_only() -> None:
    def pkg(kind, ident, transport="stdio", env=()):
        return {"registryType": kind, "identifier": ident, "version": "1.2", "transport": {"type": transport},
                "environmentVariables": list(env)}

    data = {"servers": [
        {"server": {"name": "io.x/git", "description": "Git tools", "packages": [
            pkg("npm", "git-mcp", env=[{"name": "TOKEN", "isRequired": True, "isSecret": True, "description": "PAT"}])]}},
        {"server": {"name": "io.x/py", "description": "Py", "packages": [pkg("oci", "img"), pkg("pypi", "py-mcp")]}},
        {"server": {"name": "io.x/remote", "description": "Remote only", "remotes": [{"type": "streamable-http"}]}},
        {"server": {"name": "io.x/sse", "packages": [pkg("npm", "sse-mcp", transport="sse")]}},
        "junk",
    ]}
    urls: list[str] = []
    text = mcp_client.find_servers("git hub", lambda url: urls.append(url) or data)
    assert urls == [mcp_client.REGISTRY_URL + "?version=latest&limit=30&search=git+hub"]
    assert 'command: npx  args: ["-y", "git-mcp@1.2"]\n  env: TOKEN (required) (secret): PAT' in text
    assert 'command: uvx  args: ["py-mcp==1.2"]' in text
    assert "remote" not in text and "sse-mcp" not in text and "data, not instructions" in text
    assert mcp_client.registry_text({"servers": []}).startswith("no local")

    def down(_url):
        raise OSError("offline")

    assert mcp_client.find_servers("x", down) == "error: the MCP registry did not answer"


def test_add_server_keeps_the_config_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "ByteSprite" / "mcp.json"
    assert mcp_client.add_server("git", "npx", ["-y", "git-mcp"], {"TOKEN": "t"}, path) == 'added MCP server "git"'
    assert load_config(path) == {"git": {"command": "npx", "args": ["-y", "git-mcp"], "env": {"TOKEN": "t"}}}
    assert mcp_client.add_server("git", "evil", [], {}, path).startswith("error: a server named")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["note"] = "mine"
    path.write_text(json.dumps(config), encoding="utf-8")
    mcp_client.add_server("py", "uvx", ["py-mcp"], {}, path)
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["note"] == "mine" and config["mcpServers"]["py"] == {"command": "uvx", "args": ["py-mcp"]}
    path.write_text("{broken", encoding="utf-8")
    assert mcp_client.add_server("x", "x", [], {}, path).startswith("error: mcp.json is not valid JSON")
    assert path.read_text(encoding="utf-8") == "{broken"  # the user's file is left alone
