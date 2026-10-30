"""Unit tests for procedural sprite matrices and AnimationController."""

from __future__ import annotations

import pytest

from src.graphics.animation_controller import AnimationClip, AnimationController
from src.graphics.sprite_matrix import (
    ALL_ANIMATIONS,
    REQUIRED_ANIMATIONS,
    flip_frame_horizontal,
    get_sprite_frames,
)


class TestSpriteMatrix:
    @pytest.mark.parametrize("size", [16, 32])
    @pytest.mark.parametrize("state", ALL_ANIMATIONS)
    def test_frame_dimensions_and_rgba_validity(self, size: int, state: str) -> None:
        frames = get_sprite_frames(state, size=size)
        assert len(frames) >= 2, f"{state} ({size}x{size}) must have at least 2 animation frames"

        for frame_idx, frame in enumerate(frames):
            assert len(frame) == size, f"{state}[{frame_idx}] row count != {size}"
            visible_count = 0
            for y, row in enumerate(frame):
                assert len(row) == size, f"{state}[{frame_idx}] row {y} length != {size}"
                for pixel in row:
                    assert len(pixel) == 4
                    r, g, b, a = pixel
                    assert 0 <= r <= 255
                    assert 0 <= g <= 255
                    assert 0 <= b <= 255
                    assert 0 <= a <= 255
                    # Frosted palette is translucent by design; count clearly visible texels
                    if a >= 100:
                        visible_count += 1

            # Corner pixels must remain transparent for window click pass-through
            assert frame[0][0][3] == 0
            assert frame[0][size - 1][3] == 0
            assert visible_count > (size * size) // 10

    @pytest.mark.parametrize("state", ALL_ANIMATIONS)
    def test_consecutive_frames_animate_distinct_pixels(self, state: str) -> None:
        assert set(REQUIRED_ANIMATIONS) <= set(ALL_ANIMATIONS)
        frames = get_sprite_frames(state, size=32)
        assert any(frames[i] != frames[0] for i in range(1, len(frames)))

    def test_invalid_size_or_state_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            get_sprite_frames("idle", size=24)
        with pytest.raises(KeyError):
            get_sprite_frames("nonexistent_state", size=32)

    def test_horizontal_flip_reverses_rows_and_preserves_shape(self) -> None:
        frame = get_sprite_frames("walk", size=16)[0]
        flipped = flip_frame_horizontal(frame)
        assert len(flipped) == 16
        assert all(len(row) == 16 for row in flipped)
        for y in range(16):
            assert flipped[y] == list(reversed(frame[y]))


class TestAnimationController:
    def test_cycles_frames_with_custom_durations_and_loops(self) -> None:
        controller = AnimationController(size=16)
        clip = AnimationClip(
            name="custom",
            frames=get_sprite_frames("walk", size=16)[:3],
            durations_ms=[100, 200, 150],
            loop=True,
        )
        controller.register_clip(clip)
        controller.set_state("custom", force=True)

        assert controller.frame_index == 0
        assert controller.current_frame_duration_ms() == 100

        # Partial step does not advance frame
        assert controller.advance(60) is False
        assert controller.frame_index == 0

        # Crossing 100ms advances to frame 1 with 10ms carry-over
        assert controller.advance(50) is True
        assert controller.frame_index == 1
        assert controller.current_frame_duration_ms() == 200

        # Advance 200ms -> frame 2 (carry 10 + 190 = 200)
        assert controller.advance(190) is True
        assert controller.frame_index == 2

        # Advance 150ms -> wraps back to frame 0
        assert controller.advance(150) is True
        assert controller.frame_index == 0

    def test_set_state_idempotent_unless_forced(self) -> None:
        controller = AnimationController(size=32)
        controller.set_state("walk")
        duration = controller.current_frame_duration_ms()
        controller.advance(duration)
        assert controller.frame_index == 1

        # Calling set_state("walk") again does not reset mid-cycle
        assert controller.set_state("walk") is False
        assert controller.frame_index == 1

        # Forced reset goes back to frame 0
        assert controller.set_state("walk", force=True) is True
        assert controller.frame_index == 0

    def test_oneshot_animation_returns_to_base_state(self) -> None:
        controller = AnimationController(size=32)
        controller.set_state("idle")
        controller.play_oneshot("blink")
        assert controller.active_clip_name == "blink"

        blink_frames = get_sprite_frames("blink", size=32)
        for _ in range(len(blink_frames) + 1):
            controller.advance(controller.current_frame_duration_ms())
            if controller.active_clip_name == "idle":
                break

        assert controller.active_clip_name == "idle"
        assert controller.frame_index == 0
