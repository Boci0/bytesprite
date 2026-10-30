"""Embedded procedural 16x16 and 32x32 RGBA pixel-art paperclip sprite matrices for ByteSprite."""

from __future__ import annotations

from functools import lru_cache

RGBA = tuple[int, int, int, int]
FrameMatrix = list[list[RGBA]]

REQUIRED_ANIMATIONS: tuple[str, ...] = (
    "idle",
    "blink",
    "walk",
    "sleep",
    "excited",
)

ALL_ANIMATIONS: tuple[str, ...] = (
    "idle",
    "blink",
    "walk",
    "sleep",
    "excited",
    "hungry",
    "eating",
)

# Frosted-glass neutral palette: low-saturation translucent greys that read on light and dark
# desktops alike, so the companion blends in instead of drawing the eye
PALETTE: dict[str, RGBA] = {
    ".": (0, 0, 0, 0),          # Transparent
    "s": (0, 0, 0, 34),         # Soft ground shadow
    "h": (255, 255, 255, 40),   # Faint antenna / sparkle aura
    "O": (46, 50, 58, 190),     # Smoky graphite outline
    "D": (118, 126, 138, 120),  # Shade glass
    "B": (168, 175, 186, 105),  # Frosted body midtone
    "L": (214, 219, 227, 125),  # Top highlight
    "W": (255, 255, 255, 190),  # Glint / catchlight
    "F": (232, 235, 240, 120),  # Face / belly frost
    "f": (198, 204, 212, 110),  # Belly lower shade
    "E": (32, 34, 40, 215),     # Eye
    "C": (206, 178, 188, 70),   # Barely-there blush
    "A": (196, 202, 212, 150),  # Antenna bulb
    "G": (236, 239, 245, 150),  # Soft glow
    "R": (214, 128, 108, 170),  # Muted alert tint
    "P": (168, 174, 198, 140),  # Sleepy Zzz
    "M": (92, 84, 94, 170),     # Mouth accent
    "T": (168, 206, 214, 130),  # Sweat drop / particle
}


def _new_grid(size: int) -> list[list[str]]:
    return [["." for _ in range(size)] for _ in range(size)]


def _put(grid: list[list[str]], x: int, y: int, ch: str) -> None:
    size = len(grid)
    if 0 <= x < size and 0 <= y < size:
        grid[y][x] = ch


def _hline(grid: list[list[str]], x1: int, x2: int, y: int, ch: str) -> None:
    for x in range(min(x1, x2), max(x1, x2) + 1):
        _put(grid, x, y, ch)


def _stamp(grid: list[list[str]], ox: int, oy: int, rows: list[str]) -> None:
    for dy, row in enumerate(rows):
        for dx, ch in enumerate(row):
            if ch != " ":
                _put(grid, ox + dx, oy + dy, ch)


def _grid_to_rgba(grid: list[list[str]]) -> FrameMatrix:
    return [[PALETTE[ch] for ch in row] for row in grid]


# ---------------------------------------------------------------------------
# Paperclip Character Composer (shared wire tracer)
# ---------------------------------------------------------------------------

# Gem-clip centerline: inner end -> inner top loop -> bottom loop -> outer top loop -> outer end
_CLIP_PATH_32: tuple[tuple[int, int], ...] = (
    (13, 20), (13, 9), (15, 7), (17, 7), (19, 9), (19, 24), (17, 26), (11, 26),
    (9, 24), (9, 6), (11, 4), (20, 4), (22, 6), (22, 20),
)
_CLIP_PATH_16: tuple[tuple[int, int], ...] = (
    (6, 10), (6, 5), (7, 4), (8, 4), (9, 5), (9, 12), (8, 13), (5, 13),
    (4, 12), (4, 3), (5, 2), (10, 2), (11, 3), (11, 10),
)


