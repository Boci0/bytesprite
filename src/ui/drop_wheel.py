"""Frameless translucent frosted radial drop wheel for interactive File Forge selection."""

from __future__ import annotations

import ctypes
import math
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget

from src.graphics.sprite_matrix import get_sprite_frames
from src.tools.file_forge import ForgeWheelOption
from src.ui.theme import (
    DANGER_HEX,
    GLASS_RAISED_RGBA,
    GLASS_RGBA,
    HAIRLINE_RGBA,
    TEXT_DIM_HEX,
    TEXT_HEX,
    qcolor,
    ui_font,
)


def _build_hub_sprite_qimage() -> QImage:
    frame = get_sprite_frames("idle", size=16)[0]
    h = len(frame)
    w = len(frame[0]) if h > 0 else 0
    raw = bytes(ch for row in frame for pixel in row for ch in pixel)
    img = QImage(raw, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return img.scaled(
        32,
        32,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def _annular_wedge_polygon(
    cx: float,
    cy: float,
    inner_r: float,
    outer_r: float,
    theta_start: float,
    theta_end: float,
    arc_steps: int = 18,
) -> QPolygonF:
    """Build a clockwise annular sector polygon where theta=0 is 12 o'clock."""
    pts: list[QPointF] = []
    for i in range(arc_steps + 1):
        t = theta_start + (theta_end - theta_start) * (i / arc_steps)
        pts.append(QPointF(cx + outer_r * math.sin(t), cy - outer_r * math.cos(t)))
    for i in range(arc_steps, -1, -1):
        t = theta_start + (theta_end - theta_start) * (i / arc_steps)
        pts.append(QPointF(cx + inner_r * math.sin(t), cy - inner_r * math.cos(t)))
    return QPolygonF(pts)


class RadialDropWheel(QWidget):
    """Context-aware radial wheel spawned when hovering files over ByteSprite."""

    WHEEL_WIDTH: int = 300
    WHEEL_HEIGHT: int = 316
    CENTER_X: float = 150.0
    CENTER_Y: float = 170.0
    INNER_RADIUS: float = 40.0
    OUTER_RADIUS: float = 122.0
    HOVER_OUTER_RADIUS: float = 131.0
    HIT_OUTER_RADIUS: float = 146.0
    GAP_RADIANS: float = 0.042

    def __init__(
        self,
        *,
        on_option_dropped: Callable[[list[Path], ForgeWheelOption], None],
        on_cancelled: Callable[[], None] | None = None,
        always_on_top: bool = True,
        enable_watchdog: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_option_dropped = on_option_dropped
        self._on_cancelled = on_cancelled
        self._always_on_top = always_on_top

        if enable_watchdog is None:
            enable_watchdog = os.environ.get("QT_QPA_PLATFORM", "").lower() != "offscreen"
        self._enable_watchdog = bool(enable_watchdog)

        self._paths: list[Path] = []
        self._options: list[ForgeWheelOption] = []
        self._hovered_index: int = -1

        # Precomputed per-open geometry caches (zero trig inside paintEvent)
        self._normal_polys: list[QPolygonF] = []
        self._hover_polys: list[QPolygonF] = []
        self._label_centers: list[QPointF] = []
        self._hub_sprite: QImage = _build_hub_sprite_qimage()

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(80)
        self._watchdog_timer.timeout.connect(self._on_watchdog_tick)

        self._configure_window()

    def _configure_window(self) -> None:
        self.setWindowTitle("ByteSprite Drop Wheel")
        self.setFixedSize(self.WHEEL_WIDTH, self.WHEEL_HEIGHT)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        self.setPalette(pal)

    @property
    def options(self) -> tuple[ForgeWheelOption, ...]:
        return tuple(self._options)

    @property
    def hovered_index(self) -> int:
        return self._hovered_index

    def selected_option(self) -> ForgeWheelOption | None:
        if 0 <= self._hovered_index < len(self._options):
            return self._options[self._hovered_index]
        return None

    def slice_center_point(self, index: int) -> QPoint:
        """Return widget-local integer point at the center of slice `index`."""
        if 0 <= index < len(self._label_centers):
            pt = self._label_centers[index]
            return QPoint(int(round(pt.x())), int(round(pt.y())))
        return QPoint(int(self.CENTER_X), int(self.CENTER_Y))

    def hub_center_point(self) -> QPoint:
        return QPoint(int(self.CENTER_X), int(self.CENTER_Y))

    def open_for_paths(
        self,
        paths: Sequence[Path],
        options: Sequence[ForgeWheelOption],
        *,
        window_top_left: QPoint,
        initial_global_cursor: QPoint | None = None,
    ) -> None:
        """Precompute slice polygons, position wheel on screen, and show immediately."""
        self._paths = [Path(p) for p in paths]
        self._options = list(options)
        self._hovered_index = -1
        self._rebuild_geometry_cache()

        self.move(window_top_left)
        if initial_global_cursor is not None:
            local = initial_global_cursor - window_top_left
            self._hovered_index = self.hit_test_option_index(QPointF(local))

        self.show()
        self.raise_()
        self.update()
        if self._enable_watchdog:
            self._watchdog_timer.start()

    def dismiss(self) -> None:
        self._watchdog_timer.stop()
        self._hovered_index = -1
        if self.isVisible():
            self.hide()

    def _rebuild_geometry_cache(self) -> None:
        self._normal_polys.clear()
        self._hover_polys.clear()
        self._label_centers.clear()

        n = len(self._options)
        if n == 0:
            return

        step = (2.0 * math.pi) / n
        half_gap = min(self.GAP_RADIANS * 0.5, step * 0.12)
        mid_r = (self.INNER_RADIUS + self.OUTER_RADIUS) * 0.5 + 2.0

        for i in range(n):
            center_theta = i * step
            t0 = center_theta - (step * 0.5) + half_gap
            t1 = center_theta + (step * 0.5) - half_gap
            self._normal_polys.append(
                _annular_wedge_polygon(
                    self.CENTER_X,
                    self.CENTER_Y,
                    self.INNER_RADIUS,
                    self.OUTER_RADIUS,
                    t0,
                    t1,
                )
            )
            self._hover_polys.append(
                _annular_wedge_polygon(
                    self.CENTER_X,
                    self.CENTER_Y,
                    self.INNER_RADIUS - 2.0,
                    self.HOVER_OUTER_RADIUS,
                    t0,
                    t1,
                )
            )
            lx = self.CENTER_X + mid_r * math.sin(center_theta)
            ly = self.CENTER_Y - mid_r * math.cos(center_theta)
            self._label_centers.append(QPointF(lx, ly))

    def hit_test_option_index(self, local_pos: QPointF) -> int:
        """Compute O(1) polar slice index, or -1 if inside neutral hub or outside ring."""
        n = len(self._options)
        if n == 0:
            return -1
        dx = local_pos.x() - self.CENTER_X
        dy = local_pos.y() - self.CENTER_Y
        dist = math.hypot(dx, dy)
        if dist < self.INNER_RADIUS or dist > self.HIT_OUTER_RADIUS:
            return -1

        step = (2.0 * math.pi) / n
        theta = math.atan2(dx, -dy) % (2.0 * math.pi)
        shifted = (theta + (step * 0.5)) % (2.0 * math.pi)
        return int(shifted // step) % n

    def update_hover_from_global(self, global_pos: QPoint) -> ForgeWheelOption | None:
        local = self.mapFromGlobal(global_pos)
        new_idx = self.hit_test_option_index(QPointF(local))
        if new_idx != self._hovered_index:
            self._hovered_index = new_idx
            self.update()
        return self.selected_option()

    def _on_watchdog_tick(self) -> None:
        if not self.isVisible():
            self._watchdog_timer.stop()
            return
        cursor_pos = QCursor.pos()
        if not self.geometry().adjusted(-14, -14, 14, 14).contains(cursor_pos):
            self.dismiss()
            return
        if sys.platform == "win32":
            try:
                u32 = ctypes.windll.user32
                l_down = bool(u32.GetAsyncKeyState(0x01) & 0x8000)
                r_down = bool(u32.GetAsyncKeyState(0x02) & 0x8000)
                if not l_down and not r_down:
                    self.dismiss()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # OLE Drag & Drop Events
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_paths(mime) -> list[Path]:
        if mime is None or not mime.hasUrls():
            return []
        out: list[Path] = []
        for url in mime.urls():
            if url.isLocalFile():
                local = url.toLocalFile()
                if local:
                    p = Path(local)
                    if p.exists():
                        out.append(p)
        return out

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is None:
            return
        paths = self._extract_paths(event.mimeData())
        if paths or self._paths:
            if paths:
                self._paths = paths
            new_idx = self.hit_test_option_index(event.position())
            if new_idx != self._hovered_index:
                self._hovered_index = new_idx
                self.update()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent | None) -> None:
        if event is None:
            return
        new_idx = self.hit_test_option_index(event.position())
        if new_idx != self._hovered_index:
            self._hovered_index = new_idx
            self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent | None) -> None:
        self.dismiss()
        if event is not None:
            super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is None:
            return
        dropped_paths = self._extract_paths(event.mimeData()) or list(self._paths)
        self._hovered_index = self.hit_test_option_index(event.position())
        chosen = self.selected_option()
        self.dismiss()

        if chosen is not None and dropped_paths:
            event.acceptProposedAction()
            self._on_option_dropped(dropped_paths, chosen)
            return

        event.acceptProposedAction()
        if self._on_cancelled is not None:
            self._on_cancelled()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def _header_preview_text(self) -> tuple[str, QColor]:
        if not self._paths:
            return ("Drop to convert", QColor(TEXT_DIM_HEX))
        lead = self._paths[0].name
        if len(lead) > 18:
            lead = f"{lead[:15]}..."
        src_label = f"{len(self._paths)} items" if len(self._paths) > 1 else lead

        chosen = self.selected_option()
        if chosen is None:
            return (f"{src_label}  ·  pick a slice", QColor(TEXT_DIM_HEX))
        return (f"{src_label}  ->  {chosen.preview_suffix}", QColor(TEXT_HEX))

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # Near-zero alpha hit layer keeps Win32 OLE DragOver contiguous across gaps
        painter.fillRect(
            QRect(4, 4, self.width() - 8, self.height() - 8),
            QColor(18, 16, 30, 4),
        )
        hairline = QPen(qcolor(HAIRLINE_RGBA), 1.0)

        # Header pill with the live "file -> result" preview
        header_text, header_color = self._header_preview_text()
        banner = QRectF(18.5, 8.5, self.width() - 37.0, 26.0)
        painter.setPen(hairline)
        painter.setBrush(qcolor(GLASS_RGBA))
        painter.drawRoundedRect(banner, 13.0, 13.0)
        chosen = self.selected_option()
        if chosen is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(chosen.color_hex))
            painter.drawEllipse(QPointF(banner.left() + 14.0, banner.center().y()), 3.5, 3.5)
        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(header_color)
        painter.drawText(banner, Qt.AlignmentFlag.AlignCenter, header_text)

        # Frosted ring backdrop
        center = QPointF(self.CENTER_X, self.CENTER_Y)
        halo_r = self.OUTER_RADIUS + 6.0
        painter.setPen(hairline)
        painter.setBrush(qcolor(GLASS_RGBA))
        painter.drawEllipse(center, halo_r, halo_r)

        for idx, opt in enumerate(self._options):
            is_hot = idx == self._hovered_index
            poly = self._hover_polys[idx] if is_hot else self._normal_polys[idx]
            if is_hot:
                accent = QColor(opt.color_hex)
                accent.setAlpha(230)
                painter.setBrush(QColor(68, 72, 84, 245))
                painter.setPen(QPen(accent, 2.0))
            else:
                painter.setBrush(qcolor(GLASS_RAISED_RGBA))
                painter.setPen(hairline)
            painter.drawPolygon(poly)

        # Center cancel hub
        hub_r = self.INNER_RADIUS - 5.0
        hub_is_neutral = self._hovered_index < 0
        painter.setBrush(qcolor(GLASS_RGBA))
        painter.setPen(QPen(QColor(DANGER_HEX), 1.6) if hub_is_neutral else hairline)
        painter.drawEllipse(center, hub_r, hub_r)

        # Crisp pixel-art sprite (no smoothing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        sprite_rect = QRect(int(self.CENTER_X) - 16, int(self.CENTER_Y) - 24, 32, 32)
        painter.drawImage(sprite_rect, self._hub_sprite)

        sub_font = ui_font(7.0)
        painter.setFont(sub_font)
        painter.setPen(QColor(DANGER_HEX) if hub_is_neutral else QColor(TEXT_DIM_HEX))
        painter.drawText(
            QRect(int(self.CENTER_X) - 28, int(self.CENTER_Y) + 9, 56, 13),
            Qt.AlignmentFlag.AlignCenter,
            "Skip",
        )

        label_font = ui_font(9.0, bold=True)
        for idx, opt in enumerate(self._options):
            is_hot = idx == self._hovered_index
            pt = self._label_centers[idx]
            box = QRectF(pt.x() - 34.0, pt.y() - 14.0, 68.0, 28.0)

            painter.setFont(label_font)
            painter.setPen(QColor("#FFFFFF") if is_hot else QColor(TEXT_HEX))
            painter.drawText(
                QRectF(box.left(), box.top(), box.width(), 15.0),
                Qt.AlignmentFlag.AlignCenter,
                opt.label,
            )

            painter.setFont(sub_font)
            painter.setPen(QColor(opt.color_hex) if is_hot else QColor(TEXT_DIM_HEX))
            painter.drawText(
                QRectF(box.left(), box.top() + 14.0, box.width(), 13.0),
                Qt.AlignmentFlag.AlignCenter,
                opt.subtitle.capitalize(),
            )

        painter.end()
