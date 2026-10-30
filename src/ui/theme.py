"""Frosted-graphite UI tokens shared by the context menu and the radial drop wheel."""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont

# Opaque panels: Qt has no backdrop blur and translucent menus let busy windows bleed through text
GLASS_RGBA: tuple[int, int, int, int] = (30, 32, 38, 255)
GLASS_RAISED_RGBA: tuple[int, int, int, int] = (44, 47, 55, 255)
HAIRLINE_RGBA: tuple[int, int, int, int] = (255, 255, 255, 30)
HOVER_RGBA: tuple[int, int, int, int] = (255, 255, 255, 26)
TEXT_HEX: str = "#E7E9ED"
TEXT_DIM_HEX: str = "#9BA1AB"
DANGER_HEX: str = "#E6A0A0"
FONT_FAMILY: str = "Segoe UI"


def rgba_css(rgba: tuple[int, int, int, int]) -> str:
    r, g, b, a = rgba
    return f"rgba({r}, {g}, {b}, {a})"


def qcolor(rgba: tuple[int, int, int, int]) -> QColor:
    return QColor(*rgba)


def ui_font(point_size: float, *, bold: bool = False) -> QFont:
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(point_size)
    font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return font
