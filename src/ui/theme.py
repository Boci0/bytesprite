"""Paper-and-ink UI tokens shared by every Paperduck surface, matched to the origami duck.

Panels are folded paper: rounded corners, a hairline edge, a soft shadow and one dog-eared corner. The scheme
follows the Windows app theme at startup unless Settings > Theme picks light or dark;
`PAPERDUCK_THEME=light|dark` overrides both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, QRectF
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

RGBA = tuple[int, int, int, int]


# Settings > Theme. A file of its own because every module copies these tokens at import, before state.json loads
THEME_FILE = Path.home() / ".paperduck" / "theme.txt"
THEME_CHOICES = {"system": "Match Windows", "light": "Light", "dark": "Dark"}


def saved_theme() -> str:
    try:
        choice = THEME_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "system"
    return choice if choice in THEME_CHOICES else "system"


def save_theme(choice: str) -> None:
    """Store a Settings > Theme choice; it applies from the next start."""
    if choice not in THEME_CHOICES:
        raise ValueError(f"unknown theme {choice!r}")
    THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
    THEME_FILE.write_text(choice, encoding="utf-8")


def _system_is_light() -> bool:
    forced = os.environ.get("PAPERDUCK_THEME", "").strip().lower() or saved_theme()
    if forced in ("light", "dark"):
        return forced == "light"
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return False


IS_LIGHT: bool = _system_is_light()

# Opaque panels: Qt has no backdrop blur and translucent menus let busy windows bleed through text
if IS_LIGHT:
    GLASS_RGBA: RGBA = (250, 248, 243, 255)  # warm paper
    GLASS_RAISED_RGBA: RGBA = (240, 236, 227, 255)  # a sheet underneath
    OUTLINE_RGBA: RGBA = (222, 215, 201, 255)  # paper edge
    SHINE_RGBA: RGBA = (255, 255, 255, 200)  # top edge catching the light
    HAIRLINE_RGBA: RGBA = (60, 50, 30, 30)  # separators
    HOVER_RGBA: RGBA = (196, 140, 10, 34)  # yolk wash
    FOLD_RGBA: RGBA = (226, 216, 196, 255)  # the dog-eared corner's underside
    BUBBLE_RGBA: RGBA = (240, 233, 214, 255)  # your chat messages
    SHADOW_RGBA: RGBA = (60, 45, 20, 22)
    TEXT_HEX, TEXT_DIM_HEX = "#1D1C22", "#77736B"
    ACCENT_HEX = "#A86F00"  # yolk, deepened for contrast on paper
    DANGER_HEX = "#C9472C"  # the duck's vermilion beak
    SLEEP_HEX = "#6E6C93"  # dusk ink
    ON_ACCENT_HEX = "#FFFFFF"
else:
    GLASS_RGBA = (28, 27, 32, 255)  # ink
    GLASS_RAISED_RGBA = (38, 37, 44, 255)
    OUTLINE_RGBA = (54, 52, 61, 255)
    SHINE_RGBA = (255, 255, 255, 18)
    HAIRLINE_RGBA = (255, 255, 255, 22)
    HOVER_RGBA = (242, 190, 58, 30)
    FOLD_RGBA = (68, 65, 76, 255)
    BUBBLE_RGBA = (52, 49, 44, 255)
    SHADOW_RGBA = (0, 0, 0, 40)
    TEXT_HEX, TEXT_DIM_HEX = "#EFECE5", "#9B978E"
    ACCENT_HEX = "#F2BE3A"  # the duck's yolk yellow: the one accent
    DANGER_HEX = "#EE7A5C"  # vermilion, lifted for text on ink
    SLEEP_HEX = "#A3A1C4"
    ON_ACCENT_HEX = "#1D1C22"

FONT_FAMILY: str = "Segoe UI Variable Text" if os.path.exists(r"C:\Windows\Fonts\SegUIVar.ttf") else "Segoe UI"
RADIUS: int = 10  # panel corners
FOLD: int = 12  # dog-ear size on panels
SHADOW: int = 4  # soft shadow margin reserved inside menu widgets
PIXEL: int = 2  # small stroke / gap unit kept for HUD bars and markers


def rgba_css(rgba: RGBA) -> str:
    r, g, b, a = rgba
    return f"rgba({r}, {g}, {b}, {a})"


def qcolor(rgba: RGBA) -> QColor:
    return QColor(*rgba)


def ui_font(point_size: float, *, bold: bool = False) -> QFont:
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(point_size)
    font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return font


def pet_accent(color: QColor | str) -> QColor:
    """Fold a tool colour into the duck palette, keeping its meaning: vermilion warns, dusk is off, greys dim."""
    c = QColor(color)
    hue = c.hsvHue()
    if c.hsvSaturation() < 40:
        return QColor(TEXT_DIM_HEX)
    if hue < 30:
        return QColor(DANGER_HEX)
    return QColor(SLEEP_HEX if 225 <= hue <= 255 else ACCENT_HEX)


def panel_path(rect: QRectF, radius: float = RADIUS, fold: float = 0.0) -> QPainterPath:
    """Rounded rectangle whose top-right corner is cut on the diagonal when `fold` > 0 (the dog-ear)."""
    path = QPainterPath()
    if fold <= 0:
        path.addRoundedRect(rect, radius, radius)
        return path
    l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
    path.moveTo(l + radius, t)
    path.lineTo(r - fold, t)
    path.lineTo(r, t + fold)
    path.lineTo(r, b - radius)
    path.arcTo(QRectF(r - 2 * radius, b - 2 * radius, 2 * radius, 2 * radius), 0, -90)
    path.lineTo(l + radius, b)
    path.arcTo(QRectF(l, b - 2 * radius, 2 * radius, 2 * radius), 270, -90)
    path.lineTo(l, t + radius)
    path.arcTo(QRectF(l, t, 2 * radius, 2 * radius), 180, -90)
    path.closeSubpath()
    return path


def paint_panel(painter: QPainter, rect: QRect, fill: QColor | None = None, border: QColor | None = None, *,
                radius: float = RADIUS, fold: bool = True, shadow: int = 0) -> None:
    """Folded-paper panel: soft shadow (inside `shadow` px of margin), rounded body, hairline edge, a lit top
    edge and a dog-eared top-right corner."""
    fill = fill or qcolor(GLASS_RGBA)
    border = border or qcolor(OUTLINE_RGBA)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    body = QRectF(rect).adjusted(shadow + 0.5, shadow * 0.5 + 0.5, -shadow - 0.5, -shadow * 1.5 - 0.5)
    if shadow:  # stacked faint layers, offset down: alpha builds toward the panel edge
        c = qcolor(SHADOW_RGBA)
        c.setAlpha(max(1, SHADOW_RGBA[3] // shadow))
        painter.setPen(QPen(c, 0.0))
        painter.setBrush(c)
        painter.setPen(QColor(0, 0, 0, 0))
        for i in range(shadow, 0, -1):
            painter.drawRoundedRect(body.adjusted(-i, -i + 2, i, i + 1), radius + i, radius + i)
    size = min(FOLD, body.width() / 4, body.height() / 3) if fold else 0.0
    path = panel_path(body, min(radius, body.height() / 2, body.width() / 2), size)
    grad = QLinearGradient(body.topLeft(), body.bottomLeft())
    grad.setColorAt(0, fill.lighter(104) if not IS_LIGHT else fill)
    grad.setColorAt(1, fill)
    painter.setPen(QPen(border, 1.0))
    painter.setBrush(grad)
    painter.drawPath(path)
    painter.setPen(QPen(qcolor(SHINE_RGBA), 1.0))
    painter.drawLine(QPointF(body.left() + radius, body.top() + 1), QPointF(body.right() - max(size, radius) - 1, body.top() + 1))
    if size:
        r, t = body.right(), body.top()
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(qcolor(FOLD_RGBA))
        painter.drawPolygon(QPolygonF([QPointF(r - size, t), QPointF(r, t + size), QPointF(r - size + 1.5, t + size - 1.5)]))
    painter.restore()


class _PanelFrame(QObject):
    """Paints the paper panel under a translucent QSS widget (menu, tooltip) before it draws its items."""

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # type: ignore[override]
        if isinstance(obj, QWidget) and event is not None and event.type() == QEvent.Type.Paint:
            painter = QPainter(obj)
            paint_panel(painter, obj.rect(), shadow=SHADOW)
            painter.end()
        return False


def install_panel_frame(widget: QWidget) -> None:
    widget.installEventFilter(_PanelFrame(widget))  # owned by the widget: a shared filter dies with its QApplication


# App-wide: Qt-owned popups and dialogs (tooltips, QInputDialog, combo lists, checkboxes, text selection)
APP_STYLESHEET = f"""
QToolTip {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; border: 1px solid {rgba_css(OUTLINE_RGBA)};
    border-radius: 6px; padding: 4px 8px; font-family: "{FONT_FAMILY}"; }}
