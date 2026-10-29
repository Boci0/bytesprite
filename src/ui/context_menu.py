"""Dark pixel-styled right-click context menu for ByteSprite."""

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


class PetContextMenu(QMenu):
    """Context menu providing quick care actions and window toggles for ByteSprite."""

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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(MENU_STYLESHEET)

        stats = state_machine.stats
        state = state_machine.state

        header_text = (
            f"ByteSprite Lv.{stats.level} [{state.value.upper()}]"
        )
        self.header_action = QAction(header_text, self)
        self.header_action.setEnabled(False)
        self.addAction(self.header_action)

        stats_text = (
            f"Food {int(round(stats.hunger))}% | "
            f"Energy {int(round(stats.energy))}% | "
            f"Happy {int(round(stats.happiness))}%"
        )
        self.stats_action = QAction(stats_text, self)
        self.stats_action.setEnabled(False)
        self.addAction(self.stats_action)

        self.addSeparator()

        self.feed_action = QAction("Feed Berry (+Food)", self)
        self.feed_action.triggered.connect(lambda _checked=False: on_feed())
        self.addAction(self.feed_action)

        self.play_action = QAction("Play Game (+Happy)", self)
        self.play_action.triggered.connect(lambda _checked=False: on_play())
        self.addAction(self.play_action)

        if on_pet is not None:
            self.pet_action: QAction | None = QAction("Pet Companion", self)
            self.pet_action.triggered.connect(lambda _checked=False: on_pet())
            self.addAction(self.pet_action)
        else:
            self.pet_action = None

        sleep_label = "Wake Up" if state == PetState.SLEEPING else "Put to Sleep"
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
