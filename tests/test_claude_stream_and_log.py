from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.storage import storage_manager
from src.tools import ai_assist


def _events(*events: dict, end: bool = True) -> list[bytes]:
    lines = [b"event: x\n", *[b"data: " + json.dumps(e).encode() + b"\n\n" for e in events]]
    return lines + ([b'data: {"type": "message_stop"}\n'] if end else [])


def _start(index: int, block: dict) -> dict:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: dict) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def test_claude_stream_is_rebuilt_into_a_plain_reply() -> None:
    seen: list[str] = []
    lines = _events(
        {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 7, "output_tokens": 1}}},
        _start(0, {"type": "thinking", "thinking": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "hm"}),
        _delta(0, {"type": "signature_delta", "signature": "sig"}),
        {"type": "content_block_stop", "index": 0},
        _start(1, {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}),
        _delta(1, {"type": "input_json_delta", "partial_json": '{"query":'}),
        _delta(1, {"type": "input_json_delta", "partial_json": '"cats"}'}),
        {"type": "content_block_stop", "index": 1},
        _start(2, {"type": "text", "text": ""}),
        _delta(2, {"type": "text_delta", "text": "Hel"}),
        _delta(2, {"type": "text_delta", "text": "lo"}),
        _delta(2, {"type": "citations_delta", "citation": {"url": "https://a.example", "title": "A"}}),
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}})
    data = ai_assist._read_anthropic_stream(lines, seen.append)
    assert data["model"] == "claude-x" and data["stop_reason"] == "end_turn"
    assert data["usage"] == {"input_tokens": 7, "output_tokens": 12}
    assert data["content"] == [
        {"type": "thinking", "thinking": "hm", "signature": "sig"},
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "cats"}},
        {"type": "text", "text": "Hello", "citations": [{"url": "https://a.example", "title": "A"}]}]
    assert seen[0].startswith("Thinking") and "Preparing web_search" in seen and seen[-1] == "Writing, 5 characters"


def test_claude_stream_without_message_stop_is_a_dropped_connection() -> None:
    with pytest.raises(ai_assist._Transient):
        ai_assist._read_anthropic_stream(_events(_start(0, {"type": "text", "text": ""}), end=False), lambda _s: None)


def test_claude_stream_errors_and_bad_events() -> None:
    with pytest.raises(ai_assist._Transient):
        ai_assist._read_anthropic_stream(_events({"type": "error", "error": {"type": "overloaded_error"}}), lambda _s: None)
    with pytest.raises(ValueError, match="bad reply"):
        ai_assist._read_anthropic_stream(_events({"type": "error", "error": {"type": "invalid_request_error"}}), lambda _s: None)
    odd = _events(_delta(5, {"type": "text_delta", "text": "x"}), {"type": "content_block_start", "index": "0"},
                  _start(0, {"type": "tool_use", "id": "t", "name": "n", "input": {}}),
                  _delta(0, {"type": "input_json_delta", "partial_json": "{broken"}))
    assert ai_assist._read_anthropic_stream(odd, lambda _s: None)["content"][0]["input"] == {}


def test_claude_stream_size_cap() -> None:
    chunk = _delta(0, {"type": "text_delta", "text": "x" * 65536})
    lines = _events(_start(0, {"type": "text", "text": ""}), *[chunk] * (ai_assist.MAX_RESPONSE_BYTES // 65536 + 2))
    with pytest.raises(ValueError, match="reply too large"):
        ai_assist._read_anthropic_stream(lines, lambda _s: None)


def test_log_text_joins_rotated_file_and_keeps_the_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert storage_manager.log_text() == ""
    path = storage_manager.log_path()
    path.parent.mkdir(parents=True)
    path.with_name(path.name + ".1").write_text("old\n", encoding="utf-8")
    path.write_text("new\n", encoding="utf-8")
    assert storage_manager.log_text() == "old\nnew\n"
    assert storage_manager.log_text(5) == "\nnew\n"[-5:]
