"""Pomodoro Focus Timer tool plugin for ByteSprite."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem


class PomodoroPhase(str, Enum):
    """Operational phase of the Pomodoro focus timer."""

    IDLE = "idle"
    WORK = "work"
    BREAK = "break"


class PomodoroPlugin:
    """25m/5m Pomodoro countdown tool with daily goal tracking."""

    DEFAULT_WORK_SECONDS: int = 25 * 60
    DEFAULT_BREAK_SECONDS: int = 5 * 60
    LONG_BREAK_SECONDS: int = 15 * 60
    DAILY_GOAL_SESSIONS: int = 4
    WORK_PRESETS_MIN: tuple[int, ...] = (15, 25, 50)
    BREAK_PRESETS_MIN: tuple[int, ...] = (5, 10)
    SUBMENU_NAME: str = "Pomodoro Length"

    def __init__(
        self,
        work_seconds: int = DEFAULT_WORK_SECONDS,
        break_seconds: int = DEFAULT_BREAK_SECONDS,
    ) -> None:
        self._work_seconds = max(1, int(work_seconds))
        self._break_seconds = max(1, int(break_seconds))
        self._phase: PomodoroPhase = PomodoroPhase.IDLE
        self._running: bool = False
        self._remaining_seconds: int = self._work_seconds
        self._total_seconds: int = self._work_seconds
        self._completed_sessions: int = 0
        self._total_focus_seconds: int = 0
        self._notice: tuple[str, str] | None = None

    @property
    def tool_id(self) -> str:
        return "pomodoro"

    @property
    def display_name(self) -> str:
        return "Pomodoro"

    @property
    def tick_interval_ms(self) -> int:
        return 1_000

    @property
    def phase(self) -> PomodoroPhase:
        return self._phase

    @property
    def running(self) -> bool:
        return self._running

    @property
    def remaining_seconds(self) -> int:
        return self._remaining_seconds

    @property
    def completed_sessions(self) -> int:
        return self._completed_sessions

    @completed_sessions.setter
    def completed_sessions(self, value: int) -> None:
        self._completed_sessions = max(0, int(value))

    def start_work(self) -> None:
        self._phase = PomodoroPhase.WORK
        self._total_seconds = self._work_seconds
        self._remaining_seconds = self._work_seconds
        self._running = True

    def start_break(self, seconds: int | None = None) -> None:
        self._phase = PomodoroPhase.BREAK
        self._total_seconds = seconds or self._break_seconds
        self._remaining_seconds = self._total_seconds
        self._running = True

    def _long_break_due(self) -> bool:
        return self._completed_sessions > 0 and self._completed_sessions % self.DAILY_GOAL_SESSIONS == 0

    def set_lengths(self, *, work_seconds: int | None = None, break_seconds: int | None = None) -> None:
        """Change focus/break lengths; applies now when idle, otherwise from the next phase."""
        if work_seconds is not None:
            self._work_seconds = max(1, int(work_seconds))
        if break_seconds is not None:
            self._break_seconds = max(1, int(break_seconds))
        if self._phase == PomodoroPhase.IDLE:
            self._total_seconds = self._work_seconds
            self._remaining_seconds = self._work_seconds

    def pop_notification(self) -> tuple[str, str] | None:
        notice, self._notice = self._notice, None
        return notice

    def toggle_pause(self) -> bool:
        if self._phase == PomodoroPhase.IDLE:
            self.start_work()
            return True
        self._running = not self._running
        return self._running

    def reset(self) -> None:
        self._running = False
        self._phase = PomodoroPhase.IDLE
        self._total_seconds = self._work_seconds
        self._remaining_seconds = self._work_seconds

    def tick(self, elapsed_seconds: int = 1) -> bool:
        """Advance countdown when running; returns True if timer changed or completed."""
        if not self._running or elapsed_seconds <= 0:
            return False

        step = min(self._remaining_seconds, elapsed_seconds)
        self._remaining_seconds -= step
        if self._phase == PomodoroPhase.WORK:
            self._total_focus_seconds += step

        if self._remaining_seconds <= 0:
            if self._phase == PomodoroPhase.WORK:
                self._completed_sessions += 1
                self.start_break(self.LONG_BREAK_SECONDS if self._long_break_due() else None)
                self._notice = ("FOCUS DONE", "#73EFF7")
            else:
                self.reset()
                self._notice = ("BREAK OVER", "#FFCD75")
        return True

    def desired_state(self) -> CompanionState | None:
        if not self._running:
            return None
        if self._phase == PomodoroPhase.WORK:
            return CompanionState.FOCUS
        if self._phase == PomodoroPhase.BREAK:
            return CompanionState.DND
        return None

    def format_remaining(self) -> str:
        mins = self._remaining_seconds // 60
        secs = self._remaining_seconds % 60
        return f"{mins:02d}:{secs:02d}"

    def get_hud_data(self) -> HudData:
        if self._phase == PomodoroPhase.WORK:
            badge_right = "FOCUS" if self._running else "PAUSE"
            badge_color = "#FFCD75" if self._running else "#8A9AF2"
            bar_color = "#FFCD75"
        elif self._phase == PomodoroPhase.BREAK:
            badge_right = "BREAK" if self._running else "PAUSE"
            badge_color = "#73EFF7"
            bar_color = "#73EFF7"
        else:
            badge_right = "READY"
            badge_color = "#38B764"
            bar_color = "#38B764"

        pct_left = (
            (self._remaining_seconds / max(1, self._total_seconds)) * 100.0
            if self._total_seconds > 0
            else 0.0
        )
        goal_pct = min(100.0, ((self._completed_sessions % self.DAILY_GOAL_SESSIONS) / self.DAILY_GOAL_SESSIONS) * 100.0)
        if self._completed_sessions > 0 and (self._completed_sessions % self.DAILY_GOAL_SESSIONS) == 0:
            goal_pct = 100.0

        focus_mins = self._total_focus_seconds // 60
        streak_pct = min(100.0, (focus_mins / 100.0) * 100.0)

        mins_left = int(math.ceil(self._remaining_seconds / 60.0))
        if self._phase == PomodoroPhase.BREAK:
            detail = "Break: stand up and stretch"
        else:
            nth = self._completed_sessions % self.DAILY_GOAL_SESSIONS + 1
            detail = f"Session {nth}/{self.DAILY_GOAL_SESSIONS}" + (
                ", long break next" if nth == self.DAILY_GOAL_SESSIONS else ""
            )
        return HudData(
            badge_left=f"PM.{self.format_remaining()}",
            badge_right=f"{badge_right} {self.format_remaining()}",
            badge_right_color=badge_color,
            bars=(
                HudBar(
                    label="Left",
                    value=pct_left,
                    color_hex=bar_color,
                    display_text=f"{mins_left:2d}m",
                ),
                HudBar(
                    label="Done",
                    value=goal_pct,
                    color_hex="#41A6F6",
                    display_text=f"{self._completed_sessions:3d}",
                ),
                HudBar(
                    label="Focus",
                    value=streak_pct,
                    color_hex="#FF77A8",
                    display_text=f"{focus_mins:2d}m",
                ),
            ),
            detail=detail,
        )

    @property
    def work_minutes(self) -> int:
        return max(1, self._work_seconds // 60)

    def get_menu_items(self) -> list[ToolMenuItem]:
        toggle_label = (
            "Pause Pomodoro"
            if self._running
            else (
                "Resume Pomodoro"
                if self._phase != PomodoroPhase.IDLE
                else f"Start {self.work_minutes}m Focus"
            )
        )
        items = [
            ToolMenuItem(label=toggle_label, callback_id="pomodoro:toggle"),
            ToolMenuItem(label=f"Start {self._break_seconds // 60}m Break", callback_id="pomodoro:break"),
            ToolMenuItem(label="Reset Timer", callback_id="pomodoro:reset"),
        ]
        for mins in self.WORK_PRESETS_MIN:
            items.append(ToolMenuItem(
                label=f"Focus {mins}m", callback_id=f"pomodoro:work:{mins}",
                checkable=True, checked=self._work_seconds == mins * 60, submenu=self.SUBMENU_NAME,
            ))
        for mins in self.BREAK_PRESETS_MIN:
            items.append(ToolMenuItem(
                label=f"Break {mins}m", callback_id=f"pomodoro:brk:{mins}",
                checkable=True, checked=self._break_seconds == mins * 60, submenu=self.SUBMENU_NAME,
            ))
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id == "pomodoro:toggle":
            running = self.toggle_pause()
            if running:
                return (True, "FOCUS!", "#FFCD75")
            return (True, "PAUSED", "#8A9AF2")
        if callback_id == "pomodoro:break":
            self.start_break()
            return (True, "BREAK", "#73EFF7")
        if callback_id == "pomodoro:reset":
            self.reset()
            return (True, "RESET", "#38B764")
        kind, _, mins = callback_id.removeprefix("pomodoro:").partition(":")
        if kind in ("work", "brk") and mins.isdigit():
            if kind == "work":
                self.set_lengths(work_seconds=int(mins) * 60)
            else:
                self.set_lengths(break_seconds=int(mins) * 60)
            return (True, f"{mins}m SET", "#38B764")
        return (False, "", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self._phase.value,
            "running": self._running,
            "remaining_seconds": self._remaining_seconds,
            "total_seconds": self._total_seconds,
            "completed_sessions": self._completed_sessions,
            "total_focus_seconds": self._total_focus_seconds,
            "work_seconds": self._work_seconds,
            "break_seconds": self._break_seconds,
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        raw_phase = data.get("phase", PomodoroPhase.IDLE.value)
        try:
            self._phase = PomodoroPhase(raw_phase)
        except ValueError:
            self._phase = PomodoroPhase.IDLE

        if isinstance(data.get("running"), bool):
            self._running = data["running"]
        try:
            rem = int(data.get("remaining_seconds", self._work_seconds))
            tot = int(data.get("total_seconds", self._work_seconds))
            sessions = int(data.get("completed_sessions", 0))
            focus_sec = int(data.get("total_focus_seconds", 0))
            self._work_seconds = max(1, int(data.get("work_seconds", self._work_seconds)))
            self._break_seconds = max(1, int(data.get("break_seconds", self._break_seconds)))
            self._remaining_seconds = max(0, rem)
            self._total_seconds = max(1, tot)
            self._completed_sessions = max(0, sessions)
            self._total_focus_seconds = max(0, focus_sec)
        except (TypeError, ValueError):
            pass