QInputDialog {{ background: {rgba_css(GLASS_RGBA)}; color: {TEXT_HEX}; font-family: "{FONT_FAMILY}"; }}
QInputDialog QLabel {{ color: {TEXT_HEX}; }}
QInputDialog QLineEdit {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
    border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; padding: 5px 8px; }}
QInputDialog QLineEdit:focus {{ border-color: {ACCENT_HEX}; }}
QInputDialog QPushButton {{ background: transparent; color: {TEXT_HEX}; border: 1px solid {rgba_css(OUTLINE_RGBA)};
    border-radius: 6px; padding: 5px 14px; min-width: 56px; }}
QInputDialog QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; }}
QComboBox QAbstractItemView {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
    border: 1px solid {rgba_css(OUTLINE_RGBA)}; outline: none; padding: 4px;
    selection-background-color: {rgba_css(HOVER_RGBA)}; selection-color: {ACCENT_HEX}; }}
QCheckBox::indicator {{ width: 12px; height: 12px; border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 3px;
    background: {rgba_css(GLASS_RAISED_RGBA)}; }}
QCheckBox::indicator:checked {{ background: {ACCENT_HEX}; border-color: {ACCENT_HEX}; }}
QLineEdit, QPlainTextEdit {{ selection-background-color: {ACCENT_HEX}; selection-color: {ON_ACCENT_HEX}; }}
"""
