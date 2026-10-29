"""Headless simulation state machine driving ByteSprite stats and mood."""

from __future__ import annotations

import time
from collections.abc import Callable

from src.engine.models import PetState, PetStats

StateChangeCallback = Callable[[PetState, PetState, PetStats], None]
StatsChangeCallback = Callable[[PetStats], None]


class PetStateMachine:
    """Discrete-tick simulation engine for ByteSprite."""

    HUNGRY_THRESHOLD: float = 25.0
    EXHAUSTED_THRESHOLD: float = 10.0

    # Per-tick awake decay rates
    HUNGER_DECAY_AWAKE: float = 1.2
    ENERGY_DECAY_AWAKE: float = 0.8
    HAPPINESS_DECAY_AWAKE: float = 0.9
    HAPPINESS_DECAY_HUNGRY: float = 1.8

    # Per-tick sleep recovery/decay rates
    ENERGY_RECOVERY_SLEEP: float = 4.5
    HUNGER_DECAY_SLEEP: float = 0.35
    HAPPINESS_DECAY_SLEEP: float = 0.15

    PLAY_DURATION_TICKS: int = 2

    def __init__(
        self,
        stats: PetStats | None = None,
        initial_state: PetState | None = None,
    ) -> None:
        self._stats: PetStats = stats if stats is not None else PetStats()
        self._stats.clamp()
        self._play_ticks_remaining: int = 0
        self._state_callbacks: list[StateChangeCallback] = []
        self._stats_callbacks: list[StatsChangeCallback] = []

        if initial_state is not None and isinstance(initial_state, PetState):
            self._state = initial_state
            if initial_state == PetState.PLAYING:
                self._play_ticks_remaining = self.PLAY_DURATION_TICKS
        else:
            self._state = self._derive_passive_state()

    @property
    def stats(self) -> PetStats:
        return self._stats

    @property
    def state(self) -> PetState:
        return self._state

    def on_state_change(self, callback: StateChangeCallback) -> Callable[[], None]:
        """Register a listener for state transitions; returns an unsubscribe callable."""
        self._state_callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)

        return _unsubscribe

    def on_stats_change(self, callback: StatsChangeCallback) -> Callable[[], None]:
        """Register a listener for stat updates; returns an unsubscribe callable."""
        self._stats_callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._stats_callbacks:
                self._stats_callbacks.remove(callback)

        return _unsubscribe

    def tick(self, elapsed_ticks: int = 1) -> None:
        """Advance simulation by one or more discrete ticks."""
        if elapsed_ticks <= 0:
            return

        for _ in range(elapsed_ticks):
            self._step_single_tick()

        self._stats.last_updated = time.time()
        self._emit_stats_changed()

    def _step_single_tick(self) -> None:
        if self._state == PetState.SLEEPING:
            self._stats.energy += self.ENERGY_RECOVERY_SLEEP
            self._stats.hunger -= self.HUNGER_DECAY_SLEEP
            self._stats.happiness -= self.HAPPINESS_DECAY_SLEEP
            self._stats.clamp()
            if self._stats.energy >= 100.0:
                self._transition_to(self._derive_passive_state())
            return

        # Awake tick degradation
        self._stats.hunger -= self.HUNGER_DECAY_AWAKE
        self._stats.energy -= self.ENERGY_DECAY_AWAKE
        decay_happy = (
            self.HAPPINESS_DECAY_HUNGRY
            if self._stats.hunger <= self.HUNGRY_THRESHOLD
            else self.HAPPINESS_DECAY_AWAKE
        )
        self._stats.happiness -= decay_happy
        self._stats.clamp()

        if self._state == PetState.PLAYING and self._play_ticks_remaining > 0:
            self._play_ticks_remaining -= 1
            if self._stats.energy <= self.EXHAUSTED_THRESHOLD:
                self._play_ticks_remaining = 0
                self._transition_to(PetState.SLEEPING)
            elif self._play_ticks_remaining == 0:
                self._transition_to(self._derive_passive_state())
            return

        self._transition_to(self._derive_passive_state())

    def feed(self, amount: float = 25.0) -> bool:
        """Feed ByteSprite, restoring hunger and granting small happiness + XP."""
        if amount <= 0.0:
            return False
        self._play_ticks_remaining = 0
        self._stats.hunger += amount
        self._stats.happiness += 5.0
        self._stats.experience += 10
        self._stats.last_updated = time.time()
        self._stats.clamp()

        if self._state == PetState.SLEEPING:
            # Feeding wakes the pet gently unless critically exhausted
            target = (
                PetState.SLEEPING
                if self._stats.energy <= self.EXHAUSTED_THRESHOLD
                else self._derive_passive_state()
            )
            self._transition_to(target)
        else:
            self._transition_to(self._derive_passive_state())

        self._emit_stats_changed()
        return True

    def pet(self, amount: float = 15.0) -> bool:
        """Pet ByteSprite, boosting happiness and granting XP."""
        if amount <= 0.0:
            return False
        self._stats.happiness += amount
        self._stats.experience += 5
        self._stats.last_updated = time.time()
        self._stats.clamp()

        if self._state != PetState.SLEEPING and self._state != PetState.PLAYING:
            self._transition_to(self._derive_passive_state())

        self._emit_stats_changed()
        return True

    def play(
        self,
        happiness_gain: float = 20.0,
        energy_cost: float = 10.0,
        hunger_cost: float = 5.0,
    ) -> bool:
        """Engage in active play if ByteSprite has sufficient energy."""
        if self._stats.energy <= max(self.EXHAUSTED_THRESHOLD, energy_cost):
            self._play_ticks_remaining = 0
            self._transition_to(PetState.SLEEPING)
            self._emit_stats_changed()
            return False

        self._stats.energy -= energy_cost
        self._stats.hunger -= hunger_cost
        self._stats.happiness += happiness_gain
        self._stats.experience += 15
        self._stats.last_updated = time.time()
        self._stats.clamp()

        self._play_ticks_remaining = self.PLAY_DURATION_TICKS
        self._transition_to(PetState.PLAYING)
        self._emit_stats_changed()
        return True

    def rest(self) -> bool:
        """Put ByteSprite to sleep so energy recovers over subsequent ticks."""
        self._play_ticks_remaining = 0
        self._stats.energy = min(100.0, self._stats.energy + 2.0)
        self._stats.last_updated = time.time()
        self._stats.clamp()
        self._transition_to(PetState.SLEEPING)
        self._emit_stats_changed()
        return True

    def wake(self) -> bool:
        """Wake ByteSprite from sleep into its appropriate awake mood."""
        if self._state != PetState.SLEEPING:
            return False
        if self._stats.energy <= self.EXHAUSTED_THRESHOLD:
            self._stats.energy = self.EXHAUSTED_THRESHOLD + 5.0
            self._stats.clamp()
        self._stats.last_updated = time.time()
        self._transition_to(self._derive_passive_state())
        self._emit_stats_changed()
        return True

    def _derive_passive_state(self) -> PetState:
        if self._stats.energy <= self.EXHAUSTED_THRESHOLD:
            return PetState.SLEEPING
        if self._stats.hunger <= self.HUNGRY_THRESHOLD:
            return PetState.HUNGRY
        return PetState.IDLE

    def _transition_to(self, next_state: PetState) -> None:
        if next_state == self._state:
            return
        previous = self._state
        self._state = next_state
        for callback in tuple(self._state_callbacks):
            callback(previous, next_state, self._stats)

    def _emit_stats_changed(self) -> None:
        for callback in tuple(self._stats_callbacks):
            callback(self._stats)
