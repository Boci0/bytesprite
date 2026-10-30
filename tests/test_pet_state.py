"""Unit tests for PetStats, ToolRegistry, SystemMonitorPlugin, PomodoroPlugin, and PetStateMachine."""

from __future__ import annotations

import math
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, PetState, PetStats, ToolMenuItem
from src.engine.pet_state import PetStateMachine
from src.tools.base import ToolRegistry
from src.tools.pomodoro import PomodoroPhase, PomodoroPlugin
from src.tools.system_monitor import SystemMetrics, SystemMonitorPlugin, get_system_metrics


class _DummyTool:
    """Minimal custom plugin proving zero-friction extensibility."""

    def __init__(self) -> None:
        self.ticks = 0

    @property
    def tool_id(self) -> str:
        return "custom_dummy"

    @property
    def display_name(self) -> str:
        return "Custom Dummy Tool"

    @property
    def tick_interval_ms(self) -> int:
        return 5000

    def tick(self) -> bool:
        self.ticks += 1
        return True

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        return HudData(
            badge_left="DUMMY",
            badge_right="OK",
            bars=(HudBar(label="TK", value=float(self.ticks)),),
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        return [ToolMenuItem(label="Ping Dummy", callback_id="dummy:ping")]

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id == "dummy:ping":
            self.ticks += 10
            return (True, "PONG!", "#73EFF7")
        return (False, "", "")

    def to_dict(self) -> dict[str, Any]:
        return {"ticks": self.ticks}

    def load_dict(self, data: dict[str, Any]) -> None:
        self.ticks = int(data.get("ticks", 0))


class TestPetStats:
    def test_default_stats_within_bounds(self) -> None:
        stats = PetStats()
        assert 0.0 <= stats.cpu_usage <= 100.0
        assert 0.0 <= stats.ram_usage <= 100.0
        assert 0.0 <= stats.disk_usage <= 100.0
        assert stats.completed_sessions == 0
        assert stats.last_updated > 0.0
        assert stats.level == 1

    def test_boundary_clamps_on_construction(self) -> None:
        stats = PetStats(
            cpu_usage=150.0,
            ram_usage=-25.0,
            disk_usage=999.0,
            completed_sessions=-10,
            last_updated=1000.0,
        )
        assert stats.cpu_usage == 100.0
        assert stats.ram_usage == 0.0
        assert stats.disk_usage == 100.0
        assert stats.completed_sessions == 0

    def test_nan_and_inf_sanitized(self) -> None:
        stats = PetStats(
            cpu_usage=math.nan,
            ram_usage=math.inf,
            disk_usage=-math.inf,
        )
        assert 0.0 <= stats.cpu_usage <= 100.0
        assert stats.ram_usage == 100.0
        assert stats.disk_usage == 0.0


class TestMultiToolEngine:
    def test_live_win32_system_metrics_within_bounds(self) -> None:
        metrics = get_system_metrics()
        assert 0.0 <= metrics.cpu_percent <= 100.0
        assert 0.0 <= metrics.ram_percent <= 100.0
        assert 0.0 <= metrics.disk_percent <= 100.0

    def test_system_monitor_alert_threshold_triggers_hungry_alert_state(self) -> None:
        current = [SystemMetrics(cpu_percent=22.0, ram_percent=45.0, disk_percent=60.0)]
        sysmon = SystemMonitorPlugin(sampler=lambda: current[0])
        pomo = PomodoroPlugin()
        registry = ToolRegistry([sysmon, pomo])
        sm = PetStateMachine(registry=registry)

        assert sm.state == PetState.IDLE
        hud = sm.get_hud_data()
        assert hud.badge_left == "SYS.MON"
        assert hud.badge_right == "OK"
        assert len(hud.bars) == 3

        # Spike CPU above 90% threshold
        current[0] = SystemMetrics(cpu_percent=95.0, ram_percent=50.0, disk_percent=60.0)
        sm.tick()
        assert sm.state == PetState.HUNGRY  # CompanionState.ALERT
        assert sm.stats.cpu_usage == 95.0
        assert sm.get_hud_data().badge_right == "HIGH"

        # Drop back to normal
        current[0] = SystemMetrics(cpu_percent=18.0, ram_percent=50.0, disk_percent=60.0)
        sm.refresh_metrics()
        assert sm.state == PetState.IDLE
        assert sm.stats.cpu_usage == 18.0

    def test_pomodoro_focus_and_break_lifecycle(self) -> None:
        pomo = PomodoroPlugin(work_seconds=3, break_seconds=2)
        sysmon = SystemMonitorPlugin(
            sampler=lambda: SystemMetrics(cpu_percent=15.0, ram_percent=40.0, disk_percent=50.0)
        )
        sm = PetStateMachine(registry=ToolRegistry([sysmon, pomo]))

        # Start focus timer
        running = sm.toggle_pomodoro()
        assert running is True
        assert sm.active_tool_id == "pomodoro"
        assert sm.state == PetState.PLAYING  # CompanionState.FOCUS
        assert pomo.phase == PomodoroPhase.WORK

        # Tick 3 seconds to complete work session -> auto-transitions to break
        sm.tick(elapsed_ticks=3)
        assert pomo.completed_sessions == 1
        assert sm.stats.completed_sessions == 1
        assert pomo.phase == PomodoroPhase.BREAK
        assert sm.state == PetState.SLEEPING  # Break / DND state

        # Tick 2 seconds to finish break -> returns to IDLE
        sm.tick(elapsed_ticks=2)
        assert pomo.phase == PomodoroPhase.IDLE
        assert pomo.running is False
        assert sm.state == PetState.IDLE

    def test_cycle_tool_and_custom_plugin_registration(self) -> None:
        sysmon = SystemMonitorPlugin(
            sampler=lambda: SystemMetrics(cpu_percent=10.0, ram_percent=30.0, disk_percent=40.0)
        )
        pomo = PomodoroPlugin()
        dummy = _DummyTool()
        registry = ToolRegistry([sysmon, pomo, dummy])
        sm = PetStateMachine(registry=registry)

        assert sm.active_tool_id == "sysmon"
        sm.cycle_tool()
        assert sm.active_tool_id == "pomodoro"
        sm.cycle_tool()
        assert sm.active_tool_id == "custom_dummy"
        assert sm.get_hud_data().badge_left == "DUMMY"

        handled, toast, _color = sm.execute_tool_action("dummy:ping")
        assert handled is True
        assert toast == "PONG!"
        assert dummy.ticks == 10

    def test_dnd_standby_and_wake_transitions(self) -> None:
        events: list[tuple[PetState, PetState]] = []
        sm = PetStateMachine()
        unsubscribe = sm.on_state_change(lambda old, new, _stats: events.append((old, new)))

        sm.rest()
        assert sm.state == PetState.SLEEPING
        assert len(events) == 1
        assert events[0] == (PetState.IDLE, PetState.SLEEPING)

        # Duplicate rest does not re-emit
        sm.rest()
        assert len(events) == 1

        sm.wake()
        assert sm.state == PetState.IDLE
        assert len(events) == 2

        unsubscribe()
        sm.rest()
        assert len(events) == 2


def test_pomodoro_long_break_lengths_and_phase_notices() -> None:
    pomo = PomodoroPlugin(work_seconds=2, break_seconds=1)
    pomo.completed_sessions = 3
    pomo.start_work()
    pomo.tick(2)
    # 4th session completed -> long break and a FOCUS DONE notice
    assert pomo.phase == PomodoroPhase.BREAK
    assert pomo.remaining_seconds == PomodoroPlugin.LONG_BREAK_SECONDS
    assert pomo.pop_notification() == ("FOCUS DONE", "#73EFF7")
    assert pomo.pop_notification() is None
    pomo.tick(PomodoroPlugin.LONG_BREAK_SECONDS)
    assert pomo.phase == PomodoroPhase.IDLE
    assert pomo.pop_notification()[0] == "BREAK OVER"

    assert pomo.execute_action("pomodoro:work:50")[0] is True
    assert pomo.execute_action("pomodoro:brk:10")[0] is True
    assert pomo.remaining_seconds == 50 * 60  # idle -> applies immediately
    restored = PomodoroPlugin()
    restored.load_dict(pomo.to_dict())
    assert restored.work_minutes == 50
    assert any(i.callback_id == "pomodoro:brk:10" and i.checked for i in restored.get_menu_items())
