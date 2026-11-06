"""ByteSprite's companion: an origami rubber duck, drawn as smooth vector paper and animated as a rig.

Every frame is a `Pose` of jointed parts (body lean and squash, head on its neck, beak jaws, wing, tail, two
feet, eye shape, a few sparing effects). Clips are keyframes tweened into poses; `render_pose` paints one at any
pixel size, so the pet stays sharp at every scale and DPI. Coordinates are a 128-unit box facing right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from functools import lru_cache

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient

REQUIRED_ANIMATIONS: tuple[str, ...] = ("idle", "blink", "walk", "sleep", "focus", "spin")
ALL_ANIMATIONS: tuple[str, ...] = (
    "idle", "blink", "walk", "sleep", "focus", "spin", "hungry", "eating", "music",
    "cheer", "yawn", "dangle", "land", "flinch", "shine",
)
ONESHOT_ANIMATIONS: frozenset[str] = frozenset({"blink", "eating", "spin", "cheer", "yawn", "land", "flinch", "shine"})

UNITS = 128
GROUND = 106.0  # feet rest here; squash and lean pivot on it

# Folded yellow paper, lit from the top left; vermilion beak and feet (the one warm accent)
PAPER: dict[str, str] = {
    "top": "#FCE07A", "back": "#F2C445", "chest": "#F7CE52", "under": "#E2A733", "under_back": "#D9992A",
    "tail": "#EDB73A", "tail_fold": "#D49524", "wing": "#EFBE3F", "wing_fold": "#FAD866",
    "neck": "#F8D35C", "head_light": "#FDE891", "head_shade": "#E5AD3B",
    "beak": "#E8664A", "beak_low": "#C64A2E", "foot": "#E0623F", "foot_far": "#B44A31",
    "edge": "#6B4A12", "eye": "#1E1912", "fx": "#9BB7C9", "z": "#8C95B8", "bang": "#D9472B",
}


@dataclass(frozen=True)
class Pose:
    """One frame of the rig. Angles in degrees (positive turns clockwise on screen), lengths in units."""

    x: float = 0.0  # whole-body offset
    y: float = 0.0
    lean: float = 0.0  # body tilt around the feet
    sq: float = 1.0  # vertical squash (<1) or stretch (>1) at the feet, volume kept
    hx: float = 0.0  # head offset on the neck
    hy: float = 0.0
    hr: float = 0.0  # head tilt (positive nods forward / down)
    beak: float = 0.0  # 0 shut .. 1 wide open
    wing: float = 0.0  # 0 folded .. 75 raised
    tail: float = 0.0  # tail wag (positive flicks up)
    fdx: float = 0.0  # far foot step and lift
    flift: float = 0.0
    ndx: float = 0.0  # near foot step and lift
    nlift: float = 0.0
    eye: str = "open"  # open, half, closed, happy, wide, worried
    fx: tuple[tuple[str, float, float, float], ...] = ()  # (kind, x, y, alpha 0..1), never squashed


_NUMERIC = tuple(f.name for f in fields(Pose) if f.name not in ("eye", "fx"))


def _ease(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _tween(a: Pose, b: Pose, t: float) -> Pose:
    e = _ease(t)
    vals = {n: getattr(a, n) + (getattr(b, n) - getattr(a, n)) * e for n in _NUMERIC}
    return replace(a, **vals)


def _clip(keys: list[tuple[Pose, int, int]], loop: bool) -> tuple[list[Pose], list[int]]:
    """Keyframes (pose, frames toward the next key, ms per frame) -> poses and durations."""
    poses: list[Pose] = []
    durations: list[int] = []
    for i, (pose, frames, ms) in enumerate(keys):
        nxt = keys[(i + 1) % len(keys)][0] if (loop or i + 1 < len(keys)) else pose
        for j in range(frames):
            poses.append(_tween(pose, nxt, j / frames))
            durations.append(ms)
    return poses, durations


# ---------------------------------------------------------------------------
# Clips: acting, not just transforms
# ---------------------------------------------------------------------------

N = Pose()


def _build_clips() -> dict[str, tuple[list[Pose], list[int]]]:
    clips: dict[str, tuple[list[Pose], list[int]]] = {}

    # Idle: slow breath (body swells, head floats up a touch), tail sways, wing settles
    clips["idle"] = _clip([
        (N, 4, 190),
        (replace(N, sq=1.02, hy=-1.4, tail=4, wing=2), 4, 190),
        (replace(N, sq=0.99, hy=0.6, tail=-2, hr=1.5), 4, 190),
    ], loop=True)
    # Blink: lid comes down and back up, head dips with it
    clips["blink"] = (
        [replace(N, eye="half", hr=1), replace(N, eye="closed", hr=2, hy=0.5), replace(N, eye="half", hr=1)],
        [60, 90, 70],
    )
    # Walk: a waddle. Body rocks over the planted foot, the other swings forward, head counter-bobs, tail wags
    step = 6.0
    clips["walk"] = _clip([
        (replace(N, lean=-5, y=-1.5, ndx=step, nlift=5, fdx=-step * 0.5, hy=1, tail=-6, wing=3), 2, 85),
        (replace(N, lean=0, y=0.5, ndx=step * 0.5, fdx=0, hy=-1, tail=0, sq=0.98), 2, 85),
        (replace(N, lean=5, y=-1.5, ndx=-step * 0.5, fdx=step, flift=5, hy=1, tail=6, wing=3), 2, 85),
        (replace(N, lean=0, y=0.5, ndx=0, fdx=step * 0.5, hy=-1, tail=0, sq=0.98), 2, 85),
    ], loop=True)
    # Sleep: head sunk back into its shoulders, settled low, slow deep breaths, one Z drifting up
    tuck = replace(N, eye="closed", hr=-14, hx=-9, hy=13, sq=0.94, wing=0, tail=-4)
    clips["sleep"] = _clip([
        (replace(tuck, fx=(("z", 108, 52, 0.0),)), 3, 380),
        (replace(tuck, sq=0.97, hy=15, fx=(("z", 110, 44, 0.8),)), 3, 380),
        (replace(tuck, sq=0.94, hy=16.5, fx=(("z", 113, 34, 0.5),)), 3, 380),
    ], loop=True)
    # Focus (Pomodoro): head lowered over its work, eyes narrowed, small reading nods and a slow tail tick
    read = replace(N, eye="half", hr=14, hx=2, hy=5, tail=-2)
    clips["focus"] = _clip([
        (read, 4, 160),
        (replace(read, hr=18, hy=6.5, tail=2), 3, 160),
        (replace(read, hr=12, hy=4.5, hx=3), 4, 160),
        (replace(read, eye="closed", hr=14), 1, 120),
    ], loop=True)
    # Spin (click reaction): a quack. Rears back, pops up with wings flared and beak open, lands, shakes off
    clips["spin"] = _clip([
        (replace(N, sq=0.9, hr=-10, hx=-3, tail=6), 2, 60),
        (replace(N, y=-12, sq=1.08, hr=-16, beak=1.0, wing=48, tail=14, flift=3, nlift=4, eye="happy"), 2, 55),
        (replace(N, y=-14, sq=1.04, hr=-12, beak=0.8, wing=24, tail=10, flift=3, nlift=4, eye="happy"), 2, 55),
        (replace(N, y=0, sq=0.88, hr=-4, beak=0.2, wing=30, tail=2), 2, 60),
        (replace(N, sq=1.02, lean=3, wing=6, tail=-6, eye="happy"), 2, 70),
        (replace(N, lean=-2, tail=5, eye="happy"), 1, 140),
        (N, 1, 120),
    ], loop=False)
    # Hungry (CPU or RAM alert): uneasy, looking around, tapping a foot, a bead of sweat
    worry = replace(N, eye="worried", sq=0.98, fx=(("sweat", 108, 30, 0.9),))
    clips["hungry"] = _clip([
        (replace(worry, hr=-6, hx=-1, nlift=3), 3, 140),
        (replace(worry, hr=4, hx=1, nlift=0, fx=(("sweat", 108, 33, 0.9),)), 3, 140),
        (replace(worry, hr=-3, nlift=3, lean=-1), 3, 140),
    ], loop=True)
    # Eating: two pecks down at the ground, beak snapping, then a gulp
    peck = replace(N, hr=52, hx=4, hy=24, lean=6, tail=8, beak=0.6)
    clips["eating"] = _clip([
        (replace(N, hr=-6, hy=-2), 2, 70),
        (peck, 2, 60),
        (replace(peck, beak=0.0, hy=22), 2, 70),
        (replace(N, hr=-4, hy=-1, beak=0.0), 2, 70),
        (peck, 2, 60),
        (replace(peck, beak=0.0, hy=22), 2, 70),
        (replace(N, hr=-14, hy=-3, sq=1.03, eye="happy"), 2, 90),
        (replace(N, eye="happy"), 1, 160),
    ], loop=False)
    # Music: nodding on the beat, swaying hips, tapping feet, a note now and then
    clips["music"] = _clip([
        (replace(N, lean=-4, hr=8, ndx=2, nlift=3, tail=-6, eye="happy", fx=(("note", 110, 34, 0.9),)), 2, 120),
        (replace(N, lean=0, hr=-2, sq=0.97, eye="happy"), 2, 120),
        (replace(N, lean=4, hr=8, fdx=2, flift=3, tail=6, eye="happy", fx=(("note", 16, 30, 0.9),)), 2, 120),
        (replace(N, lean=0, hr=-2, sq=0.97, eye="happy"), 2, 120),
    ], loop=True)
    # Cheer (big win): crouch, launch with full wing beats, beak open, land on a squash, proud settle
    clips["cheer"] = _clip([
        (replace(N, sq=0.84, hr=10, hy=4, wing=8, eye="closed"), 2, 80),
        (replace(N, y=-16, sq=1.1, hr=-14, beak=0.9, wing=72, tail=14, flift=5, nlift=5, eye="happy",
                 fx=(("spark", 24, 26, 1.0), ("spark", 104, 14, 0.9))), 2, 60),
        (replace(N, y=-18, sq=1.04, hr=-12, beak=0.9, wing=18, tail=10, flift=5, nlift=5, eye="happy",
                 fx=(("spark", 18, 18, 0.7), ("spark", 110, 22, 1.0))), 2, 60),
        (replace(N, y=-14, sq=1.04, hr=-10, beak=0.7, wing=70, tail=12, flift=4, nlift=4, eye="happy",
                 fx=(("spark", 14, 30, 0.4),)), 2, 70),
        (replace(N, y=0, sq=0.84, hr=6, beak=0.1, wing=36, eye="happy"), 2, 70),
        (replace(N, sq=1.03, hr=-8, wing=10, tail=6, eye="happy"), 2, 100),
        (replace(N, hr=-6, tail=4, eye="happy"), 1, 300),
    ], loop=False)
    # Yawn: head tips back, beak opens slow and wide, wings stretch out, then a sleepy slump
    clips["yawn"] = _clip([
        (replace(N, eye="half"), 2, 90),
        (replace(N, hr=-22, hy=-3, beak=1.0, wing=34, sq=1.04, eye="closed", tail=8), 2, 260),
        (replace(N, hr=-24, hy=-3, beak=1.0, wing=40, sq=1.05, eye="closed", tail=8), 2, 200),
        (replace(N, hr=8, hy=3, beak=0.0, wing=0, sq=0.96, eye="half"), 2, 160),
        (replace(N, eye="half"), 1, 200),
    ], loop=False)
    # Dangle (being dragged): lifted, feet kicking, wings fluttering for balance, wide-eyed
    lift = replace(N, y=-6, sq=1.05, eye="wide", beak=0.25, hr=-4)
    clips["dangle"] = _clip([
        (replace(lift, lean=-5, wing=20, flift=-4, nlift=-1, fdx=-2, ndx=3), 2, 90),
        (replace(lift, lean=0, wing=34, flift=-2, nlift=-4), 2, 90),
        (replace(lift, lean=5, wing=18, flift=-1, nlift=-4, fdx=3, ndx=-2), 2, 90),
        (replace(lift, lean=0, wing=32, flift=-4, nlift=-2), 2, 90),
    ], loop=True)
    # Land (drag released): touchdown squash with wings out for balance, overshoot, a quick tail shake
    clips["land"] = _clip([
        (replace(N, sq=0.82, hr=12, hy=5, wing=30, eye="closed"), 2, 50),
        (replace(N, sq=1.06, hr=-6, wing=12, eye="open"), 2, 70),
        (replace(N, tail=10, lean=1), 1, 60),
        (replace(N, tail=-8, lean=-1), 1, 60),
        (replace(N, tail=5), 1, 70),
        (N, 1, 120),
    ], loop=False)
    # Flinch (something failed): startles back with feathers up, a shiver, then a worried hunch
    clips["flinch"] = _clip([
        (replace(N, lean=-8, hx=-6, hr=-16, wing=22, tail=12, sq=1.04, eye="wide", fx=(("bang", 112, 10, 1.0),)), 1, 70),
        (replace(N, lean=-6, hx=-5, hr=-12, wing=16, tail=10, x=-1.5, eye="wide", fx=(("bang", 112, 10, 1.0),)), 1, 50),
        (replace(N, lean=-6, hx=-5, hr=-12, wing=16, tail=10, x=1.5, eye="wide", fx=(("bang", 112, 10, 0.8),)), 1, 50),
        (replace(N, lean=-6, hx=-5, hr=-12, wing=14, tail=10, x=-1.0, eye="wide", fx=(("bang", 112, 10, 0.6),)), 1, 60),
        (replace(N, sq=0.94, hr=16, hy=6, eye="worried", fx=(("sweat", 108, 34, 0.9),)), 2, 160),
        (replace(N, sq=0.96, hr=12, hy=5, eye="worried", fx=(("sweat", 108, 38, 0.6),)), 1, 260),
    ], loop=False)
    # Shine (ambient): preens. Turns its head back to the wing, nibbles twice, looks up pleased
    preen = replace(N, hr=-62, hx=-22, hy=8, wing=14, beak=0.3, eye="half", lean=-2)
    clips["shine"] = _clip([
        (N, 2, 70),
        (preen, 2, 80),
        (replace(preen, wing=22, beak=0.0, hy=9), 2, 80),
        (preen, 2, 80),
        (replace(preen, wing=22, beak=0.0, hy=9), 2, 80),
        (replace(N, hr=-6, eye="happy", sq=1.02, tail=6), 2, 100),
        (replace(N, eye="happy"), 1, 220),
    ], loop=False)
    return clips


@lru_cache(maxsize=1)
def _clips() -> dict[str, tuple[list[Pose], list[int]]]:
    return _build_clips()


def get_poses(state: str) -> list[Pose]:
    if state not in _clips():
        raise KeyError(f"Unknown sprite animation state: {state!r}")
    return _clips()[state][0]


def get_durations(state: str) -> list[int]:
    if state not in _clips():
        raise KeyError(f"Unknown sprite animation state: {state!r}")
    return _clips()[state][1]


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------

def _poly(painter: QPainter, pts: tuple[tuple[float, float], ...], color: str, edge: QPen) -> None:
    painter.setBrush(QColor(color))
    painter.setPen(edge)
    painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


def _fold(painter: QPainter, a: tuple[float, float], b: tuple[float, float], alpha: int = 70) -> None:
    painter.setPen(QPen(QColor(255, 248, 220, alpha), 0.8))
    painter.drawLine(QPointF(*a), QPointF(*b))


def _foot(painter: QPainter, bx: float, dx: float, lift: float, color: str, edge: QPen) -> None:
    painter.save()
    painter.translate(bx + dx, GROUND - lift)  # negative lift: dangling below the body
    painter.rotate(18 if lift < 0 else -10 if lift > 0 else 0)
    _poly(painter, ((-6, -10), (5, -10), (10, 0), (5, -1.5), (1, 0), (-3, -1.5), (-8, 0)), color, edge)
    painter.restore()


def _eye(painter: QPainter, style: str) -> None:
    ex, ey = 89.0, 32.0
    ink = QColor(PAPER["eye"])
    painter.setPen(Qt.PenStyle.NoPen)
    if style in ("open", "wide", "worried"):
        r = 3.1 if style == "wide" else 2.5
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(ex, ey), r, r * 1.1)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawEllipse(QPointF(ex + r * 0.4, ey - r * 0.45), r * 0.36, r * 0.36)
        if style == "worried":  # brow sloping down toward the back of the head
            pen = QPen(ink, 1.3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(ex - 4.5, ey - 5.2), QPointF(ex + 3.5, ey - 7.0))
        return
    pen = QPen(ink, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if style == "half":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        half = QPainterPath()
        half.moveTo(ex - 2.6, ey)
        half.arcTo(QRectF(ex - 2.6, ey - 2.8, 5.2, 5.6), 180, 180)
        half.closeSubpath()
        painter.drawPath(half)
        painter.setPen(pen)
        painter.drawLine(QPointF(ex - 3.4, ey - 0.2), QPointF(ex + 3.4, ey - 0.6))
        return
    if style == "happy":  # an upturned arc
        path.moveTo(ex - 3.2, ey + 1.2)
        path.quadTo(ex, ey - 3.4, ex + 3.2, ey + 1.2)
    else:  # closed: a resting downward arc
        path.moveTo(ex - 3.2, ey - 0.6)
        path.quadTo(ex, ey + 2.8, ex + 3.2, ey - 0.6)
    painter.drawPath(path)


def _head(painter: QPainter, pose: Pose, edge: QPen) -> None:
    painter.save()
    pivot = (86.0, 62.0)
    painter.translate(pivot[0] + pose.hx, pivot[1] + pose.hy)
    painter.rotate(pose.hr)
    painter.translate(-pivot[0], -pivot[1])
    _poly(painter, ((70, 64), (74, 26), (96, 22), (101, 64)), PAPER["neck"], edge)
    _poly(painter, ((74, 26), (96, 22), (84, 44)), PAPER["head_light"], edge)
    _poly(painter, ((96, 22), (101, 64), (88, 46)), PAPER["head_shade"], edge)
    _fold(painter, (84, 44), (88, 46))
    _fold(painter, (84, 44), (72, 64), 50)
    # Beak: two jaws hinged at the base so it can quack, peck and yawn
    hinge = QPointF(97.0, 37.0)
    for low in (True, False):
        painter.save()
        painter.translate(hinge)
        painter.rotate((22.0 if low else -10.0) * pose.beak)
        painter.translate(-hinge)
        if low:
            _poly(painter, ((97, 37), (118, 37.5), (97, 43)), PAPER["beak_low"], edge)
        else:
            _poly(painter, ((95, 29), (121, 36), (97, 37.5)), PAPER["beak"], edge)
        painter.restore()
    _eye(painter, pose.eye)
    painter.restore()


def _fx(painter: QPainter, kind: str, x: float, y: float, alpha: float) -> None:
    if alpha <= 0:
        return
    painter.save()
    painter.setOpacity(max(0.0, min(1.0, alpha)))
    painter.translate(x, y)
    if kind == "z":
        pen = QPen(QColor(PAPER["z"]), 1.8)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([QPointF(-4, -4), QPointF(4, -4), QPointF(-4, 4), QPointF(4, 4)]))
    elif kind == "sweat":
        drop = QPainterPath()
        drop.moveTo(0, -5)
        drop.cubicTo(4, 0, 4, 4, 0, 4.5)
        drop.cubicTo(-4, 4, -4, 0, 0, -5)
        painter.setPen(QPen(QColor(70, 104, 128, 180), 0.8))
        painter.setBrush(QColor(PAPER["fx"]))
        painter.drawPath(drop)
    elif kind == "note":
        ink = QColor(PAPER["edge"])
        painter.setPen(QPen(ink, 1.6))
        painter.drawLine(QPointF(2.6, 3), QPointF(2.6, -8))
        painter.drawLine(QPointF(2.6, -8), QPointF(6.5, -5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(0, 3.5), 3.0, 2.3)
    elif kind == "spark":
        star = QPainterPath()
        for i in range(8):
            r = 6.0 if i % 2 == 0 else 1.6
            a = i * math.pi / 4
            pt = QPointF(r * math.sin(a), -r * math.cos(a))
            star.moveTo(pt) if i == 0 else star.lineTo(pt)
        star.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 236, 170))
        painter.drawPath(star)
    elif kind == "bang":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PAPER["bang"]))
        painter.drawRoundedRect(QRectF(-1.8, 0, 3.6, 11), 1.6, 1.6)
        painter.drawEllipse(QPointF(0, 15), 2.0, 2.0)
    painter.restore()


def paint_pose(painter: QPainter, pose: Pose, *, mirrored: bool = False) -> None:
    """Paint one pose into the current 128-unit painter space."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if mirrored:
        painter.translate(UNITS, 0)
        painter.scale(-1, 1)
    # Ground shadow: shrinks and fades as the duck leaves the ground
    height = max(0.0, -pose.y)
    shade = QRadialGradient(QPointF(64 + pose.x, 110), 40)
    shade.setColorAt(0, QColor(0, 0, 0, int(70 * (1 - height / 40))))
    shade.setColorAt(1, QColor(0, 0, 0, 0))
    painter.save()
    painter.translate(64 + pose.x, 110)
    k = 1.0 - height / 50
    painter.scale(k, 0.15 * k)
    painter.translate(-(64 + pose.x), -110)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(shade)
    painter.drawEllipse(QPointF(64 + pose.x, 110), 40, 40)
    painter.restore()

    edge = QPen(QColor(107, 74, 18, 150), 0.9)  # PAPER["edge"] at 60%: fold edges, not cartoon outlines
    edge.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.save()
    painter.translate(64 + pose.x, GROUND + pose.y)
    painter.rotate(pose.lean)
    painter.scale(1.0 / math.sqrt(pose.sq), pose.sq)
    painter.translate(-64, -GROUND)
    _foot(painter, 54, pose.fdx, pose.flift, PAPER["foot_far"], edge)
    _foot(painter, 72, pose.ndx, pose.nlift, PAPER["foot"], edge)
    # Tail: a folded point that wags around its root
    painter.save()
    painter.translate(42, 74)
    painter.rotate(-pose.tail)
    painter.translate(-42, -74)
    _poly(painter, ((16, 42), (47, 66), (31, 90)), PAPER["tail"], edge)
    _poly(painter, ((16, 42), (40, 72), (31, 90)), PAPER["tail_fold"], edge)
    painter.restore()
    # Hull: five facets meeting at one fold point, lit from the top left
    m = (74, 80)
    _poly(painter, ((44, 64), (94, 60), m), PAPER["top"], edge)
    _poly(painter, ((94, 60), (104, 80), m), PAPER["chest"], edge)
    _poly(painter, ((104, 80), (92, 96), m), PAPER["under"], edge)
    _poly(painter, ((40, 94), m, (92, 96)), PAPER["under_back"], edge)
    _poly(painter, ((44, 64), m, (40, 94)), PAPER["back"], edge)
    _fold(painter, (44, 64), (94, 60), 90)
    # Wing: folded flap hinged at the shoulder
    painter.save()
    painter.translate(82, 68)
    painter.rotate(pose.wing)
    painter.translate(-82, -68)
    _poly(painter, ((40, 66), (82, 66), (66, 86)), PAPER["wing"], edge)
    _poly(painter, ((40, 66), (82, 66), (60, 73)), PAPER["wing_fold"], edge)
    painter.restore()
    _head(painter, pose, edge)
    painter.restore()
    for kind, fx_x, fx_y, alpha in pose.fx:
        _fx(painter, kind, fx_x, fx_y, alpha)
    painter.restore()


def render_pose(pose: Pose, px: int, *, mirrored: bool = False) -> QImage:
    """Rasterize a pose to a px-by-px premultiplied ARGB image with a clear background."""
    if px < 16:
        raise ValueError(f"Sprite size {px} is too small; expected at least 16 px.")
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.scale(px / UNITS, px / UNITS)
    paint_pose(painter, pose, mirrored=mirrored)
    painter.end()
    return image


def get_sprite_frames(state: str, size: int = 32) -> list[QImage]:
    """Rendered frames of a clip at `size` px (icons, wheel hub, tests); the pet renders poses itself."""
    return [render_pose(pose, size) for pose in get_poses(state)]
