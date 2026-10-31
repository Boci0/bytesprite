"""Pixel-art "summon" effect when AI Assist opens: sparkles burst from the pet.
Purely visual and click-through; deletes itself when done."""

from __future__ import annotations

import math
import random
import time

from PyQt6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QScreen
from PyQt6.QtWidgets import QWidget

DURATION_S = 1.2
BLOCK = 4  # sparkle pixel size, matching the sprite's chunky pixels
SPARKS = 32
_PALETTE = ("#73EFF7", "#41A6F6", "#B48EF2", "#FFCD75", "#FFFFFF")
MAX_DIST = 230
REACH = MAX_DIST + 3 * BLOCK + 24  # overlay half-size: farthest spark, its arms and the upward drift


class ScreenGlow(QWidget):
    def __init__(self, screen: QScreen, origin: QPoint | None = None) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        center = origin if origin is not None else screen.geometry().center()
        # Only the area the sparks reach, not the whole screen
        self.setGeometry(QRect(center.x() - REACH, center.y() - REACH, 2 * REACH, 2 * REACH))
        self._origin = QPointF(REACH, REACH)
        rng = random.Random()
        # (angle, distance px, size in blocks, color, delay s, twinkle phase)
        self._sparks = [(rng.uniform(0, 2 * math.pi), rng.uniform(60, MAX_DIST), rng.choice((1, 1, 2)),
                         QColor(rng.choice(_PALETTE)), rng.uniform(0, 0.15), rng.random())
                        for _ in range(SPARKS)]
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def play(self) -> None:
        self.show()
        self._timer.start()

    def progress(self) -> float:
        return min(1.0, (time.monotonic() - self._t0) / DURATION_S)

    def _tick(self) -> None:
        if self.progress() >= 1.0:
            self._timer.stop()
            self.close()
            return
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = self.progress()
        elapsed = p * DURATION_S
        painter = QPainter(self)  # no antialiasing: crisp pixels
        for angle, dist, size, color, delay, phase in self._sparks:
            t = (elapsed - delay) / (DURATION_S * 0.8)
            if not 0.0 < t < 1.0:
                continue
            ease = 1 - (1 - t) ** 3
            x = self._origin.x() + math.cos(angle) * dist * ease
            y = self._origin.y() + math.sin(angle) * dist * ease - 18 * t  # drift up a little
            fade = 1.0 if t < 0.6 else (1.0 - t) / 0.4
            twinkle = int((elapsed * 12 + phase * 4)) % 3 != 0
            self._paint_spark(painter, x, y, size, color, fade, twinkle)
        painter.end()

    @staticmethod
    def _paint_spark(painter: QPainter, x: float, y: float, size: int, color: QColor, fade: float,
                     twinkle: bool) -> None:
        """Pixel star: a center block, plus arms on twinkle frames (bigger sparks get longer arms)."""
        c = QColor(color)
        c.setAlpha(int(255 * fade))
        b = BLOCK
        cx, cy = int(x) // b * b, int(y) // b * b  # snap to the pixel grid
        painter.fillRect(cx, cy, b, b, c)
        if twinkle:
            c.setAlpha(int(170 * fade))
            for step in range(1, size + 1):
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    painter.fillRect(cx + dx * step * b, cy + dy * step * b, b, b, c)


def play_screen_glow(screen: QScreen | None, origin: QPoint | None = None) -> ScreenGlow | None:
    if screen is None:
        return None
    glow = ScreenGlow(screen, origin)
    glow.play()
    return glow
