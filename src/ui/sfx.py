"""Tiny synthesized UI sounds (no audio assets) streamed through one shared QAudioSink."""

from __future__ import annotations

import math
import os
import random
import struct

from PyQt6.QtCore import QCoreApplication, QIODevice, QObject, QTimer

SAMPLE_RATE = 48000
VOLUME = 0.25
SINK_BUFFER_BYTES = 4800  # ~50ms of mono Int16: tick latency vs underruns while Python is busy
IDLE_SUSPEND_MS = 20_000  # keep the stream running this long after the last wheel closes
# C major pentatonic from C4: sliding around the wheel plays a rising scale, never a clash
_TICK_SEMITONES = (0, 2, 4, 7, 9, 12, 14, 16, 19, 21)

# Why one sink, kept running between wheels, suspended only after IDLE_SUSPEND_MS:
# - Windows powers an idle output down, and waking it swallows the first ~100ms: a whole tick.
# - Creating a sink costs ~1s; resume/suspend block the GUI thread ~20-60ms each, so doing them
#   per summon stuttered the wheel. Back-to-back wheels now pay nothing.
# - A running stream blocks PC sleep, so it still suspends once wheels go quiet.
# (A worker-thread sink was tried: Qt's audio device handle is bound to the GUI thread's COM
# apartment, and using it elsewhere stalled the GUI for seconds.)
_bank: list[bytes] = []  # raw mono Int16 PCM per pitch, synthesized once at startup
_sink = None
_sink_device_id: bytes = b""
_stream: _TickStream | None = None
_idle_timer: QTimer | None = None
_devices: object = None
_awake: set[int] = set()  # ids of TickSounds whose wheel is open


def _synth_tick(semitones: int) -> bytes:
    """Short woody marimba knock, pitched by slice."""
    freq = 261.63 * 2 ** (semitones / 12)
    n = int(SAMPLE_RATE * 0.12)
    out = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.001) * math.exp(-t * 45.0)
        # Bar-like 4x overtone that dies fast, so the attack knocks and the tail stays mellow
        tone = math.sin(2 * math.pi * freq * t) + 0.3 * math.exp(-t * 120.0) * math.sin(8 * math.pi * freq * t)
        click = math.exp(-t * 400.0) * math.sin(2 * math.pi * 900 * t)
        out += struct.pack("<h", int(32767 * VOLUME * math.tanh(1.3 * (0.6 * env * tone + 0.35 * click))))
    return bytes(out)


def _synth_soft(semitones: float) -> bytes:
    """Soft picker-style tick: rounded attack, one mid tone with a tiny drop, almost no tail.

    Hover ticks fire constantly, so they stay the lightest sound: short, quiet, no sharp click.
    """
    rng = random.Random(int(semitones * 10))  # fixed seed: same slice, same tick
    freq = 620.0 * 2 ** (semitones / 12)
    n = int(SAMPLE_RATE * 0.035)
    wave = []
    phase = lp = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        # The ear times a tick by its transient: a 2ms fade-in with a faint one felt late
        attack = 0.5 - 0.5 * math.cos(math.pi * min(1.0, t / 0.0003))
        phase += 2 * math.pi * freq * (1 + 0.12 * math.exp(-t * 300.0)) / SAMPLE_RATE
        lp += 0.5 * (rng.uniform(-1.0, 1.0) - lp)
        wave.append(attack * (math.exp(-t * 110.0) * math.sin(phase) + 0.45 * math.exp(-t * 2500.0) * lp))
    peak = max(map(abs, wave)) or 1.0
    return b"".join(struct.pack("<h", int(32767 * VOLUME * 0.6 * s / peak)) for s in wave)


# Small even steps around the ring: hints direction without playing a tune (big jumps grate)
_SOFT_STEPS = tuple(i * 0.4 for i in range(10))
_VOICE, _VOICE_STEPS = _synth_soft, _SOFT_STEPS  # or _synth_tick, _TICK_SEMITONES for the marimba


