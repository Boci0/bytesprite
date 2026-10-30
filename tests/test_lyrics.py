"""Lyrics plugin: LRC parsing, line lookup and tick logic with a fake media-session reader."""

from __future__ import annotations

import time

from src.tools.lyrics import LyricsPlugin, line_at, parse_lrc


class _FakeReader:
    alive = True

    def __init__(self, snap: dict) -> None:
        self.snap = snap

    def latest(self) -> dict:
        return self.snap

    def close(self) -> None:
        pass


def _playing(pos_ms: float) -> dict:
    return {"title": "Song", "artist": "Band", "status": "Playing", "pos": pos_ms, "at": time.time() * 1000}


def test_parse_lrc_handles_repeats_and_sorting() -> None:
    lines = parse_lrc("[00:05.00][00:09.50] chorus\n[00:01.50] intro\nno stamp\n[01:00.00]")
    assert lines == [(1500, "intro"), (5000, "chorus"), (9500, "chorus"), (60000, "")]


def test_line_at_boundaries() -> None:
    lines = [(1000, "a"), (2000, "b")]
    assert line_at(lines, 0) == ""
    assert line_at(lines, 1000) == "a"
    assert line_at(lines, 99999) == "b"
    assert line_at([], 500) == ""


def test_plugin_is_off_by_default_and_ignores_ticks() -> None:
    plugin = LyricsPlugin()
    assert plugin.tick() is False
    assert plugin.line == ""
    assert plugin.busy is False


def test_plugin_shows_line_for_playing_track_and_clears_on_pause() -> None:
    plugin = LyricsPlugin()
    plugin.load_dict({"enabled": True})
    reader = _FakeReader(_playing(2500))
    plugin._reader = reader  # type: ignore[assignment]
    plugin._track = ("Song", "Band")
    plugin._lines = [(1000, "first"), (2000, "second"), (5000, "third")]
    assert plugin.tick() is True
    assert plugin.line == "second"
    assert plugin.busy is True

    reader.snap = {**reader.snap, "status": "Paused"}
    assert plugin.tick() is True
    assert plugin.line == ""


def test_plugin_persists_enabled_flag_and_toggle_stops_reader() -> None:
    plugin = LyricsPlugin()
    handled, toast, _ = plugin.execute_action("lyrics:toggle")
    assert handled and toast == "LYRICS ON"
    assert plugin.to_dict() == {"enabled": True}
    plugin.execute_action("lyrics:toggle")
    assert plugin.to_dict() == {"enabled": False}
    assert plugin.execute_action("other")[0] is False
