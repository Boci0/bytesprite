"""Paper theme: tool colours fold into the duck palette, panels are rounded with a dog-eared corner."""

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QImage, QPainter

from src.ui.theme import (ACCENT_HEX, DANGER_HEX, FOLD_RGBA, GLASS_RGBA, SLEEP_HEX, TEXT_DIM_HEX, paint_panel,
                          pet_accent)


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
