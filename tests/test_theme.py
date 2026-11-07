"""Paper theme: tool colours fold into the duck palette, panels are rounded with a dog-eared corner."""

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from src.ui.theme import (ACCENT_HEX, DANGER_HEX, FOLD_RGBA, GLASS_RGBA, SLEEP_HEX, TEXT_DIM_HEX, paint_panel,
                          pet_accent)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_pet_accent_keeps_meaning() -> None:
    assert pet_accent("#EF7D57").name().upper() == DANGER_HEX  # vermilion error
    assert pet_accent("#8A9AF2").name().upper() == SLEEP_HEX  # dusk: off / paused
    assert pet_accent("#C9D1D9").name().upper() == TEXT_DIM_HEX  # grey
    for decorative in ("#38B764", "#73EFF7", "#FFCD75", "#41A6F6", "#FF77A8", "#B48EF2"):
        assert pet_accent(decorative).name().upper() == ACCENT_HEX


def test_panel_rounds_corners_and_folds_the_top_right() -> None:
    img = QImage(60, 40, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    paint_panel(painter, QRect(0, 0, 60, 40))
    painter.end()
    assert img.pixelColor(0, 0).alpha() == 0  # rounded corner stays clear
    assert img.pixelColor(59, 0).alpha() == 0  # dog-ear cut
    assert img.pixelColor(30, 30) == QColor(*GLASS_RGBA)  # body
    assert img.pixelColor(52, 8) == QColor(*FOLD_RGBA)  # the folded flap


def test_unfolded_panel_has_no_dog_ear() -> None:
    img = QImage(60, 40, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    paint_panel(painter, QRect(0, 0, 60, 40), radius=4, fold=False)
    painter.end()
    corner = img.pixelColor(56, 4)  # near the top the dark fill is a touch lighter (lit edge gradient)
    assert corner.alpha() == 255 and corner != QColor(*FOLD_RGBA)


def test_theme_choice_is_saved_and_restarts_only_on_a_change(tmp_path, monkeypatch, qapp) -> None:
    from src.engine.models import PetStats
    from src.engine.pet_state import PetStateMachine
    from src.graphics.animation_controller import AnimationController
    from src.ui import theme
    from src.ui.pet_window import PetWindow

    monkeypatch.setattr(theme, "THEME_FILE", tmp_path / "theme.txt")
    assert theme.saved_theme() == "system"  # no file: follow Windows
    win = PetWindow(state_machine=PetStateMachine(PetStats()), animation_controller=AnimationController(size=32),
                    auto_wander=False)
    restarts: list[int] = []
    monkeypatch.setattr(win, "restart", lambda: restarts.append(1))
    try:
        win.trigger_tool_action("theme:system")  # already the choice: nothing to do
        win.trigger_tool_action("theme:neon")  # unknown: ignored
        assert restarts == [] and not theme.THEME_FILE.exists()
        win.trigger_tool_action("theme:light")
        assert restarts == [1] and theme.saved_theme() == "light"
        menu = win.build_context_menu()
        assert [a.text() for a in menu.theme_menu.actions() if a.isChecked()] == ["Light"]
        menu.deleteLater()
    finally:
        win.deleteLater()
    theme.THEME_FILE.write_text("garbage", encoding="utf-8")
    assert theme.saved_theme() == "system"


def test_text_box_right_click_menus_get_the_pet_menu_look(monkeypatch) -> None:
    """Qt's own Undo/Copy/Paste menu took the dark box styling and black theme icons, so it was unreadable."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QApplication, QLineEdit, QMenu, QPlainTextEdit

    from src.ui.context_menu import MENU_STYLESHEET, style_text_box_menus

    app = QApplication.instance() or QApplication([])
    style_text_box_menus(app)
    shown: list[QMenu] = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *_a: shown.append(menu))
    line, draft = QLineEdit("hi"), QPlainTextEdit("hello")
    for target in (line, draft.viewport()):
        app.sendEvent(target, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(5, 5)))
    assert len(shown) == 2
    for menu in shown:
        assert menu.styleSheet() == MENU_STYLESHEET
        assert any(a.text().startswith("&Copy") or "Copy" in a.text() for a in menu.actions())
        assert all(a.icon().isNull() for a in menu.actions())
