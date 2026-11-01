"""Pixel-frame UI tokens shared by every ByteSprite surface, cut from the sprite's own palette."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QRect
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QWidget

# Opaque panels: Qt has no backdrop blur and translucent menus let busy windows bleed through text
GLASS_RGBA: tuple[int, int, int, int] = (30, 32, 38, 255)
GLASS_RAISED_RGBA: tuple[int, int, int, int] = (44, 47, 55, 255)
OUTLINE_RGBA: tuple[int, int, int, int] = (96, 103, 116, 255)  # sprite shade glass "D", made opaque
SHINE_RGBA: tuple[int, int, int, int] = (214, 219, 227, 46)  # sprite top highlight "L"
HAIRLINE_RGBA: tuple[int, int, int, int] = (255, 255, 255, 30)
HOVER_RGBA: tuple[int, int, int, int] = (176, 222, 255, 38)  # ice-blue wash
TEXT_HEX: str = "#E7E9ED"
TEXT_DIM_HEX: str = "#9BA1AB"
ACCENT_HEX: str = "#B0DEFF"  # sprite focus pulse "Q": the one accent
DANGER_HEX: str = "#E6A08C"  # sprite alert tint "R", lifted for text
SLEEP_HEX: str = "#A8AEC6"  # sprite Zzz "P"
FONT_FAMILY: str = "Segoe UI"
PIXEL: int = 2  # frame border and corner step, half a sprite pixel at the pet's 4x scale


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


def pet_accent(color: QColor | str) -> QColor:
    """Fold a tool colour into the pet palette, keeping its meaning: coral warns, periwinkle is off, greys dim."""
    c = QColor(color)
    hue = c.hsvHue()
    if c.hsvSaturation() < 40:
        return QColor(TEXT_DIM_HEX)
    if hue < 30:
        return QColor(DANGER_HEX)
    return QColor(SLEEP_HEX if 225 <= hue <= 255 else ACCENT_HEX)


def _stepped(rect: QRect, inset: int, p: int) -> tuple[QRect, ...]:
    r = rect.adjusted(inset, inset, -inset, -inset)
    return (r.adjusted(2 * p, 0, -2 * p, 0), r.adjusted(p, p, -p, -p), r.adjusted(0, 2 * p, 0, -2 * p))


def paint_pixel_panel(painter: QPainter, rect: QRect, fill: QColor | None = None,
                      border: QColor | None = None, p: int = PIXEL) -> None:
    """Sprite-style panel: hard outline, corners cut in two pixel steps, a top shine row, no smoothing."""
    fill = fill or qcolor(GLASS_RGBA)
    border = border or qcolor(OUTLINE_RGBA)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    for part in _stepped(rect, 0, p):
        painter.fillRect(part, border)
    for part in _stepped(rect, p, p):
        painter.fillRect(part, fill)
    painter.fillRect(QRect(rect.left() + 3 * p, rect.top() + p, rect.width() - 6 * p, p), qcolor(SHINE_RGBA))
    painter.restore()


class _PixelFrame(QObject):
    """Paints the pixel panel under a translucent QSS widget (menu, tooltip) before it draws its items."""

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # type: ignore[override]
        if isinstance(obj, QWidget) and event is not None and event.type() == QEvent.Type.Paint:
            painter = QPainter(obj)
            paint_pixel_panel(painter, obj.rect())
            painter.end()
        return False


def install_pixel_frame(widget: QWidget) -> None:
    widget.installEventFilter(_PixelFrame(widget))  # owned by the widget: a shared filter dies with its QApplication


# App-wide: Qt-owned popups and dialogs (tooltips, QInputDialog, combo lists, checkboxes, text selection)
APP_STYLESHEET = f"""
QToolTip {{ background: {rgba_css(GLASS_RGBA)}; color: {TEXT_HEX}; border: {PIXEL}px solid {rgba_css(OUTLINE_RGBA)};
    padding: 4px 6px; font-family: "{FONT_FAMILY}"; }}
QInputDialog {{ background: {rgba_css(GLASS_RGBA)}; color: {TEXT_HEX}; font-family: "{FONT_FAMILY}"; }}
QInputDialog QLabel {{ color: {TEXT_HEX}; }}
QInputDialog QLineEdit {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
    border: 1px solid {rgba_css(OUTLINE_RGBA)}; padding: 4px 6px; }}
QInputDialog QPushButton {{ background: transparent; color: {TEXT_HEX}; border: 1px solid {rgba_css(OUTLINE_RGBA)};
    padding: 4px 12px; min-width: 56px; }}
QInputDialog QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; }}
QComboBox QAbstractItemView {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
    border: 1px solid {rgba_css(OUTLINE_RGBA)}; outline: none;
    selection-background-color: {rgba_css(HOVER_RGBA)}; selection-color: {ACCENT_HEX}; }}
QCheckBox::indicator {{ width: 10px; height: 10px; border: {PIXEL}px solid {rgba_css(OUTLINE_RGBA)};
    background: {rgba_css(GLASS_RAISED_RGBA)}; }}
QCheckBox::indicator:checked {{ background: {ACCENT_HEX}; }}
QLineEdit, QPlainTextEdit {{ selection-background-color: {ACCENT_HEX}; selection-color: #1E2026; }}
"""
