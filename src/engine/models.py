"""Core data models and mood state enumerations for ByteSprite."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PetState(str, Enum):
    """Discrete behavioral and mood states for ByteSprite."""

    IDLE = "idle"
    SLEEPING = "sleeping"
    PLAYING = "playing"
    HUNGRY = "hungry"


def _clamp_stat(value: Any, default: float = 80.0) -> float:
    """Coerce and clamp a primary stat value to [0.0, 100.0]."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(numeric):
        return default
    if math.isinf(numeric):
        return 100.0 if numeric > 0 else 0.0
    return max(0.0, min(100.0, numeric))


def _sanitize_int(value: Any, default: int = 0) -> int:
    """Coerce an integer counter to a non-negative int."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0, int(numeric))


def _sanitize_timestamp(value: Any) -> float:
    """Coerce a timestamp to a finite positive float."""
    if isinstance(value, bool):
        return time.time()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return time.time()
    if not math.isfinite(numeric) or numeric <= 0.0:
        return time.time()
    return numeric


@dataclass
class PetStats:
    """Mutable simulation statistics for ByteSprite."""

    hunger: float = 80.0
    energy: float = 85.0
    happiness: float = 80.0
    experience: int = 0
    last_updated: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.clamp()

    def clamp(self) -> None:
        """Enforce [0.0, 100.0] bounds on primary stats and non-negative XP."""
        self.hunger = _clamp_stat(self.hunger, default=80.0)
        self.energy = _clamp_stat(self.energy, default=85.0)
        self.happiness = _clamp_stat(self.happiness, default=80.0)
        self.experience = _sanitize_int(self.experience, default=0)
        self.last_updated = _sanitize_timestamp(self.last_updated)

    @property
    def level(self) -> int:
        """Derived companion level starting at 1."""
        return 1 + (self.experience // 100)

    def to_dict(self) -> dict[str, float | int]:
        """Serialize stats to a JSON-compatible dictionary."""
        self.clamp()
        return {
            "hunger": round(self.hunger, 2),
            "energy": round(self.energy, 2),
            "happiness": round(self.happiness, 2),
            "experience": self.experience,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PetStats:
        """Reconstruct validated PetStats from a dictionary."""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            hunger=_clamp_stat(raw.get("hunger", 80.0), default=80.0),
            energy=_clamp_stat(raw.get("energy", 85.0), default=85.0),
            happiness=_clamp_stat(raw.get("happiness", 80.0), default=80.0),
            experience=_sanitize_int(raw.get("experience", 0), default=0),
            last_updated=_sanitize_timestamp(raw.get("last_updated", time.time())),
        )
