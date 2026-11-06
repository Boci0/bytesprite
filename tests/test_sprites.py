"""Unit tests for the vector duck rig and AnimationController."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from src.graphics.animation_controller import AnimationClip, AnimationController
from src.graphics.duck import (ALL_ANIMATIONS, REQUIRED_ANIMATIONS, Pose, get_poses, get_sprite_frames, render_icon,
                              render_pose)


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class TestDuckSprite:
    @pytest.mark.parametrize("size", [16, 32, 128])
    @pytest.mark.parametrize("state", ALL_ANIMATIONS)
    def test_frames_render_clear_corners_and_a_solid_body(self, size: int, state: str) -> None:
        frames = get_sprite_frames(state, size=size)
        assert len(frames) >= 2, f"{state} must have at least 2 frames"
        for idx, img in enumerate(frames):
            assert (img.width(), img.height()) == (size, size)
            # Top corners stay clear so the layered window passes clicks through there
            assert img.pixelColor(0, 0).alpha() == 0 and img.pixelColor(size - 1, 0).alpha() == 0
            solid = sum(img.pixelColor(x, y).alpha() >= 100 for y in range(size) for x in range(size))
            assert solid > size * size // 10, f"{state}[{idx}] too faint"

    @pytest.mark.parametrize("state", ALL_ANIMATIONS)
    def test_every_clip_moves(self, state: str) -> None:
        assert set(REQUIRED_ANIMATIONS) <= set(ALL_ANIMATIONS)
        frames = get_sprite_frames(state, size=32)
        assert any(frames[i] != frames[0] for i in range(1, len(frames)))
        # Acting, not one rigid transform: some joint besides the whole body moves in every clip
        poses = get_poses(state)
        joints = ("hx", "hy", "hr", "beak", "wing", "tail", "fdx", "flift", "ndx", "nlift")
        assert any(getattr(p, j) != getattr(poses[0], j) for p in poses for j in joints) or             any(p.eye != poses[0].eye for p in poses)

    def test_invalid_size_or_state_raises(self) -> None:
        with pytest.raises(ValueError):
            get_sprite_frames("idle", size=8)
        with pytest.raises(KeyError):
            get_sprite_frames("nonexistent_state", size=32)

    def test_mirrored_render_flips_the_duck(self) -> None:
        plain, flipped = render_pose(Pose(), 128), render_pose(Pose(), 128, mirrored=True)
        assert plain != flipped
        # The beak tip (units 108..118 at row 36) points right normally, left when mirrored
        beak_right = all(plain.pixelColor(x, 36).alpha() > 0 for x in range(108, 118))
        beak_left = all(flipped.pixelColor(127 - x, 36).alpha() > 0 for x in range(108, 118))
        assert beak_right and beak_left


class TestAnimationController:
    def test_cycles_frames_with_custom_durations_and_loops(self) -> None:
        controller = AnimationController(size=16)
        clip = AnimationClip(
            name="custom",
            frames=get_poses("walk")[:3],
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

        blink_frames = get_poses("blink")
        for _ in range(len(blink_frames) + 1):
            controller.advance(controller.current_frame_duration_ms())
            if controller.active_clip_name == "idle":
                break

        assert controller.active_clip_name == "idle"
        assert controller.frame_index == 0


def test_icon_fills_its_square() -> None:
    img = render_icon(64)
    cols = [x for x in range(64) if any(img.pixelColor(x, y).alpha() > 40 for y in range(64))]
    rows = [y for y in range(64) if any(img.pixelColor(x, y).alpha() > 40 for x in range(64))]
    assert cols[0] <= 3 and cols[-1] >= 60  # the duck spans the width: no empty canvas around it
    assert rows[0] >= 2 and rows[-1] <= 61 and (rows[0] + rows[-1]) / 2 == pytest.approx(31.5, abs=3)  # centred
