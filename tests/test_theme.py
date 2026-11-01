"""Pixel-frame theme: tool colours fold into the pet palette, panels have stepped corners."""

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QImage, QPainter

from src.ui.theme import ACCENT_HEX, DANGER_HEX, OUTLINE_RGBA, SLEEP_HEX, TEXT_DIM_HEX, paint_pixel_panel, pet_accent


def test_pet_accent_keeps_meaning() -> None:
    assert pet_accent("#EF7D57").name().upper() == DANGER_HEX  # coral error
    assert pet_accent("#8A9AF2").name().upper() == SLEEP_HEX  # periwinkle off / paused
    assert pet_accent("#C9D1D9").name().upper() == TEXT_DIM_HEX  # grey
    for decorative in ("#38B764", "#73EFF7", "#FFCD75", "#41A6F6", "#FF77A8", "#B48EF2"):
        assert pet_accent(decorative).name().upper() == ACCENT_HEX


def test_pixel_panel_cuts_corners() -> None:
    img = QImage(20, 20, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    paint_pixel_panel(painter, QRect(0, 0, 20, 20))
    painter.end()
    assert img.pixelColor(0, 0).alpha() == 0  # stepped corner stays clear
    assert img.pixelColor(2, 2) == QColor(*OUTLINE_RGBA)  # middle step of the outline
    assert img.pixelColor(10, 0) == QColor(*OUTLINE_RGBA)  # straight top edge
