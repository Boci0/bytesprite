"""Frosted-graphite right-click context menu for ByteSprite Multi-Tool."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QMenu, QWidget, QWidgetAction

from src.engine.models import PetState
from src.engine.pet_state import PetStateMachine
from src.ui.theme import (
    FONT_FAMILY,
    GLASS_RGBA,
    HAIRLINE_RGBA,
    HOVER_RGBA,
    TEXT_DIM_HEX,
    TEXT_HEX,
    rgba_css,
)

MENU_STYLESHEET = f"""
QMenu {{
    background-color: {rgba_css(GLASS_RGBA)};
    color: {TEXT_HEX};
    border: 1px solid {rgba_css(HAIRLINE_RGBA)};
    border-radius: 10px;
    padding: 6px;
    font-family: "{FONT_FAMILY}";
    font-size: 10pt;
}}
QMenu::item {{
    padding: 6px 28px 6px 12px;
    border-radius: 6px;
    background: transparent;
}}
QMenu::item:selected {{
    background-color: {rgba_css(HOVER_RGBA)};
    color: #FFFFFF;
}}
QMenu::item:disabled {{
    color: {TEXT_DIM_HEX};
}}
QMenu::separator {{
    height: 1px;
    background: {rgba_css(HAIRLINE_RGBA)};
    margin: 5px 6px;
}}
QMenu::indicator {{
    width: 14px;
    height: 14px;
    margin-left: 4px;
}}
QMenu::right-arrow {{
    width: 8px;
    height: 8px;
    margin-right: 8px;
}}
"""

QUICK_TOOL_ITEMS: tuple[tuple[str, str], ...] = (
    ("Snip Region -> Copy Image", "snip:image"),
    ("Snip Region -> Copy Text (OCR)", "snip:text"),
    ("Pick Screen Color -> Copy HEX", "snip:color"),
    ("Human Typer...", "typer:open"),
)

_STATE_LABELS: dict[PetState, tuple[str, str]] = {
    PetState.IDLE: ("Online", "#9FD3A8"),
    PetState.PLAYING: ("Focus", "#E8D29F"),
    PetState.HUNGRY: ("Alert", "#E6A0A0"),
    PetState.SLEEPING: ("Do Not Disturb", "#B4B8D8"),
}


def style_menu(menu: QMenu) -> QMenu:
    """Apply the frameless translucent frosted look (rounded corners need a clear backdrop)."""
    menu.setWindowFlags(
        menu.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setStyleSheet(MENU_STYLESHEET)
    return menu


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
        style_menu(self)

        stats = state_machine.stats
        state = state_machine.state
        state_text, state_color = _STATE_LABELS.get(state, (state.value.title(), TEXT_HEX))

        # Header card: name + status dot on line one, live telemetry on line two
        header = QLabel(
            f'<div style="font-size:10.5pt; font-weight:600; color:{TEXT_HEX};">ByteSprite'
            f'&nbsp;&nbsp;<span style="color:{state_color}; font-size:9pt;">&#9679; {state_text}</span></div>'
            f'<div style="font-size:8.5pt; color:{TEXT_DIM_HEX};">'
            f"CPU {int(round(stats.cpu_usage))}%&nbsp;&nbsp;&middot;&nbsp;&nbsp;"
            f"RAM {int(round(stats.ram_usage))}%&nbsp;&nbsp;&middot;&nbsp;&nbsp;"
            f"Disk {int(round(stats.disk_usage))}%</div>"
        )
        header.setStyleSheet(f'background: transparent; padding: 6px 12px 4px 12px; font-family: "{FONT_FAMILY}";')
        self.header_action = QWidgetAction(self)
        self.header_action.setDefaultWidget(header)
        self.header_action.setEnabled(False)
        self.addAction(self.header_action)
        self.addSeparator()

        def connect(act: QAction, cid: str) -> None:
            if on_tool_action is not None:
                act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            else:
                act.triggered.connect(
                    lambda _checked=False, c=cid: state_machine.execute_tool_action(c)
                )

        # Tools: screenshot/typing first (most used), then plugin submenus
        self.plugin_actions: dict[str, QAction] = {}
        self.plugin_submenus: dict[str, QMenu] = {}
        self.quick_menu = style_menu(QMenu("Screenshot && Typing", self))
        for label, cid in QUICK_TOOL_ITEMS:
            q_act = QAction(label, self.quick_menu)
            if on_tool_action is not None:
                q_act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            self.quick_menu.addAction(q_act)
            self.plugin_actions[cid] = q_act
        self.addMenu(self.quick_menu)

        top_level_items = []
        for item in state_machine.get_active_menu_items():
            if item.callback_id in {"sysmon:refresh", "pomodoro:toggle"}:
                continue
            if not item.submenu:
                top_level_items.append(item)
                continue
            sub = self.plugin_submenus.get(item.submenu)
            if sub is None:
                # "&&" renders a literal "&" instead of a mnemonic underline
                sub = style_menu(QMenu(item.submenu.replace("&", "&&"), self))
                self.addMenu(sub)
                self.plugin_submenus[item.submenu] = sub
            p_act = QAction(item.label, sub)
            p_act.setEnabled(item.enabled)
            if item.checkable:
                p_act.setCheckable(True)
                p_act.setChecked(item.checked)
            connect(p_act, item.callback_id)
            sub.addAction(p_act)
            self.plugin_actions[item.callback_id] = p_act

        self.addSeparator()

        # Focus timer group
        pomo = state_machine.pomodoro_plugin
        pomo_label = (
            "Pause Pomodoro Timer"
            if (pomo is not None and pomo.running)
            else "Start / Resume Pomodoro (25m)"
        )
        self.play_action = QAction(pomo_label, self)
        self.play_action.triggered.connect(lambda _checked=False: on_play())
        self.addAction(self.play_action)
        for item in top_level_items:
            p_act = QAction(item.label, self)
            p_act.setEnabled(item.enabled)
            if item.checkable:
                p_act.setCheckable(True)
                p_act.setChecked(item.checked)
            connect(p_act, item.callback_id)
            self.addAction(p_act)
            self.plugin_actions[item.callback_id] = p_act

        self.addSeparator()

        # HUD view switcher (radio list) + metrics refresh
        self.hud_menu = style_menu(QMenu("HUD View", self))
        self.tool_view_actions: dict[str, QAction] = {}
        for plugin in state_machine.registry.plugins:
            tid = plugin.tool_id
            act = QAction(plugin.display_name, self.hud_menu)
            act.setCheckable(True)
            act.setChecked(tid == state_machine.active_tool_id)
            if on_select_tool is not None:
                act.triggered.connect(lambda _checked=False, t=tid: on_select_tool(t))
            else:
                act.triggered.connect(
                    lambda _checked=False, t=tid: state_machine.select_tool(t)
                )
            self.hud_menu.addAction(act)
            self.tool_view_actions[tid] = act
        self.hud_menu.addSeparator()
        if on_pet is not None:
            self.pet_action: QAction | None = QAction("Cycle View (Click Sprite)", self.hud_menu)
            self.pet_action.triggered.connect(lambda _checked=False: on_pet())
            self.hud_menu.addAction(self.pet_action)
        else:
            self.pet_action = None
        self.feed_action = QAction("Refresh System Metrics", self.hud_menu)
        self.feed_action.triggered.connect(lambda _checked=False: on_feed())
        self.hud_menu.addAction(self.feed_action)
        self.addMenu(self.hud_menu)

        # Window settings
        self.settings_menu = style_menu(QMenu("Settings", self))
        self.always_on_top_action = QAction("Always on Top", self.settings_menu)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.setChecked(always_on_top)
        self.always_on_top_action.toggled.connect(on_toggle_on_top)
        self.settings_menu.addAction(self.always_on_top_action)

        self.auto_wander_action = QAction("Gentle Wander", self.settings_menu)
        self.auto_wander_action.setCheckable(True)
        self.auto_wander_action.setChecked(auto_wander)
        self.auto_wander_action.toggled.connect(on_toggle_wander)
        self.settings_menu.addAction(self.auto_wander_action)

        self.reset_position_action = QAction("Reset Position", self.settings_menu)
        self.reset_position_action.triggered.connect(
            lambda _checked=False: on_reset_position()
        )
        self.settings_menu.addAction(self.reset_position_action)
        self.addMenu(self.settings_menu)

        sleep_label = (
            "Exit Do Not Disturb"
            if state == PetState.SLEEPING
            else "Do Not Disturb"
        )
        self.sleep_action = QAction(sleep_label, self)
        self.sleep_action.triggered.connect(lambda _checked=False: on_toggle_sleep())
        self.addAction(self.sleep_action)

        self.addSeparator()

        self.quit_action = QAction("Quit ByteSprite", self)
        self.quit_action.triggered.connect(lambda _checked=False: on_quit())
        self.addAction(self.quit_action)
