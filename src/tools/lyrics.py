"""Lyrics tool plugin: reads the playing song from Windows media controls and shows its synced LRCLIB line."""

from __future__ import annotations

import base64
import bisect
import http.client
import json
import os
from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_LRC_WORD = re.compile(r"<\d+:\d+(?:\.\d+)?>")  # per-word timing in Pear's cache
_LRCLIB = "https://lrclib.net/api/"
_USER_AGENT = "ByteSprite/0.9"
_MAX_BYTES = 512 * 1024
_MAX_LINE_CHARS = 90
_MAX_RETRIES = 3
# Pear Music caches the lyrics it shows as lyrics_cache/<id>.lrc; its debug log maps the song title to <id>
_PEAR_DIR = Path(os.environ.get("APPDATA", "")) / "com.peerm" / "peerm_app"
_PEAR_PLAY = re.compile(r'playSong START === .*? song="(.*)" id=(\S+) ')
_PEAR_LOG_TAIL = 256 * 1024
_RETRY_S = 8.0
# Pear writes <id>.lrc 10..65s after a song starts, so its cache is re-checked this often until it lands
_PEAR_POLL_S = 1.0
# Pear tracks wait this long for Pear's own lyrics (what Pear displays, matched on its real title and artist)
# before ByteSprite asks LRCLIB itself: Pear radio titles reach SMTC with the placeholder artist "Pear Radio",
# so an LRCLIB guess can be another artist's song of the same name
_PEAR_GRACE_S = 12.0
_LOADING_MAX_S = 45.0  # stop the "looking up" dots after this; most songs that have lyrics are found by then
# Browser media sessions are YouTube videos and streams, not songs (music goes through Pear or a desktop
# player). Firefox reports a hashed app id, so long sessions are also skipped by length.
_BROWSER_APP_RE = re.compile(r"msedge|chrome|firefox|brave|opera|vivaldi", re.IGNORECASE)
_MAX_SONG_S = 15 * 60


def _is_song(snap: dict[str, Any]) -> bool:
    if _BROWSER_APP_RE.search(str(snap.get("app") or "")):
        return False
    try:
        return float(snap.get("dur") or 0) / 1000 <= _MAX_SONG_S
    except (TypeError, ValueError):
        return True