class _TickStream(QIODevice):
    """Endless pull source: the current tick, then silence. A new tick replaces the old one."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pcm = b""
        self._pos = 0
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def start_tick(self, pcm: bytes) -> None:
        self._pcm, self._pos = pcm, 0

    def readData(self, maxlen: int) -> bytes:  # type: ignore[override]
        # Pulled on the GUI thread (measured), so start_tick() can never interleave with a read
        maxlen -= maxlen % 2
        chunk = self._pcm[self._pos : self._pos + maxlen]
        self._pos += len(chunk)
        return chunk + bytes(maxlen - len(chunk))

    def writeData(self, _data) -> int:  # type: ignore[override]
        return 0

    def isSequential(self) -> bool:  # type: ignore[override]
        return True

    def bytesAvailable(self) -> int:  # type: ignore[override]
        return SINK_BUFFER_BYTES + super().bytesAvailable()


def _open_sink() -> None:
    """(Re)open the shared stream on the current default output, suspended unless a wheel is open."""
    global _sink, _sink_device_id, _stream
    # Sound is optional: this runs from Qt slots, where an uncaught exception aborts the whole app
    try:
        from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        device = QMediaDevices.defaultAudioOutput()
        _sink_device_id = bytes(device.id())
        if _sink is not None:
            _sink.stop()
            _sink.deleteLater()
            _sink = None
        app = QCoreApplication.instance()
        if _stream is None:
            _stream = _TickStream(app)
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        _sink = QAudioSink(device, fmt, app)
        _sink.setBufferSize(SINK_BUFFER_BYTES)
        _sink.start(_stream)
        if not _awake:
            _sink.suspend()
    except Exception:
        _sink = None


def _on_outputs_changed() -> None:
    # Windows fires this for property churn too, including our own stream starting. Reopening on
    # every one killed the stream for seconds at a time and blocked the GUI: only follow real
    # default-device switches.
    try:
        from PyQt6.QtMultimedia import QMediaDevices

        if bytes(QMediaDevices.defaultAudioOutput().id()) != _sink_device_id:
            _open_sink()
    except Exception:
        pass


def _resume() -> None:
    if _idle_timer is not None:
        _idle_timer.stop()
    if _sink is not None:
        from PyQt6.QtMultimedia import QAudio

        if _sink.state() == QAudio.State.SuspendedState:
            _sink.resume()


def _suspend_if_idle() -> None:
    if _awake:
        return
    from PyQt6.QtMultimedia import QAudio

    if _sink is None or _sink.state() == QAudio.State.StoppedState:
        # A failed or errored open would mean silent wheels until a device switch; retry while
        # idle, where the ~1s open cannot stall a wheel
        _open_sink()
    elif _stream is not None:
        _stream.start_tick(b"")
        _sink.suspend()


def preload_tick_sounds() -> list[bytes]:
    """Synthesize the tick bank and open the suspended stream once; call at startup."""
    global _devices, _idle_timer
    if _bank or os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return _bank
    _bank.extend(_VOICE(s) for s in _VOICE_STEPS)
    app = QCoreApplication.instance()
    _idle_timer = QTimer(app)
    _idle_timer.setSingleShot(True)
    _idle_timer.setInterval(IDLE_SUSPEND_MS)
    _idle_timer.timeout.connect(_suspend_if_idle)
    _open_sink()
    try:
        from PyQt6.QtMultimedia import QMediaDevices

        # A sink stays bound to the device it opened on; follow speaker/headphone switches
        _devices = QMediaDevices(app)
        _devices.audioOutputsChanged.connect(_on_outputs_changed)  # type: ignore[attr-defined]
    except Exception:
        pass
    return _bank


def prewarm() -> None:
    """Resume the stream ahead of a wheel (e.g. on mouse press) without marking one open."""
    _resume()
    if not _awake and _idle_timer is not None:
        _idle_timer.start()  # nothing opened after all: still suspend once quiet


def _synth_notes(notes: tuple[tuple[float, float], ...], level: float = 0.5) -> bytes:
    """Soft chiptune cue: rounded triangle notes (semitones from C5, seconds), each a quick decay."""
    out = bytearray()
    for semitones, length in notes:
        freq = 523.25 * 2 ** (semitones / 12)
        for i in range(int(SAMPLE_RATE * length)):
            t = i / SAMPLE_RATE
            env = min(1.0, t / 0.004) * math.exp(-t * 18.0)
            tri = 2 * abs(2 * ((freq * t) % 1.0) - 1) - 1
            out += struct.pack("<h", int(32767 * VOLUME * level * env * (0.7 * tri + 0.3 * math.sin(2 * math.pi * freq * t))))
    return bytes(out)


# AI chat cues: open sparkle, send blip, reply chime, error bump (all pentatonic, never harsh)
_CUE_NOTES: dict[str, tuple[tuple[float, float], ...]] = {
    "open": ((0, 0.06), (7, 0.06), (12, 0.16)),
    "send": ((7, 0.05), (12, 0.09)),
    "reply": ((9, 0.07), (16, 0.16)),
    "error": ((-5, 0.08), (-8, 0.16)),
}
_cues: dict[str, bytes] = {}
_WAKE_PAD = bytes(int(SAMPLE_RATE * 0.1) * 2)  # a suspended output swallows its first ~100ms


def play_cue(name: str) -> None:
    """One-off UI cue on the shared stream; the stream suspends again once quiet."""
    if _stream is None or _sink is None or name not in _CUE_NOTES:
        return
    if name not in _cues:
        _cues[name] = _synth_notes(_CUE_NOTES[name])
    try:
        from PyQt6.QtMultimedia import QAudio

        waking = _sink.state() == QAudio.State.SuspendedState
        _resume()
        _stream.start_tick((_WAKE_PAD if waking else b"") + _cues[name])
        if not _awake and _idle_timer is not None:
            _idle_timer.start()
    except Exception:
        pass


class TickSounds(QObject):
    """Pitched hover ticks for one wheel. wake() when it opens, sleep() when it closes."""

    def wake(self, *, defer: bool = False) -> None:
        """defer=True resumes on the next event-loop turn so the wheel's first frame is not held up."""
        _awake.add(id(self))
        if defer:
            QTimer.singleShot(0, lambda: _awake and _resume())  # skip if closed again meanwhile
        else:
            _resume()

    def sleep(self) -> None:
        # Shared sink: a hidden wheel's dismiss must not affect the wheel that is still open
        _awake.discard(id(self))
        if not _awake and _idle_timer is not None:
            _idle_timer.start()

    def play(self, index: int) -> None:
        # No wake() here: a stray hover on a hidden wheel would keep the stream (and PC) awake
        if _stream is not None and _bank:
            _stream.start_tick(_bank[index % len(_bank)])
