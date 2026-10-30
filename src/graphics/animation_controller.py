"""Frame timing and animation state playback controller for ByteSprite."""

from __future__ import annotations

from dataclasses import dataclass

from src.graphics.sprite_matrix import (
    ALL_ANIMATIONS,
    FrameMatrix,
    flip_frame_horizontal,
    get_sprite_frames,
)

DEFAULT_FRAME_DURATIONS_MS: dict[str, list[int]] = {
    "idle": [750, 450, 750, 450],
    "blink": [90, 130, 90],
    "walk": [150, 150, 150, 150],
    "sleep": [850, 850, 850, 850],
    "focus": [130] * 12,
    "spin": [90, 55, 50, 50, 60, 50, 50, 55, 200],
    "hungry": [500, 500, 500, 500],
    "eating": [180, 180, 180, 220],
}


@dataclass(frozen=True)
class AnimationClip:
    """Immutable sequence of pixel frames paired with per-frame durations."""

    name: str
    frames: list[FrameMatrix]
    durations_ms: list[int]
    loop: bool = True

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError(f"AnimationClip {self.name!r} must contain at least one frame.")
        if len(self.frames) != len(self.durations_ms):
            raise ValueError(
                f"AnimationClip {self.name!r} frames ({len(self.frames)}) "
                f"must match durations_ms ({len(self.durations_ms)})."
            )
        if any(d <= 0 for d in self.durations_ms):
            raise ValueError(f"AnimationClip {self.name!r} durations must be positive ms.")


class AnimationController:
    """Manages active animation clips, frame timing, looping, and one-shot overlays."""

    def __init__(self, size: int = 32, initial_state: str = "idle") -> None:
        self._size = size
        self._clips: dict[str, AnimationClip] = {}
        self._load_default_clips(size)

        self._base_state: str = initial_state if initial_state in self._clips else "idle"
        self._oneshot_state: str | None = None
        self._frame_index: int = 0
        self._elapsed_in_frame_ms: int = 0
        self._facing_left: bool = False

    def _load_default_clips(self, size: int) -> None:
        for name in ALL_ANIMATIONS:
            frames = get_sprite_frames(name, size=size)
            raw_durations = DEFAULT_FRAME_DURATIONS_MS.get(name, [250] * len(frames))
            durations = (
                raw_durations[: len(frames)]
                if len(raw_durations) >= len(frames)
                else raw_durations + [raw_durations[-1]] * (len(frames) - len(raw_durations))
            )
            self._clips[name] = AnimationClip(
                name=name,
                frames=frames,
                durations_ms=durations,
                loop=(name not in {"blink", "eating", "spin"}),
            )

    @property
    def size(self) -> int:
        return self._size

    @property
    def base_state(self) -> str:
        return self._base_state

    @property
    def active_clip_name(self) -> str:
        return self._oneshot_state if self._oneshot_state is not None else self._base_state

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def facing_left(self) -> bool:
        return self._facing_left

    @facing_left.setter
    def facing_left(self, value: bool) -> None:
        self._facing_left = bool(value)

    def register_clip(self, clip: AnimationClip) -> None:
        """Register or replace a named animation clip."""
        self._clips[clip.name] = clip

    def set_state(self, state: str, *, force: bool = False) -> bool:
        """Set looping base animation state; returns True if playback reset."""
        if state not in self._clips:
            raise KeyError(f"Unknown animation clip: {state!r}")

        if not force and self._base_state == state and self._oneshot_state is None:
            return False

        self._base_state = state
        self._oneshot_state = None
        self._frame_index = 0
        self._elapsed_in_frame_ms = 0
        return True

    def play_oneshot(self, state: str) -> None:
        """Play a non-looping reaction clip once, then return to base_state."""
        if state not in self._clips:
            raise KeyError(f"Unknown animation clip: {state!r}")
        self._oneshot_state = state
        self._frame_index = 0
        self._elapsed_in_frame_ms = 0

    def current_clip(self) -> AnimationClip:
        return self._clips[self.active_clip_name]

    def current_frame_duration_ms(self) -> int:
        clip = self.current_clip()
        return clip.durations_ms[self._frame_index]

    def current_frame(self) -> FrameMatrix:
        clip = self.current_clip()
        frame = clip.frames[self._frame_index]
        return flip_frame_horizontal(frame) if self._facing_left else frame

    def advance(self, elapsed_ms: int) -> bool:
        """Advance playback clock by elapsed_ms; returns True if visible frame changed."""
        if elapsed_ms <= 0:
            return False

        prev_clip = self.active_clip_name
        prev_index = self._frame_index
        self._elapsed_in_frame_ms += elapsed_ms

        while True:
            clip = self.current_clip()
            frame_duration = clip.durations_ms[self._frame_index]
            if self._elapsed_in_frame_ms < frame_duration:
                break

            self._elapsed_in_frame_ms -= frame_duration
            next_index = self._frame_index + 1

            if next_index < len(clip.frames):
                self._frame_index = next_index
            elif self._oneshot_state is not None:
                # Oneshot finished; revert cleanly to base_state
                self._oneshot_state = None
                self._frame_index = 0
                self._elapsed_in_frame_ms = 0
                break
            elif clip.loop:
                self._frame_index = 0
            else:
                self._frame_index = len(clip.frames) - 1
                self._elapsed_in_frame_ms = 0
                break

        return (self.active_clip_name != prev_clip) or (self._frame_index != prev_index)
