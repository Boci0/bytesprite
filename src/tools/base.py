"""Lightweight extensible plugin contract and registry for ByteSprite multi-tools."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from src.engine.models import CompanionState, HudData, ToolMenuItem


@runtime_checkable
class ToolPlugin(Protocol):
    """Protocol implemented by all ByteSprite utility tools."""

    @property
    def tool_id(self) -> str:
        """Unique machine identifier for the tool (e.g. 'sysmon', 'pomodoro')."""

    @property
    def display_name(self) -> str:
        """Human-readable label shown in the context menu."""

    @property
    def tick_interval_ms(self) -> int:
        """Desired polling/update cadence in milliseconds when active."""

    def tick(self) -> bool:
        """Advance tool state by one tick; return True if HUD or state changed."""

    def desired_state(self) -> CompanionState | None:
        """Return an override CompanionState if the tool demands attention/focus, or None."""

    def get_hud_data(self) -> HudData:
        """Return the current 3-bar pixel HUD representation for this tool."""

    def get_menu_items(self) -> list[ToolMenuItem]:
        """Return quick-action menu items specific to this tool."""

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        """Execute a menu action by ID; returns (handled, toast_text, toast_color_hex)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize persistent tool settings/state to a JSON-compatible dict."""

    def load_dict(self, data: dict[str, Any]) -> None:
        """Restore persistent tool settings/state from a dict."""


class ToolRegistry:
    """Manages registered ToolPlugins, active HUD view switching, and state aggregation."""

    def __init__(self, plugins: list[ToolPlugin] | None = None) -> None:
        self._plugins: dict[str, ToolPlugin] = {}
        self._order: list[str] = []
        self._active_id: str = ""
        if plugins:
            for plugin in plugins:
                self.register(plugin)

    def register(self, plugin: ToolPlugin) -> None:
        """Register a tool plugin in display order."""
        tid = plugin.tool_id
        if tid not in self._plugins:
            self._order.append(tid)
        self._plugins[tid] = plugin
        if not self._active_id:
            self._active_id = tid

    def unregister(self, tool_id: str) -> None:
        """Remove a registered plugin by tool_id."""
        if tool_id in self._plugins:
            del self._plugins[tool_id]
            self._order.remove(tool_id)
            if self._active_id == tool_id:
                self._active_id = self._order[0] if self._order else ""

    @property
    def plugins(self) -> list[ToolPlugin]:
        return [self._plugins[tid] for tid in self._order]

    @property
    def active_tool_id(self) -> str:
        return self._active_id

    @property
    def active_plugin(self) -> ToolPlugin | None:
        return self._plugins.get(self._active_id)

    def get_plugin(self, tool_id: str) -> ToolPlugin | None:
        return self._plugins.get(tool_id)

    def set_active_tool(self, tool_id: str) -> bool:
        if tool_id not in self._plugins:
            return False
        self._active_id = tool_id
        return True

    def cycle_active_tool(self) -> ToolPlugin | None:
        """Switch the active HUD view to the next registered tool plugin."""
        if not self._order:
            return None
        if self._active_id not in self._order:
            self._active_id = self._order[0]
            return self._plugins[self._active_id]
        idx = self._order.index(self._active_id)
        next_idx = (idx + 1) % len(self._order)
        self._active_id = self._order[next_idx]
        return self._plugins[self._active_id]

    def tick_all(self) -> bool:
        """Tick all registered tools and return True if any updated."""
        changed = False
        for tid in self._order:
            if self._plugins[tid].tick():
                changed = True
        return changed

    def min_active_tick_interval_ms(self, default_ms: int) -> int:
        """Return the smallest tick_interval_ms among actively running/busy plugins."""
        fastest = default_ms
        for tid in self._order:
            plugin = self._plugins[tid]
            is_active = bool(
                getattr(plugin, "running", False)
                or getattr(plugin, "busy", False)
                or getattr(plugin, "has_pending_update", False)
            )
            if is_active:
                fastest = min(fastest, max(100, int(plugin.tick_interval_ms)))
        return fastest

    def pop_notifications(self) -> list[tuple[str, str]]:
        """Drain pending (toast_text, color_hex) notifications from plugins."""
        notifications: list[tuple[str, str]] = []
        for tid in self._order:
            popper = getattr(self._plugins[tid], "pop_notification", None)
            if callable(popper):
                item = popper()
                if isinstance(item, tuple) and len(item) == 2:
                    notifications.append((str(item[0]), str(item[1])))
        return notifications

    def close_all(self) -> None:
        """Release background workers or OS listeners held by registered plugins."""
        for tid in self._order:
            closer = getattr(self._plugins[tid], "close", None)
            if callable(closer):
                closer()
