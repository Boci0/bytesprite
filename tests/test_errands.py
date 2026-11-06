"""Errands: trigger parsing, local checks, the plugin's limits and persistence, and how a fired one reaches the chat."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools import errands as er
from src.tools.errands import ERRAND_MARK, Errand, ErrandsPlugin, Probes, check, parse_when


def _probes(**kw) -> Probes:
    p = Probes(exes=lambda: set(), listening=lambda port: False, title=lambda: "", idle=lambda: 0.0)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _plugin(**kw) -> ErrandsPlugin:
    plugin = ErrandsPlugin()
    plugin.probes = _probes(**kw)
    return plugin


@pytest.mark.parametrize("when", [
    None, {}, {"kind": "nope"}, {"kind": "background_done"}, {"kind": "background_prints", "id": "bg1"},
    {"kind": "app_closes", "exe": "notepad"}, {"kind": "app_closes", "exe": "a/b.exe"},
    {"kind": "port_opens", "port": "80"}, {"kind": "port_opens", "port": 0}, {"kind": "port_opens", "port": True},
    {"kind": "at_time", "time": "25:00"}, {"kind": "at_time", "minutes": 0}, {"kind": "at_time", "minutes": 99999},
    {"kind": "window_appears", "title": "  "}, {"kind": "file_appears", "pattern": "~/no_such_folder_x/*.zip"},
    {"kind": "file_appears", "pattern": "~/report.zip"},
])
def test_parse_when_refuses_bad_triggers(when) -> None:
    with pytest.raises(ValueError):
        parse_when(when)


def test_parse_when_cleans_good_triggers(tmp_path: Path) -> None:
    assert parse_when({"kind": "background_done", "id": " bg2 "}) == ("background_done", {"id": "bg2"})
    assert parse_when({"kind": "app_closes", "exe": "Setup.EXE"}) == ("app_closes", {"exe": "setup.exe"})
    assert parse_when({"kind": "at_time", "minutes": 5}) == ("at_time", {"minutes": 5})
    kind, arg = parse_when({"kind": "file_appears", "pattern": str(tmp_path / "*.zip")})
    assert kind == "file_appears" and arg["folder"] == str(tmp_path) and arg["glob"] == "*.zip"
    assert parse_when({"kind": "next_startup", "junk": 1}) == ("next_startup", {})


def test_background_errands_need_a_live_process_and_fire_on_exit_or_output() -> None:
    state = {"code": None, "text": "old\n"}
    bg = lambda name, since: (state["code"], False, state["text"][since:], len(state["text"])) if name == "bg1" else None  # noqa: E731
    plugin = _plugin(bg=bg)
    with pytest.raises(ValueError, match="no background command"):
        plugin.add({"kind": "background_done", "id": "bg9"}, "x", "sid")
    done = plugin.add({"kind": "background_done", "id": "bg1"}, "fix it", "sid")
    prints = plugin.add({"kind": "background_prints", "id": "bg1", "text": "Ready"}, "open it", "sid")
    assert prints.base == {"offset": 4}
    plugin.poll()
    assert not plugin.fired
    state["text"] += "server ready on :3000\n"
    plugin.poll()
    assert plugin.fired == [(prints, "bg1 printed 'Ready'. Its newest output:\nserver ready on :3000")]
    state["code"] = 1
    plugin.poll()
    assert plugin.fired[1][0] is done and "exited with code 1" in plugin.fired[1][1] and "old" in plugin.fired[1][1]
    assert plugin.errands == []


def test_prints_errand_ignores_output_from_before_it_was_set() -> None:
    errand = Errand("e1", "background_prints", {"id": "bg1", "text": "ready"}, "x", base={"offset": 10})
    seen = []
    bg = lambda name, since: seen.append(since) or (None, False, "", 10)  # noqa: E731
    assert check(errand, _probes(bg=bg), 0) is None and seen == [10]


def test_file_errand_waits_for_a_new_settled_file(tmp_path: Path) -> None:
    (tmp_path / "old.zip").write_bytes(b"1")
    plugin = _plugin()
    errand = plugin.add({"kind": "file_appears", "pattern": str(tmp_path / "*.zip")}, "unzip", "sid")
    assert errand.base == {"names": ["old.zip"]}
    (tmp_path / "new.zip.crdownload").write_bytes(b"12")
    plugin.poll()
    (tmp_path / "new.zip").write_bytes(b"123")
    plugin.poll()  # seen once: may still be writing
    assert not plugin.fired
    plugin.poll()
    assert plugin.fired[0][1] == f"new in {tmp_path}: new.zip (3 bytes)"


def test_app_port_window_time_and_return_triggers() -> None:
    exes = {"setup.exe"}
    plugin = _plugin(exes=lambda: exes)
    with pytest.raises(ValueError, match="not running"):
        plugin.add({"kind": "app_closes", "exe": "other.exe"}, "x", "sid")
    app = plugin.add({"kind": "app_closes", "exe": "setup.exe"}, "x", "sid")
    plugin.poll()
    exes.clear()
    plugin.poll()
    assert plugin.fired[-1] == (app, "setup.exe is no longer running")

    assert check(Errand("e", "port_opens", {"port": 3000}, "x"), _probes(listening=lambda p: p == 3000), 0)
    window = Errand("e", "window_appears", {"title": "sign in"}, "x")
    assert check(window, _probes(title=lambda: "Bank - Sign in"), 0) is None  # private titles never match
    assert check(Errand("e", "window_appears", {"title": "build"}, "x"), _probes(title=lambda: "Build OK"), 0)

    soon = _plugin().add({"kind": "at_time", "minutes": 2}, "x", "sid", now=1000.0)
    assert soon.base == {"due": 1120.0}
    assert check(soon, _probes(), 1119) is None and check(soon, _probes(), 1120) == "it is time"
    now = datetime(2026, 11, 6, 18, 30).timestamp()
    late = _plugin().add({"kind": "at_time", "time": "08:00"}, "x", "sid", now=now)
    assert datetime.fromtimestamp(late.base["due"]) == datetime(2026, 11, 7, 8, 0)  # already past: tomorrow

    idle = {"s": 10.0}
    back = Errand("e", "user_returns", {}, "x")
    probes = _probes(idle=lambda: idle["s"])
    assert check(back, probes, 0) is None  # never away yet
    idle["s"] = er.AWAY_S + 1
    assert check(back, probes, 0) is None
    idle["s"] = 2.0
    assert check(back, probes, 0) == "the user is back at the PC"


def test_plugin_limits_expiry_budget_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    with pytest.raises(ValueError, match="instruction"):
        plugin.add({"kind": "user_returns"}, "  ", "sid")
    with pytest.raises(ValueError, match="under"):
        plugin.add({"kind": "user_returns"}, "x" * (er.MAX_ERRAND_CHARS + 1), "sid")
    for _ in range(er.MAX_ERRANDS):
        plugin.add({"kind": "user_returns"}, "hi", "sid", now=0.0)
    with pytest.raises(ValueError, match="already waiting"):
        plugin.add({"kind": "user_returns"}, "hi", "sid")
    assert plugin.cancel("e3").startswith("cancelled e3") and plugin.cancel("e3").startswith("error")
    plugin.poll(now=er.ERRAND_HOURS * 3600 + 1)
    assert plugin.errands == [] and plugin.fired == []  # expired, never fired
    assert all(plugin.take_run() for _ in range(er.MAX_ERRAND_RUNS)) and not plugin.take_run()
    assert plugin.execute_action("errands:cancel_all")[0]


def test_a_probe_error_never_fires_an_errand() -> None:
    def boom() -> set[str]:
        raise OSError("denied")

    plugin = _plugin()
    plugin.errands.append(Errand("e1", "app_closes", {"exe": "a.exe"}, "x", expires=time.time() + 60))
    plugin.probes.exes = boom
    plugin.poll()
    assert plugin.fired == [] and len(plugin.errands) == 1


def test_persistence_keeps_waiting_drops_background_and_fires_startup(tmp_path: Path) -> None:
    plugin = _plugin(bg=lambda n, s: (None, False, "", 0))
    plugin.add({"kind": "background_done", "id": "bg1"}, "a", "s1", now=100.0)
    plugin.add({"kind": "file_appears", "pattern": str(tmp_path / "*.zip")}, "b", "s2", now=100.0)
    plugin.add({"kind": "next_startup"}, "why did it crash?", "s3", now=100.0)
    plugin.runs, plugin.done = 3, 2
    saved = plugin.to_dict()
    again = ErrandsPlugin()
    again.load_dict(saved, now=200.0)
    assert [e.kind for e in again.errands] == ["file_appears"] and again.errands[0].sid == "s2"
    assert [(e.instruction, ev.startswith("Paperduck just started")) for e, ev in again.fired] == [
        ("why did it crash?", True)]
    assert again.runs == 3 and again.done == 2 and again.add({"kind": "user_returns"}, "x", "s").id == "e4"
    broken = ErrandsPlugin()
    broken.load_dict({"errands": [{"kind": "at_time"}, "junk", {"id": 1}]})
    assert broken.errands == [] and broken.fired == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows background commands")
def test_background_peek_keeps_the_models_read_cursor(tmp_path: Path) -> None:
    from src.tools.ai_tools import BackgroundProcs

    procs = BackgroundProcs()
    try:
        procs.start(tmp_path, "echo one")
        end = time.monotonic() + 10
        while (got := procs.peek("bg1")) is not None and got[0] is None and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.2)
        code, stopped, text, offset = procs.peek("bg1")
        assert code == 0 and not stopped and "one" in text and offset >= 3
        assert procs.peek("bg1", offset)[2] == "" and procs.peek("nope") is None
        assert "one" in procs.read("bg1")  # peeking did not use up what the model has not read
    finally:
        procs.stop_all()


def test_tools_guard_and_body_language() -> None:
    from src.tools.ai_assist import read_local_data
    from src.tools.ai_tools import APPROVED_TOOLS, TOOL_SPECS
    from src.ui.pet_window import activity_clip

    names = {s["function"]["name"] for s in TOOL_SPECS}
    assert {"set_errand", "cancel_errand"} <= names
    assert "set_errand" in APPROVED_TOOLS and "cancel_errand" not in APPROVED_TOOLS
    assert read_local_data({"role": "user", "content": ERRAND_MARK + ": when bg1 finishes..."})
    assert not read_local_data({"role": "user", "content": "hello"})
    assert [activity_clip(t) for t in ("approval", "run_command", "web_search", "github__list", "read_file", "")] == [
        "wait", "type", "scan", "scan", "focus", "focus"]


def test_fired_errand_runs_in_the_chat_that_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QApplication

    from src.tools import ai_assist
    from src.ui import ai_chat
    from src.ui.ai_tool_host import APPROVE_TIMEOUT_S, ERRAND_APPROVE_S

    app = QApplication.instance() or QApplication([])  # noqa: F841
    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    host = SimpleNamespace(specs=[], run=lambda n, a: "", procs=None, approve_s=APPROVE_TIMEOUT_S)
    chat = ai_chat.AiChat(plugin, tool_host=host)
    try:
        started = []
        monkeypatch.setattr(chat, "_start_job", lambda **kw: started.append(kw["history"]))
        chat._history += [{"role": "user", "content": "build it"}, {"role": "assistant", "content": "started bg1"}]
        sid = chat.ensure_sid()
        chat._save_session()
        chat.new_chat()  # the user moved on to another chat
        errand = Errand("e1", "background_done", {"id": "bg1"}, "read the failures and fix them", sid)
        assert chat.run_errand(errand, "bg1 exited with code 1. Its last output:\nFAILED test_x")
        sent = started[-1][-1]["content"]
        assert chat._sid == sid and started[-1][0]["content"] == "build it"  # resumed with its context
        assert sent.startswith(ERRAND_MARK + ": when bg1 finishes") and "<data>\nbg1 exited" in sent
        assert sent.endswith("Errand: read the failures and fix them") and host.approve_s == ERRAND_APPROVE_S
        chat._job = object()
        assert chat.run_errand(errand, "x") is False  # never interleaves with a pending answer
        chat._job = None
        chat._on_done(None, "stopped")
        assert host.approve_s == APPROVE_TIMEOUT_S
        chat.new_chat()
        chat.resume(sid)  # the saved errand turn reads as the instruction, not the raw evidence
        texts = [t.toPlainText() for t in chat.findChildren(ai_chat.ChatText)]
        assert "Errand: read the failures and fix them" in texts and not any("<data>" in t for t in texts)
    finally:
        chat.close()
