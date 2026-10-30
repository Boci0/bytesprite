"""Dark pixel-styled right-click context menu for ByteSprite Multi-Tool."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QWidget

from src.engine.models import PetState
from src.engine.pet_state import PetStateMachine

MENU_STYLESHEET = """
QMenu {
    background-color: #181425;
    color: #F0F7F4;
    border: 2px solid #3A4466;
    padding: 6px 4px;
    font-family: "Consolas", "Cascadia Mono", "Segoe UI", monospace;
    font-size: 12px;
}
QMenu::item {
    padding: 6px 22px 6px 14px;
    border-radius: 3px;
    margin: 1px 4px;
}
QMenu::item:selected {
    background-color: #263859;
    color: #73EFF7;
}
QMenu::item:disabled {
    color: #8B9BB4;
    font-weight: bold;
}
QMenu::separator {
    height: 1px;
    background: #3A4466;
    margin: 5px 8px;
}
QMenu::indicator {
    width: 12px;
    height: 12px;
    margin-left: 6px;
}
"""


QUICK_TOOL_ITEMS: tuple[tuple[str, str], ...] = (
    ("Snip Region -> Copy Image", "snip:image"),
    ("Snip Region -> Copy Text (OCR)", "snip:text"),
    ("Pick Screen Color -> Copy HEX", "snip:color"),
    ("Human Typer...", "typer:open"),
)


class PetContextMenu(QMenu):
    """Context menu providing multi-tool actions, HUD switcher, and window toggles."""

    def __init__(
        self,
        *,
        state_machine: PetStateMachine,
        always_on_top: bool,
        auto_wander: bool,
        on_feed: Callable[[], object],
        on_play: Callable[[], object],
        on_toggle_sleep: Callable[[], object],
        on_reset_position: Callable[[], object],
        on_toggle_on_top: Callable[[bool], object],
        on_toggle_wander: Callable[[bool], object],
        on_quit: Callable[[], object],
        on_pet: Callable[[], object] | None = None,
        on_select_tool: Callable[[str], object] | None = None,
        on_tool_action: Callable[[str], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(MENU_STYLESHEET)

        stats = state_machine.stats
        state = state_machine.state
        active_plugin = state_machine.active_plugin
        tool_title = active_plugin.display_name if active_plugin is not None else "Multi-Tool"

        state_label_map = {
            PetState.IDLE: "ONLINE",
            PetState.PLAYING: "FOCUS",
            PetState.HUNGRY: "ALERT",
            PetState.SLEEPING: "DND",
        }
        state_badge = state_label_map.get(state, state.value.upper())

        header_text = f"ByteSprite [{state_badge}] - {tool_title}"
        self.header_action = QAction(header_text, self)
        self.header_action.setEnabled(False)
        self.addAction(self.header_action)

        stats_text = (
            f"CPU {int(round(stats.cpu_usage))}% | "
            f"RAM {int(round(stats.ram_usage))}% | "
            f"DSK {int(round(stats.disk_usage))}%"
        )
        self.stats_action = QAction(stats_text, self)
        self.stats_action.setEnabled(False)
        self.addAction(self.stats_action)

        self.addSeparator()

        # Tool View Switcher
        self.tool_view_actions: dict[str, QAction] = {}
        for plugin in state_machine.registry.plugins:
            tid = plugin.tool_id
            act = QAction(f"HUD: {plugin.display_name}", self)
            act.setCheckable(True)
            act.setChecked(tid == state_machine.active_tool_id)
            if on_select_tool is not None:
                act.triggered.connect(lambda _checked=False, t=tid: on_select_tool(t))
            else:
                act.triggered.connect(
                    lambda _checked=False, t=tid: state_machine.select_tool(t)
                )
            self.addAction(act)
            self.tool_view_actions[tid] = act

        if on_pet is not None:
            self.pet_action: QAction | None = QAction("Cycle HUD View (Click Sprite)", self)
            self.pet_action.triggered.connect(lambda _checked=False: on_pet())
            self.addAction(self.pet_action)
        else:
            self.pet_action = None

        self.addSeparator()

        # Primary Multi-Tool Quick Actions (System Monitor + Pomodoro)
        self.feed_action = QAction("Refresh System Metrics", self)
        self.feed_action.triggered.connect(lambda _checked=False: on_feed())
        self.addAction(self.feed_action)

        pomo = state_machine.pomodoro_plugin
        pomo_label = (
            "Pause Pomodoro Timer"
            if (pomo is not None and pomo.running)
            else "Start / Resume Pomodoro (25m)"
        )
        self.play_action = QAction(pomo_label, self)
        self.play_action.triggered.connect(lambda _checked=False: on_play())
        self.addAction(self.play_action)

        # Additional plugin-contributed actions (e.g. Break, Reset, File Forge, Clip Vault)
        self.plugin_actions: dict[str, QAction] = {}
        self.plugin_submenus: dict[str, QMenu] = {}
        for item in state_machine.get_active_menu_items():
            if item.callback_id in {"sysmon:refresh", "pomodoro:toggle"}:
                continue
            target_menu: QMenu = self
            if item.submenu:
                sub = self.plugin_submenus.get(item.submenu)
                if sub is None:
                    sub = QMenu(item.submenu, self)
                    sub.setStyleSheet(MENU_STYLESHEET)
                    self.addMenu(sub)
                    self.plugin_submenus[item.submenu] = sub
                target_menu = sub

            p_act = QAction(item.label, target_menu)
            p_act.setEnabled(item.enabled)
            if item.checkable:
                p_act.setCheckable(True)
                p_act.setChecked(item.checked)
            cid = item.callback_id
            if on_tool_action is not None:
                p_act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            else:
                p_act.triggered.connect(
                    lambda _checked=False, c=cid: state_machine.execute_tool_action(c)
                )
            target_menu.addAction(p_act)
            self.plugin_actions[cid] = p_act

        # Screen & typing quick tools are UI-only actions routed through on_tool_action
        self.quick_menu = QMenu("Screen & Typing", self)
        self.quick_menu.setStyleSheet(MENU_STYLESHEET)
        for label, cid in QUICK_TOOL_ITEMS:
            q_act = QAction(label, self.quick_menu)
            if on_tool_action is not None:
                q_act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            self.quick_menu.addAction(q_act)
            self.plugin_actions[cid] = q_act
        self.addMenu(self.quick_menu)

        sleep_label = (
            "Exit Standby (Wake Up)"
            if state == PetState.SLEEPING
            else "Do Not Disturb (Standby)"
        )
        self.sleep_action = QAction(sleep_label, self)
        self.sleep_action.triggered.connect(lambda _checked=False: on_toggle_sleep())
        self.addAction(self.sleep_action)

        self.addSeparator()

        self.always_on_top_action = QAction("Always on Top", self)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.setChecked(always_on_top)
        self.always_on_top_action.toggled.connect(on_toggle_on_top)
        self.addAction(self.always_on_top_action)

        self.auto_wander_action = QAction("Gentle Wander", self)
        self.auto_wander_action.setCheckable(True)
        self.auto_wander_action.setChecked(auto_wander)
        self.auto_wander_action.toggled.connect(on_toggle_wander)
        self.addAction(self.auto_wander_action)

        self.reset_position_action = QAction("Reset Position", self)
        self.reset_position_action.triggered.connect(
            lambda _checked=False: on_reset_position()
        )
        self.addAction(self.reset_position_action)

        self.addSeparator()

        self.quit_action = QAction("Quit", self)
        self.quit_action.triggered.connect(lambda _checked=False: on_quit())
        self.addAction(self.quit_action)
