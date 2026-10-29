"""Unit tests for PetStats and PetStateMachine."""

from __future__ import annotations

import math
import pytest

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine


class TestPetStats:
    def test_default_stats_within_bounds(self) -> None:
        stats = PetStats()
        assert 0.0 <= stats.hunger <= 100.0
        assert 0.0 <= stats.energy <= 100.0
        assert 0.0 <= stats.happiness <= 100.0
        assert stats.experience == 0
        assert stats.last_updated > 0.0
        assert stats.level == 1

    def test_boundary_clamps_on_construction(self) -> None:
        stats = PetStats(
            hunger=150.0,
            energy=-25.0,
            happiness=999.0,
            experience=-10,
            last_updated=1000.0,
        )
        assert stats.hunger == 100.0
        assert stats.energy == 0.0
        assert stats.happiness == 100.0
        assert stats.experience == 0

    def test_nan_and_inf_sanitized(self) -> None:
        stats = PetStats(
            hunger=math.nan,
            energy=math.inf,
            happiness=-math.inf,
        )
        assert 0.0 <= stats.hunger <= 100.0
        assert stats.energy == 0.0 or stats.energy == 100.0
        assert 0.0 <= stats.happiness <= 100.0


class TestPetStateMachine:
    def test_initial_idle_state(self) -> None:
        sm = PetStateMachine(PetStats(hunger=80.0, energy=80.0, happiness=80.0))
        assert sm.state == PetState.IDLE

    def test_tick_degradation_reduces_awake_stats(self) -> None:
        sm = PetStateMachine(PetStats(hunger=80.0, energy=80.0, happiness=80.0))
        sm.tick()
        assert sm.stats.hunger < 80.0
        assert sm.stats.energy < 80.0
        assert sm.stats.happiness < 80.0

    def test_tick_clamps_stats_at_zero(self) -> None:
        sm = PetStateMachine(PetStats(hunger=1.0, energy=50.0, happiness=0.5))
        sm.tick(elapsed_ticks=20)
        assert sm.stats.hunger >= 0.0
        assert sm.stats.energy >= 0.0
        assert sm.stats.happiness >= 0.0

    def test_hungry_transition_when_hunger_low(self) -> None:
        sm = PetStateMachine(PetStats(hunger=26.0, energy=80.0, happiness=80.0))
        assert sm.state == PetState.IDLE
        sm.tick(elapsed_ticks=2)
        assert sm.stats.hunger <= 25.0
        assert sm.state == PetState.HUNGRY

    def test_feed_restores_hunger_and_returns_to_idle(self) -> None:
        sm = PetStateMachine(PetStats(hunger=15.0, energy=80.0, happiness=60.0))
        assert sm.state == PetState.HUNGRY
        fed = sm.feed(amount=30.0)
        assert fed is True
        assert sm.stats.hunger == 45.0
        assert sm.stats.experience > 0
        assert sm.state == PetState.IDLE

    def test_feed_clamps_hunger_at_100(self) -> None:
        sm = PetStateMachine(PetStats(hunger=90.0, energy=80.0, happiness=98.0))
        sm.feed(amount=40.0)
        assert sm.stats.hunger == 100.0
        assert sm.stats.happiness == 100.0

    def test_pet_increases_happiness_and_experience(self) -> None:
        sm = PetStateMachine(PetStats(hunger=70.0, energy=70.0, happiness=50.0))
        before_xp = sm.stats.experience
        result = sm.pet(amount=20.0)
        assert result is True
        assert sm.stats.happiness == 70.0
        assert sm.stats.experience > before_xp

    def test_play_transitions_to_playing_and_returns_after_ticks(self) -> None:
        sm = PetStateMachine(PetStats(hunger=70.0, energy=70.0, happiness=50.0))
        played = sm.play()
        assert played is True
        assert sm.state == PetState.PLAYING
        assert sm.stats.happiness > 50.0
        assert sm.stats.energy < 70.0

        sm.tick(elapsed_ticks=3)
        assert sm.state == PetState.IDLE

    def test_play_refused_when_exhausted(self) -> None:
        sm = PetStateMachine(PetStats(hunger=70.0, energy=5.0, happiness=50.0))
        played = sm.play()
        assert played is False
        assert sm.state == PetState.SLEEPING

    def test_rest_transitions_to_sleeping_and_recovers_energy(self) -> None:
        sm = PetStateMachine(PetStats(hunger=70.0, energy=40.0, happiness=60.0))
        sm.rest()
        assert sm.state == PetState.SLEEPING
        energy_after_rest = sm.stats.energy
        sm.tick(elapsed_ticks=5)
        assert sm.stats.energy > energy_after_rest

    def test_sleeping_auto_wakes_at_full_energy(self) -> None:
        sm = PetStateMachine(PetStats(hunger=80.0, energy=90.0, happiness=80.0))
        sm.rest()
        assert sm.state == PetState.SLEEPING
        sm.tick(elapsed_ticks=1)
        assert sm.state == PetState.SLEEPING
        sm.tick(elapsed_ticks=1)
        assert sm.stats.energy == 100.0
        assert sm.state == PetState.IDLE

    def test_exhaustion_auto_sleeps_on_tick(self) -> None:
        sm = PetStateMachine(PetStats(hunger=80.0, energy=10.5, happiness=80.0))
        sm.tick(elapsed_ticks=1)
        assert sm.state == PetState.SLEEPING

    def test_state_change_callback_invoked_with_old_and_new_states(self) -> None:
        events: list[tuple[PetState, PetState, float]] = []
        sm = PetStateMachine(PetStats(hunger=80.0, energy=80.0, happiness=80.0))
        unsubscribe = sm.on_state_change(
            lambda old, new, stats: events.append((old, new, stats.energy))
        )

        sm.rest()
        assert len(events) == 1
        assert events[0][0] == PetState.IDLE
        assert events[0][1] == PetState.SLEEPING

        # Calling rest again while already sleeping does not re-emit duplicate transition
        sm.rest()
        assert len(events) == 1

        sm.wake()
        assert len(events) == 2
        assert events[1][0] == PetState.SLEEPING
        assert events[1][1] == PetState.IDLE

        unsubscribe()
        sm.rest()
        assert len(events) == 2
