"""Tiny synthesized UI sounds (no audio assets): winsound on Windows, QSoundEffect elsewhere."""

from __future__ import annotations

import io
import math
import os
import struct
import sys
import tempfile
import wave

from PyQt6.QtCore import QCoreApplication, QObject, QUrl

SAMPLE_RATE = 22050
VOLUME = 0.25  # baked into the samples: winsound has no volume control
# C major pentatonic from C5: sliding around the wheel plays a rising scale, never a clash
_TICK_SEMITONES = (0, 2, 4, 7, 9, 12, 14, 16, 19, 21)

# One app-wide bank built at startup: wheels are created lazily, and a QSoundEffect bank built
# then is still loading during the first hovers
_paths: list[str] = []
_effects: list = []
_devices: object = None


def _synth_tick(semitones: int) -> bytes:
    """Short glassy pluck with a tiny click at the start, pitched by slice."""
    freq = 523.25 * 2 ** (semitones / 12)
    n = int(SAMPLE_RATE * 0.12)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = min(1.0, t / 0.001) * math.exp(-t * 32.0)
        tone = math.sin(2 * math.pi * freq * t) + 0.4 * math.sin(4 * math.pi * freq * t)
        click = math.exp(-t * 600.0) * math.sin(2 * math.pi * 3000 * t)
        samples.append(VOLUME * math.tanh(1.3 * (0.6 * env * tone + 0.35 * click)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))
    return buf.getvalue()


def preload_tick_sounds() -> list[str]:
    """Write the tick WAVs once (and load QSoundEffects off Windows); call at startup."""
    global _devices
    if _paths or os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        return _paths
    for semitones in _TICK_SEMITONES:
        path = os.path.join(tempfile.gettempdir(), f"bytesprite_tick_v2_{semitones}.wav")
        try:
            with open(path, "wb") as f:
                f.write(_synth_tick(semitones))
        except OSError:
            pass  # another running instance holds it; its identical copy still plays
        _paths.append(path)
    if sys.platform == "win32":
        return _paths

    from PyQt6.QtMultimedia import QMediaDevices, QSoundEffect

    app = QCoreApplication.instance()
    for path in _paths:
        effect = QSoundEffect(app)
        effect.setSource(QUrl.fromLocalFile(path))
        _effects.append(effect)
    # Effects stay bound to the device they first opened; follow speaker/headphone switches
    _devices = QMediaDevices(app)
    _devices.audioOutputsChanged.connect(  # type: ignore[attr-defined]
        lambda: [e.setAudioDevice(QMediaDevices.defaultAudioOutput()) for e in _effects]
    )
    return _paths


class TickSounds(QObject):
    """Plays pitched ticks from the shared bank."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        preload_tick_sounds()

    def play(self, index: int) -> None:
        if not _paths:
            return
        i = index % len(_paths)
        if sys.platform == "win32":
            import winsound

            # Qt's FFmpeg QSoundEffect reported playing yet stayed silent on Windows; winmm is
            # always on the default output. A new tick cuts the previous one, which suits ticks.
            winsound.PlaySound(
                _paths[i], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
            )
        elif _effects:
            _effects[i].play()
