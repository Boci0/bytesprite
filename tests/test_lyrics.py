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


def test_plugin_retries_a_missed_lookup(monkeypatch) -> None:
    import src.tools.lyrics as lyrics

    calls: list[float] = []

    def fake_fetch(title: str, artist: str, duration_s: float) -> list[tuple[int, str]]:
        calls.append(duration_s)
        return [(0, "found")] if len(calls) > 1 else []  # first lookup misses (stale duration / timeout)

    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", fake_fetch)
    monkeypatch.setattr(lyrics.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    plugin = LyricsPlugin()
    plugin.load_dict({"enabled": True})
    plugin._reader = _FakeReader({**_playing(500), "dur": 200000})  # type: ignore[assignment]
    plugin.tick()
    assert plugin._lines == [] and plugin.line == ""
    plugin.tick()
    assert len(calls) == 1  # no hammering before the retry delay
    plugin._retry_at = 0.0
    plugin.tick()
    assert len(calls) == 2 and plugin.line == "found"


def test_parse_lrc_strips_word_timing() -> None:
    assert parse_lrc("[00:00.930]<00:00.930>Whole <00:01.230>heap<00:02.880>") == [(930, "Whole heap")]


def test_pear_cached_lyrics_uses_newest_id_for_title(monkeypatch, tmp_path) -> None:
    import src.tools.lyrics as lyrics

    (tmp_path / "lyrics_cache").mkdir()
    (tmp_path / "lyrics_cache" / "stream_new.lrc").write_text("[00:01.00]right", encoding="utf-8")
    (tmp_path / "lyrics_cache" / "stream_old.lrc").write_text("[00:01.00]stale", encoding="utf-8")
    (tmp_path / "peerm_debug.log").write_text(
        '[1] [player] === playSong START === token=1 song="A \\"B\\" - C" id=stream_old source=stream\n'
        '[2] [player] === playSong START === token=2 song="A \\"B\\" - C" id=stream_new source=stream\n',
        encoding="utf-8")
    monkeypatch.setattr(lyrics, "_PEAR_DIR", tmp_path)
    assert lyrics.pear_cached_lyrics('A \\"B\\" - C') == [(1000, "right")]
    assert lyrics.pear_cached_lyrics("Unknown") == []
    monkeypatch.setattr(lyrics, "_PEAR_DIR", tmp_path / "missing")
    assert lyrics.pear_cached_lyrics("A") == []


def test_late_pear_cache_is_picked_up_without_waiting_for_retry(monkeypatch, tmp_path) -> None:
    import src.tools.lyrics as lyrics

    (tmp_path / "lyrics_cache").mkdir()
    (tmp_path / "peerm_debug.log").write_text(
        '[1] [player] === playSong START === token=1 song="Song" id=stream_x source=stream\n', encoding="utf-8")
    monkeypatch.setattr(lyrics, "_PEAR_DIR", tmp_path)
    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", lambda *_a: [])  # LRCLIB has nothing
    monkeypatch.setattr(lyrics.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    plugin = LyricsPlugin()
    plugin.load_dict({"enabled": True})
    plugin._reader = _FakeReader(_playing(1500))  # type: ignore[assignment]
    plugin.tick()
    assert plugin.line == "" and plugin.loading

    # Pear writes its .lrc seconds later; the next 1s poll finds it, long before the 8s LRCLIB retry
    (tmp_path / "lyrics_cache" / "stream_x.lrc").write_text("[00:01.00]late line\n[00:03.00]next", encoding="utf-8")
    plugin._pear_check_at = 0.0
    plugin._reader.snap = _playing(2000)  # type: ignore[attr-defined]
    plugin.tick()
    assert plugin.line == "late line" and not plugin.loading


def test_pear_track_prefers_pear_lyrics_over_an_lrclib_guess(monkeypatch, tmp_path) -> None:
    import src.tools.lyrics as lyrics

    (tmp_path / "lyrics_cache").mkdir()
    (tmp_path / "peerm_debug.log").write_text(
        '[1] [player] === playSong START === token=1 song="Song" id=stream_y source=stream\n', encoding="utf-8")
    monkeypatch.setattr(lyrics, "_PEAR_DIR", tmp_path)
    calls: list[str] = []

    def wrong_song(title: str, artist: str, duration_s: float) -> list[tuple[int, str]]:
        calls.append(title)
        return [(0, "another artist's song")]  # same name, wrong artist

    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", wrong_song)
    monkeypatch.setattr(lyrics.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    plugin = LyricsPlugin()
    plugin.load_dict({"enabled": True})
    plugin._reader = _FakeReader({**_playing(1500), "app": "com.peerm.app"})  # type: ignore[assignment]
    plugin.tick()
    assert calls == [] and plugin.loading  # Pear track: wait for Pear's own lyrics first

    plugin._lrclib_at = 0.0  # grace over, Pear still has nothing: fall back to LRCLIB
    plugin.tick()
    assert calls == ["Song"] and plugin.line == "another artist's song"

    # Pear's lyrics land later and replace the guess
    (tmp_path / "lyrics_cache" / "stream_y.lrc").write_text("[00:01.00]the real line", encoding="utf-8")
    plugin._pear_check_at = 0.0
    plugin.tick()
    assert plugin.line == "the real line"


def test_browser_and_long_sessions_are_not_songs(monkeypatch) -> None:
    import src.tools.lyrics as lyrics_mod

    monkeypatch.setattr(lyrics_mod, "fetch_synced_lyrics", lambda *_a: [])
    monkeypatch.setattr(lyrics_mod, "pear_lyrics_path", lambda _t: None)
    plugin = LyricsPlugin()
    plugin.load_dict({"enabled": True})
    reader = _FakeReader({**_playing(1000), "app": "MSEdge", "dur": 240000})
    plugin._reader = reader  # type: ignore[assignment]
    plugin.tick()
    assert not plugin.playing and plugin.title == "" and not plugin.loading
    reader.snap = {**_playing(1000), "app": "308046B0AF4A39CB", "dur": 3876341}  # Firefox, 64 min video
    plugin.tick()
    assert not plugin.playing and plugin.title == ""
    reader.snap = {**_playing(1000), "app": "Spotify.exe", "dur": 240000}
    plugin.tick()
    assert plugin.playing and plugin.title == "Song"


def test_broken_lrclib_reply_is_a_miss_not_a_dead_lookup(monkeypatch, tmp_path) -> None:
    """A non-HTTP reply raised http.client.BadStatusLine out of the lookup thread, so `_lines` stayed
    None: the card showed loading dots and the track was never retried."""
    import socket
    import threading

    from src.tools import lyrics

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(b"NOT-HTTP\r\n\r\n")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(lyrics, "_LRCLIB", f"http://127.0.0.1:{server.getsockname()[1]}/api/")
    monkeypatch.setattr(lyrics, "_PEAR_DIR", tmp_path)
    try:
        assert lyrics.fetch_synced_lyrics("Song", "Artist", 200.0) == []
    finally:
        server.close()