# One long-lived PowerShell prints a JSON line every 300ms; spawning it per poll would cost far more.
# Prefers Pear Music (peerm_app.exe) and falls back to whatever Windows treats as the current session.
_SMTC_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$tMgr = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$tProps = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); [void]$t.Wait(-1); $t.Result }
$mgr = Await ($tMgr::RequestAsync()) $tMgr
while ($true) {
    $line = '{}'
    try {
        $s = $mgr.GetSessions() | Where-Object { $_.SourceAppUserModelId -match 'peerm|pear' } | Select-Object -First 1
        if (-not $s) { $s = $mgr.GetCurrentSession() }
        if ($s) {
            $p = Await ($s.TryGetMediaPropertiesAsync()) $tProps
            $tl = $s.GetTimelineProperties()
            $line = @{
                title  = $p.Title
                artist = $p.Artist
                status = [string]$s.GetPlaybackInfo().PlaybackStatus
                pos    = $tl.Position.TotalMilliseconds
                dur    = $tl.EndTime.TotalMilliseconds
                at     = $tl.LastUpdatedTime.ToUnixTimeMilliseconds()
                app    = $s.SourceAppUserModelId
            } | ConvertTo-Json -Compress
        }
    } catch {}
    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 300
}
"""


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """Parse LRC text into sorted (milliseconds, line) pairs; blank lines are kept as pauses."""
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stamps = _LRC_TIME.findall(raw)
        if not stamps:
            continue
        lyric = _LRC_WORD.sub("", _LRC_TIME.sub("", raw)).strip()
        for minutes, seconds in stamps:
            out.append((int((int(minutes) * 60 + float(seconds)) * 1000), lyric))
    out.sort(key=lambda pair: pair[0])
    return out


def line_at(lines: list[tuple[int, str]], position_ms: float) -> str:
    """Return the lyric active at `position_ms`, or '' before the first line."""
    idx = bisect.bisect_right(lines, position_ms, key=lambda pair: pair[0]) - 1
    return lines[idx][1] if idx >= 0 else ""


def pear_lyrics_path(title: str) -> Path | None:
    """Where Pear Music caches (or will cache) the lyrics for `title`; None if the log never played it."""
    try:
        with open(_PEAR_DIR / "peerm_debug.log", "rb") as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - _PEAR_LOG_TAIL))
            tail = log.read().decode("utf-8", "replace")
    except OSError:
        return None
    ids = [song_id for song, song_id in _PEAR_PLAY.findall(tail) if song == title]
    return _PEAR_DIR / "lyrics_cache" / f"{ids[-1]}.lrc" if ids else None


def pear_cached_lyrics(title: str) -> list[tuple[int, str]]:
    """Synced lyrics Pear Music already found for `title` (any of its providers); [] when not cached."""
    path = pear_lyrics_path(title)
    if path is None:
        return []
    try:
        return parse_lrc(path.read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return []


def _fetch_json(path: str, params: dict[str, str]) -> Any:
    req = urllib.request.Request(
        f"{_LRCLIB}{path}?{urllib.parse.urlencode(params)}", headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310 - fixed https lrclib.net base
        return json.loads(resp.read(_MAX_BYTES))


def fetch_synced_lyrics(title: str, artist: str, duration_s: float = 0.0) -> list[tuple[int, str]]:
    """Pear Music's own lyrics cache first, then LRCLIB; [] when neither has synced lyrics.

    Pear radio tracks arrive as title "Song - Channel" with the placeholder artist "Pear Radio", so those
    are also searched by the leading "Song" part, accepting only an exact track name of similar length.
    """
    cached = pear_cached_lyrics(title)
    if cached:
        return cached
    clean = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title
    head, _, tail = clean.partition(" - ")
    head = head.strip()
    generic = artist.strip().lower() in ("", "pear radio")
    # (path, params, require the result's track name to equal `head`)
    attempts: list[tuple[str, dict[str, str], bool]] = []
    if not generic:
        attempts += [("get", {"track_name": title, "artist_name": artist}, False),
                     ("search", {"q": f"{clean} {artist}"}, False)]
    if tail.strip():
        channel = re.split(r"[-\s]+", tail.strip())[-1]
        attempts += [("search", {"q": f"{head} {channel}"}, True), ("search", {"q": head}, True)]
    def run(attempt: tuple[str, dict[str, str], bool]) -> list[tuple[int, str]]:
        path, params, exact = attempt
        try:
            data = _fetch_json(path, params)
        except (OSError, ValueError, http.client.HTTPException):  # e.g. BadStatusLine, IncompleteRead
            return []
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            synced = item.get("syncedLyrics")
            if not isinstance(synced, str) or not synced.strip():
                continue
            if exact and str(item.get("trackName", "")).strip().lower() != head.lower():
                continue
            length = item.get("duration")
            if duration_s > 0 and isinstance(length, (int, float)) and abs(length - duration_s) > 5:
                continue
            return parse_lrc(synced)
        return []

    if not attempts:
        return []
    # All attempts run at once (a miss costs seconds); results are read in priority order
    with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
        for found in pool.map(run, attempts):
            if found:
                return found
    return []


class _SmtcReader:
    """Owns the PowerShell child and keeps only its newest snapshot."""

    def __init__(self) -> None:
        encoded = base64.b64encode(_SMTC_SCRIPT.encode("utf-16-le")).decode("ascii")
        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
        self._snap: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            self._snap = data if isinstance(data, dict) else {}

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def latest(self) -> dict[str, Any]:
        return self._snap

    def close(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass


class LyricsPlugin:
    """Shows the current synced lyric line of the song playing in Pear Music (or any SMTC player).

    Off by default: enabling it starts a PowerShell child and sends the song title and artist to lrclib.net.
    """

    SUBMENU_NAME: str = "Lyrics"

    def __init__(self) -> None:
        self._enabled = False
        self._reader: _SmtcReader | None = None
        self._track: tuple[str, str] = ("", "")
        self._lines: list[tuple[int, str]] | None = None  # None while the lookup is running
        self._playing = False
        self._position_ms = 0.0
        self._line = ""
        self._cache: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._retries = 0
        self._retry_at = 0.0
        self._track_started = 0.0
        self._pear_path: Path | None = None
        self._pear_check_at = 0.0
        self._from_pear = False  # Pear's lines replace an LRCLIB guess, never the other way round
        self._lrclib_at: float | None = None  # when to ask LRCLIB; None once asked
        self._was_loading = False

    @property
    def tool_id(self) -> str:
        return "lyrics"

    @property
    def display_name(self) -> str:
        return "Lyrics"

    @property
    def tick_interval_ms(self) -> int:
        return 250

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def busy(self) -> bool:
        # Stays fast while paused too: otherwise a new song is noticed up to 2s late
        return self._enabled

    @property
    def playing(self) -> bool:
        """A track is playing in an SMTC player (only known while the tool is on)."""
        return self._enabled and self._playing

    @property
    def has_lyrics(self) -> bool:
        """Playing a track whose synced lines are known (the card stays up through blank lines)."""
        return self.playing and bool(self._lines)

    @property
    def title(self) -> str:
        """Title of the track SMTC reports ('' when off)."""
        return self._track[0] if self._enabled else ""

    @property
    def loading(self) -> bool:
        """Playing, but no lyrics yet and still worth waiting for (drives the bubble's waiting dots)."""
        return (self.playing and not self._lines
                and time.monotonic() - self._track_started < _LOADING_MAX_S)

    @property
    def line(self) -> str:
        """Current lyric line to display; '' when off, paused, unsynced or between lines."""
        return self._line if self._enabled and self._playing else ""

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.close()
            self._track, self._lines, self._playing, self._line = ("", ""), None, False, ""

    def _fetch(self, track: tuple[str, str], duration_s: float) -> None:
        lines = self._cache.get(track)
        if lines is None:
            lines = fetch_synced_lyrics(*track, duration_s)
            if lines and len(self._cache) >= 20:
                self._cache.pop(next(iter(self._cache)))
            if lines:  # a miss may be a network blip, so only hits are cached
                self._cache[track] = lines
        # Ignore a lookup the user already skipped past, and never let a late miss wipe Pear's hit
        if track == self._track and not self._lines:
            self._retry_at = time.monotonic() + _RETRY_S
            self._lines = lines

    def _start_fetch(self, track: tuple[str, str], snap: dict[str, Any]) -> None:
        try:
            duration_s = float(snap.get("dur") or 0) / 1000
        except (TypeError, ValueError):
            duration_s = 0.0
        threading.Thread(target=self._fetch, args=(track, duration_s), daemon=True).start()

    def tick(self) -> bool:
        if not self._enabled:
            return False
        if self._reader is None or not self._reader.alive:
            try:
                self._reader = _SmtcReader()
            except OSError:
                self._enabled = False
                return True
        snap = self._reader.latest()
        if not _is_song(snap):
            snap = {}  # a browser tab or long video: no lyrics card, no lookup, no music groove
        track =(str(snap.get("title") or ""), str(snap.get("artist") or ""))
        playing = snap.get("status") == "Playing" and bool(track[0])
        now = time.monotonic()
        if track != self._track:
            from_pear_app = re.search(r"peerm|pear", str(snap.get("app") or ""), re.IGNORECASE) is not None
            self._track, self._lines, self._retries, self._from_pear = track, None, 0, False
            self._track_started, self._pear_path, self._pear_check_at = now, None, now
            self._lrclib_at = now + _PEAR_GRACE_S if from_pear_app else now
        if track[0] and not self._from_pear and now >= self._pear_check_at:
            self._pear_check_at = now + _PEAR_POLL_S
            self._poll_pear_cache(track)
        if track[0] and self._lrclib_at is not None and now >= self._lrclib_at and not self._lines:
            self._lrclib_at = None
            self._start_fetch(track, snap)
        elif (self._lines == [] and track[0] and self._lrclib_at is None and self._retries < _MAX_RETRIES
              and now >= self._retry_at):
            # A miss is often transient: LRCLIB timed out, or SMTC still held the previous song's duration
            self._lines, self._retries = None, self._retries + 1
            self._start_fetch(track, snap)
        position = 0.0
        if playing:
            # SMTC reports the position as of `at`; Pear only republishes it occasionally
            try:
                position = float(snap["pos"]) + max(0.0, time.time() * 1000 - float(snap["at"]))
            except (KeyError, TypeError, ValueError):
                playing = False
        line = line_at(self._lines, position)[:_MAX_LINE_CHARS] if playing and self._lines else ""
        loading = self.loading
        changed = line != self._line or playing != self._playing or loading != self._was_loading
        self._line, self._playing, self._position_ms, self._was_loading = line, playing, position, loading
        return changed

    def _poll_pear_cache(self, track: tuple[str, str]) -> None:
        if self._pear_path is None:
            if time.monotonic() - self._track_started > 10:
                return  # Pear logs a song at its start; later on this is another player
            self._pear_path = pear_lyrics_path(track[0])
        if self._pear_path is None or not self._pear_path.is_file():
            return
        try:
            lines = parse_lrc(self._pear_path.read_text("utf-8", "replace"))
        except OSError:
            return
        if lines:
            self._lines, self._from_pear = lines, True
            self._cache[track] = lines

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        if not self._enabled:
            return HudData(badge_left="LYRICS", badge_right="OFF", badge_right_color="#8A9AF2",
                           bars=(HudBar("Line", 0.0, display_text="  -"),
                                 HudBar("Lines", 0.0, display_text="  -"),
                                 HudBar("Synced", 0.0, display_text=" No")),
                           detail="Turn on from the Lyrics menu")
        total = len(self._lines or [])
        index = bisect.bisect_right(self._lines or [], self._position_ms, key=lambda pair: pair[0])
        synced = total > 0
        title, artist = self._track
        if not title:
            detail = "Nothing playing"
        elif self._lines is None:
            detail = f"Looking up lyrics...  {title}"
        elif not synced:
            detail = f"No synced lyrics  {title}"
        else:
            detail = f"{artist} - {title}" if artist else title
        return HudData(
            badge_left="LYRICS",
            badge_right="PLAYING" if self._playing else "PAUSED",
            badge_right_color="#38B764" if self._playing else "#8A9AF2",
            bars=(
                HudBar("Line", index * 100.0 / total if total else 0.0, "#73EFF7", f"{index:3d}"),
                HudBar("Lines", min(100.0, total / 1.2), "#41A6F6", f"{total:3d}"),
                HudBar("Synced", 100.0 if synced else 0.0, "#38B764" if synced else "#EF7D57",
                       "Yes" if synced else " No"),
            ),
            detail=detail,
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        return [ToolMenuItem(label="Show Song Lyrics", callback_id="lyrics:toggle", checkable=True,
                             checked=self._enabled, submenu=self.SUBMENU_NAME)]

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id != "lyrics:toggle":
            return (False, "", "")
        self.set_enabled(not self._enabled)
        return (True, "LYRICS ON" if self._enabled else "LYRICS OFF", "#73EFF7")

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self._enabled}

    def load_dict(self, data: dict[str, Any]) -> None:
        if isinstance(data, dict) and data.get("enabled") is True:
            self._enabled = True