def _line_points(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham line including both endpoints."""
    points: list[tuple[int, int]] = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return points
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _trace_wire(
    grid: list[list[str]],
    path: tuple[tuple[int, int], ...],
    *,
    ox: int,
    oy: int,
    brush: int,
    core: str,
    outline: str | None,
) -> None:
    """Draw a `brush`-pixel-thick wire along `path`, optionally ringed with an outline."""
    cells: set[tuple[int, int]] = set()
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        for x, y in _line_points(x0, y0, x1, y1):
            for bx in range(brush):
                for by in range(brush):
                    cells.add((x + ox + bx, y + oy + by))
    if outline is not None:
        for x, y in cells:
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    if (nx, ny) not in cells:
                        _put(grid, nx, ny, outline)
    for x, y in cells:
        _put(grid, x, y, core)


# ---------------------------------------------------------------------------
# 32x32 Character Composer
# ---------------------------------------------------------------------------

_EYES_32: dict[str, list[str]] = {
    "open": [" OOO ", "OWWWO", "OWEEO", "OWEEO", " OOO "],
    "look_left": [" OOO ", "OWWWO", "OEEWO", "OEEWO", " OOO "],
    "half": ["     ", "OOOOO", "OWEEO", "OWEEO", " OOO "],
    "closed": [" OOO ", "OLLLO", "OEEEO", "OLLLO", " OOO "],
    "happy": [" OOO ", "OLLLO", "OLELO", "OELEO", " OOO "],
    "plead": [" OOO ", "OWWWO", "OEEEO", "OEWEO", " OOO "],
}
_MOUTHS_32: dict[str, list[str]] = {
    "smile": ["M  M", " MM "],
    "open_happy": ["MMMM", "MWWM", " MM "],
    "flat": [" MM "],
    "sad": [" MM ", "M  M"],
    "munch_open": [" MM ", "M  M", " MM "],
    "munch_closed": ["MMMM"],
}


def _draw_shadow_32(grid: list[list[str]], width_radius: int = 8, y: int = 29) -> None:
    cx_left = 15 - width_radius
    cx_right = 16 + width_radius
    _hline(grid, cx_left + 2, cx_right - 2, y - 1, "s")
    _hline(grid, cx_left, cx_right, y, "s")
    _hline(grid, cx_left + 3, cx_right - 3, y + 1, "s")


def _draw_clip_32(
    grid: list[list[str]],
    *,
    dx: int = 0,
    dy: int = 0,
    eyes: str = "open",
    mouth: str = "smile",
    blush: bool = False,
) -> None:
    """Render the frosted paperclip buddy with a face across its upper loops."""
    _trace_wire(grid, _CLIP_PATH_32, ox=dx, oy=dy, brush=2, core="L", outline="O")
    # Catchlights on the outer top bend
    _put(grid, 11 + dx, 4 + dy, "W")
    _put(grid, 12 + dx, 4 + dy, "W")
    _stamp(grid, 9 + dx, 9 + dy, _EYES_32[eyes])
    _stamp(grid, 17 + dx, 9 + dy, _EYES_32[eyes])
    _stamp(grid, 14 + dx, 16 + dy, _MOUTHS_32[mouth])
    if blush:
        _put(grid, 10 + dx, 15 + dy, "C")
        _put(grid, 20 + dx, 15 + dy, "C")


def _build_32_animations() -> dict[str, list[FrameMatrix]]:
    animations: dict[str, list[FrameMatrix]] = {}
    sparkle_glyph = [" G ", "GWG", " G "]
    heart_glyph = ["CC CC", "CCCCC", " CCC ", "  C  "]
    z_big = ["PPP", " P ", "PPP"]
    z_small = ["PP", "PP"]

    def frame(
        *, dx: int = 0, dy: int = 0, eyes: str = "open", mouth: str = "smile",
        shadow: int = 8, blush: bool = False,
        extras: tuple[tuple[int, int, list[str]], ...] = (),
    ) -> FrameMatrix:
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=shadow, y=29)
        _draw_clip_32(g, dx=dx, dy=dy, eyes=eyes, mouth=mouth, blush=blush)
        for ex, ey, glyph in extras:
            _stamp(g, ex, ey, glyph)
        return _grid_to_rgba(g)

    # 1. IDLE (gentle bob)
    animations["idle"] = [
        frame(dy=0), frame(dy=0, shadow=9), frame(dy=1, shadow=9), frame(dy=0),
    ]
    # 2. BLINK
    animations["blink"] = [frame(eyes="half"), frame(eyes="closed"), frame(eyes="half")]
    # 3. WALK (sway side to side while glancing along)
    animations["walk"] = [
        frame(dx=-1, dy=-1, eyes="look_left", shadow=7),
        frame(dx=0, dy=0, eyes="open"),
        frame(dx=1, dy=-1, eyes="open", shadow=7),
        frame(dx=0, dy=0, eyes="look_left"),
    ]
    # 4. SLEEP (DND: settled low, closed eyes, rising Zzz)
    animations["sleep"] = [
        frame(dy=1, eyes="closed", mouth="flat", shadow=9, extras=((25, 6, z_small),)),
        frame(dy=2, eyes="closed", mouth="flat", shadow=9, extras=((25, 4, z_big),)),
        frame(dy=2, eyes="closed", mouth="flat", shadow=9, extras=((26, 2, z_big), (24, 8, z_small))),
        frame(dy=1, eyes="closed", mouth="flat", shadow=9, extras=((24, 6, z_small),)),
    ]
    # 5. EXCITED (FOCUS: hop with sparkles)
    animations["excited"] = [
        frame(dy=1, eyes="happy", mouth="open_happy", shadow=9, blush=True),
        frame(dy=-2, eyes="happy", mouth="open_happy", shadow=6, blush=True,
              extras=((2, 4, sparkle_glyph), (26, 3, heart_glyph))),
        frame(dy=-3, eyes="happy", mouth="open_happy", shadow=5, blush=True,
              extras=((2, 3, sparkle_glyph), (26, 2, heart_glyph))),
        frame(dy=-1, eyes="happy", mouth="smile", shadow=7, blush=True,
              extras=((3, 2, sparkle_glyph),)),
    ]
    # 6. HUNGRY (ALERT: worried wobble with sweat drop)
    sweat = [" T ", "TTT", " T "]
    animations["hungry"] = [
        frame(dx=step % 2, dy=1, eyes="plead", mouth="sad", shadow=9,
              extras=((25, 9 + (step % 2), sweat),))
        for step in range(4)
    ]
    # 7. EATING (one-shot munch)
    animations["eating"] = [
        frame(eyes="open", mouth="munch_open", blush=True),
        frame(dy=1, eyes="happy", mouth="munch_closed", blush=True),
        frame(eyes="happy", mouth="munch_open", blush=True),
        frame(dy=1, eyes="happy", mouth="smile", blush=True, extras=((24, 6, sparkle_glyph),)),
    ]
    return animations


# ---------------------------------------------------------------------------
# 16x16 Compact / Tray Pixel Matrices
# ---------------------------------------------------------------------------

def _build_16_base(*, dy: int = 0, eyes: str = "open", extra: str = "") -> FrameMatrix:
    g = _new_grid(16)
    _hline(g, 4, 11, 15, "s")
    _trace_wire(g, _CLIP_PATH_16, ox=0, oy=dy, brush=1, core="O", outline=None)
    ey = 6 + dy
    if eyes == "open":
        for ex in (5, 8):
            _put(g, ex, ey, "W")
            _put(g, ex, ey + 1, "E")
    elif eyes == "closed":
        _put(g, 5, ey + 1, "E")
        _put(g, 8, ey + 1, "E")
    elif eyes == "happy":
        for ex in (5, 8):
            _put(g, ex, ey, "E")
    if extra == "z":
        _put(g, 13, 1, "P")
        _put(g, 14, 0, "P")
    elif extra == "spark":
        _put(g, 1, 2, "G")
        _put(g, 14, 2, "C")
    elif extra == "sweat":
        _put(g, 13, 5, "T")
    # Ensure top corners remain strictly transparent
    _put(g, 0, 0, ".")
    _put(g, 15, 0, ".")
    return _grid_to_rgba(g)


def _build_16_animations() -> dict[str, list[FrameMatrix]]:
    return {
        "idle": [_build_16_base(dy=0), _build_16_base(dy=1)],
        "blink": [_build_16_base(eyes="closed"), _build_16_base(eyes="open")],
        "walk": [
            _build_16_base(dy=0), _build_16_base(dy=1),
            _build_16_base(dy=0, eyes="happy"), _build_16_base(dy=1),
        ],
        "sleep": [
            _build_16_base(dy=1, eyes="closed", extra="z"),
            _build_16_base(dy=1, eyes="closed"),
        ],
        "excited": [_build_16_base(dy=0, eyes="happy", extra="spark"), _build_16_base(dy=1, eyes="happy")],
        "hungry": [_build_16_base(dy=1, extra="sweat"), _build_16_base(dy=1, eyes="closed", extra="sweat")],
        "eating": [_build_16_base(eyes="happy"), _build_16_base(dy=1, eyes="happy")],
    }


@lru_cache(maxsize=4)
def _cached_bank(size: int) -> dict[str, list[FrameMatrix]]:
    if size == 32:
        return _build_32_animations()
    if size == 16:
        return _build_16_animations()
    raise ValueError(f"Unsupported sprite matrix size {size}; expected 16 or 32.")


def get_sprite_frames(state: str, size: int = 32) -> list[FrameMatrix]:
    """Return RGBA pixel matrix frames for a named animation state and resolution."""
    bank = _cached_bank(size)
    if state not in bank:
        raise KeyError(f"Unknown sprite animation state: {state!r}")
    return bank[state]


def flip_frame_horizontal(frame: FrameMatrix) -> FrameMatrix:
    """Return a horizontally mirrored copy of a sprite frame matrix."""
    return [list(reversed(row)) for row in frame]
