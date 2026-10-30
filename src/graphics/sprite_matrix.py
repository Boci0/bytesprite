"""Embedded procedural 16x16 and 32x32 RGBA pixel-art sprite matrices for ByteSprite."""

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


def _rect(grid: list[list[str]], x1: int, y1: int, x2: int, y2: int, ch: str) -> None:
    for y in range(min(y1, y2), max(y1, y2) + 1):
        _hline(grid, x1, x2, y, ch)


def _stamp(grid: list[list[str]], ox: int, oy: int, rows: list[str]) -> None:
    for dy, row in enumerate(rows):
        for dx, ch in enumerate(row):
            if ch != " ":
                _put(grid, ox + dx, oy + dy, ch)


def _grid_to_rgba(grid: list[list[str]]) -> FrameMatrix:
    return [[PALETTE[ch] for ch in row] for row in grid]


# ---------------------------------------------------------------------------
# 32x32 Character Composer
# ---------------------------------------------------------------------------

def _draw_shadow_32(grid: list[list[str]], width_radius: int = 8, y: int = 29) -> None:
    cx_left = 15 - width_radius
    cx_right = 16 + width_radius
    _hline(grid, cx_left + 2, cx_right - 2, y - 1, "s")
    _hline(grid, cx_left, cx_right, y, "s")
    _hline(grid, cx_left + 3, cx_right - 3, y + 1, "s")


def _draw_body_32(
    grid: list[list[str]],
    *,
    by: int = 8,
    squash: int = 0,
    antenna_dx: int = 0,
    antenna_color: str = "A",
    ear_droop: bool = False,
    arms: str = "neutral",
    feet: str = "stand",
) -> None:
    """Render ByteSprite's 32x32 cat-bot body, ears, antenna, arms, and feet."""
    top_y = by + squash
    bot_y = by + 16

    # Cat-ear tufts (left x=8..12, right x=19..23; droopy x=7..11 and x=20..24)
    if ear_droop:
        _stamp(
            grid,
            7,
            top_y - 1,
            [
                " OOO          OOO ",
                "OLLBO        OBBDO",
                "OBBBO        OBBDO",
            ],
        )
    else:
        _stamp(
            grid,
            8,
            top_y - 3,
            [
                " OO          OO ",
                "OLBO        OBBO",
                "OLBBO      OBBDO",
                "OBBBO      OBBDO",
            ],
        )

    # Central antenna stalk + inked 2x2 lantern bulb (high contrast on light & dark desktops)
    ax = 15 + antenna_dx
    _put(grid, 15, top_y - 1, "O")
    _put(grid, 16, top_y - 1, "O")
    _stamp(
        grid,
        ax - 1,
        top_y - 5,
        [
            " OO ",
            f"OW{antenna_color}O",
            f"O{antenna_color}{antenna_color}O",
            " OO ",
        ],
    )

    # Rounded body shell (x=7..24, y=top_y..bot_y)
    _hline(grid, 10, 21, top_y, "O")
    _hline(grid, 8, 23, top_y + 1, "O")
    _hline(grid, 10, 21, top_y + 1, "L")

    _hline(grid, 7, 24, top_y + 2, "O")
    _hline(grid, 9, 22, top_y + 2, "L")
    _put(grid, 10, top_y + 2, "W")
    _put(grid, 11, top_y + 2, "W")

    for y in range(top_y + 3, bot_y - 2):
        _put(grid, 6, y, "O")
        _hline(grid, 7, 24, y, "B")
        _put(grid, 7, y, "L")
        _put(grid, 23, y, "D")
        _put(grid, 24, y, "D")
        _put(grid, 25, y, "O")

    # Lower rounded belly/chin curve
    _put(grid, 7, bot_y - 2, "O")
    _hline(grid, 8, 23, bot_y - 2, "B")
    _hline(grid, 20, 23, bot_y - 2, "D")
    _put(grid, 24, bot_y - 2, "O")

    _put(grid, 8, bot_y - 1, "O")
    _put(grid, 9, bot_y - 1, "O")
    _hline(grid, 10, 21, bot_y - 1, "D")
    _put(grid, 22, bot_y - 1, "O")
    _put(grid, 23, bot_y - 1, "O")

    _hline(grid, 10, 21, bot_y, "O")

    # Cream visor / tummy screen inside body
    face_top = top_y + 4
    face_bot = bot_y - 3
    _hline(grid, 10, 21, face_top, "F")
    for y in range(face_top + 1, face_bot):
        _hline(grid, 9, 22, y, "F")
    _hline(grid, 10, 21, face_bot, "f")

    # Stubby arms
    arm_y = top_y + 8
    if arms == "neutral":
        _stamp(grid, 4, arm_y, [" OO", "OBO", " OO"])
        _stamp(grid, 25, arm_y, ["OO ", "ODO", "OO "])
    elif arms == "up":
        _stamp(grid, 4, arm_y - 3, ["OO ", "OLB", " OO"])
        _stamp(grid, 25, arm_y - 3, [" OO", "BDO", "OO "])
    elif arms == "droop":
        _stamp(grid, 5, arm_y + 2, ["OO", "OB", "OO"])
        _stamp(grid, 25, arm_y + 2, ["OO", "DO", "OO"])
    elif arms == "hold":
        # Paws tucked inward on chest to hold a berry snack
        _stamp(grid, 10, arm_y + 3, ["OOO", "OBO", "OOO"])
        _stamp(grid, 19, arm_y + 3, ["OOO", "ODO", "OOO"])

    # Stubby feet
    if feet == "stand":
        _stamp(grid, 10, bot_y, ["OOOO", "ODDO", " OOO"])
        _stamp(grid, 18, bot_y, ["OOOO", "ODDO", "OOO "])
    elif feet == "walk_l":
        _stamp(grid, 9, bot_y - 1, ["OOOO", "ODDO"])
        _stamp(grid, 18, bot_y, ["OOOO", "ODDO", "OOO "])
    elif feet == "walk_r":
        _stamp(grid, 10, bot_y, ["OOOO", "ODDO", " OOO"])
        _stamp(grid, 19, bot_y - 1, ["OOOO", "ODDO"])
    elif feet == "jump":
        _stamp(grid, 9, bot_y, ["OOO", "ODO"])
        _stamp(grid, 20, bot_y, ["OOO", "ODO"])


def _draw_face_32(
    grid: list[list[str]],
    *,
    fy: int = 14,
    eyes: str = "open",
    mouth: str = "smile",
    blush: bool = True,
) -> None:
    """Draw expressive eyes, cheeks, and mouth onto ByteSprite's visor."""
    if eyes == "open":
        # 2x3 dark eyes with top-left white catchlight
        for ex in (11, 19):
            _rect(grid, ex, fy, ex + 1, fy + 2, "E")
            _put(grid, ex, fy, "W")
    elif eyes == "half":
        for ex in (11, 19):
            _hline(grid, ex, ex + 1, fy + 1, "E")
            _hline(grid, ex, ex + 1, fy + 2, "E")
    elif eyes == "closed":
        _hline(grid, 11, 13, fy + 2, "E")
        _hline(grid, 18, 20, fy + 2, "E")
    elif eyes == "happy":
        # ^ ^ happy arcs
        _stamp(grid, 11, fy, [" E ", "E E"])
        _stamp(grid, 18, fy, [" E ", "E E"])
    elif eyes == "plead":
        for ex in (11, 18):
            _rect(grid, ex, fy, ex + 2, fy + 2, "E")
            _put(grid, ex, fy, "W")
            _put(grid, ex + 2, fy + 2, "W")

    if blush:
        _hline(grid, 9, 10, fy + 3, "C")
        _hline(grid, 21, 22, fy + 3, "C")

    my = fy + 3
    if mouth == "smile":
        _put(grid, 15, my + 1, "E")
        _put(grid, 16, my + 1, "E")
    elif mouth == "cat":
        _put(grid, 14, my, "E")
        _put(grid, 15, my + 1, "E")
        _put(grid, 16, my, "E")
        _put(grid, 17, my + 1, "E")
    elif mouth == "open_happy":
        _rect(grid, 14, my, 17, my + 1, "E")
        _hline(grid, 15, 16, my + 1, "M")
    elif mouth == "sad":
        _hline(grid, 15, 16, my, "E")
        _put(grid, 14, my + 1, "E")
        _put(grid, 17, my + 1, "E")
    elif mouth == "sleep":
        _put(grid, 15, my + 1, "M")
    elif mouth == "munch_open":
        _rect(grid, 15, my, 16, my + 1, "E")
        _put(grid, 15, my + 1, "M")
    elif mouth == "munch_closed":
        _put(grid, 14, my, "E")
        _hline(grid, 15, 16, my + 1, "E")
        _put(grid, 17, my, "E")


def _draw_sleeping_loaf_32(grid: list[list[str]], breath: int, z_step: int) -> None:
    """Cozy curled-up loaf posture for sleeping frames with floating Z particles."""
    _draw_shadow_32(grid, width_radius=10, y=29)
    top_y = 13 - breath
    bot_y = 27

    # Sleepy symmetrical drooped ears + inked periwinkle lantern
    _stamp(
        grid,
        7,
        top_y - 1,
        [
            " OOO          OOO ",
            "OLLBO        OBBDO",
        ],
    )
    _stamp(
        grid,
        14,
        top_y - 4,
        [
            " OO ",
            "OWPO",
            "OPPO",
            " OO ",
        ],
    )

    _hline(grid, 9, 22, top_y, "O")
    _hline(grid, 7, 24, top_y + 1, "O")
    _hline(grid, 9, 22, top_y + 1, "L")
    for y in range(top_y + 2, bot_y - 1):
        _put(grid, 6, y, "O")
        _hline(grid, 7, 24, y, "B")
        _put(grid, 7, y, "L")
        _put(grid, 23, y, "D")
        _put(grid, 24, y, "D")
        _put(grid, 25, y, "O")

    _put(grid, 7, bot_y - 1, "O")
    _hline(grid, 8, 23, bot_y - 1, "D")
    _put(grid, 24, bot_y - 1, "O")
    _hline(grid, 8, 23, bot_y, "O")

    # Face window
    for y in range(top_y + 4, bot_y - 2):
        _hline(grid, 10, 21, y, "F")
    _draw_face_32(grid, fy=top_y + 5, eyes="closed", mouth="sleep", blush=True)

    # Floating pixel Zzz in upper-right
    z_small = ["PPP", " P ", "PPP"]
    z_big = ["PPPP", "  P ", " P  ", "PPPP"]
    positions = [
        (23, 9, z_small),
        (24, 6, z_small),
        (25, 4, z_big),
        (26, 2, z_big),
    ]
    zx, zy, glyph = positions[z_step % 4]
    _stamp(grid, zx, zy, glyph)
    if z_step in (2, 3):
        _stamp(grid, 22, 9, z_small)


def _build_32_animations() -> dict[str, list[FrameMatrix]]:
    animations: dict[str, list[FrameMatrix]] = {}

    # 1. IDLE (4-frame gentle breathing + antenna follow-through)
    idle_frames: list[FrameMatrix] = []
    for by, sq, adx, shadow_r in [
        (8, 0, 0, 8),
        (8, 1, 0, 9),
        (9, 1, 1, 9),
        (8, 0, -1, 8),
    ]:
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=shadow_r, y=29)
        _draw_body_32(g, by=by, squash=sq, antenna_dx=adx, arms="neutral", feet="stand")
        _draw_face_32(g, fy=by + sq + 6, eyes="open", mouth="smile")
        idle_frames.append(_grid_to_rgba(g))
    animations["idle"] = idle_frames

    # 2. BLINK (3-frame crisp blink cycle)
    blink_frames: list[FrameMatrix] = []
    for eye_mode in ("half", "closed", "half"):
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=8, y=29)
        _draw_body_32(g, by=8, squash=0, antenna_dx=0, arms="neutral", feet="stand")
        _draw_face_32(g, fy=14, eyes=eye_mode, mouth="smile")
        blink_frames.append(_grid_to_rgba(g))
    animations["blink"] = blink_frames

    # 3. WALK (4-frame bouncy hop/stride)
    walk_frames: list[FrameMatrix] = []
    for by, feet_mode, adx, arm_mode, shadow_r in [
        (7, "walk_l", -1, "neutral", 7),
        (8, "stand", 0, "neutral", 8),
        (7, "walk_r", 1, "neutral", 7),
        (8, "stand", 0, "neutral", 8),
    ]:
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=shadow_r, y=29)
        _draw_body_32(g, by=by, squash=0, antenna_dx=adx, arms=arm_mode, feet=feet_mode)
        _draw_face_32(g, fy=by + 6, eyes="open", mouth="cat")
        walk_frames.append(_grid_to_rgba(g))
    animations["walk"] = walk_frames

    # 4. SLEEP (4-frame cozy loaf + rising Zzz)
    sleep_frames: list[FrameMatrix] = []
    for breath, z_step in [(0, 0), (1, 1), (1, 2), (0, 3)]:
        g = _new_grid(32)
        _draw_sleeping_loaf_32(g, breath=breath, z_step=z_step)
        sleep_frames.append(_grid_to_rgba(g))
    animations["sleep"] = sleep_frames

    # 5. EXCITED (4-frame joyful jump + raised paws + sparkles & pixel heart)
    excited_frames: list[FrameMatrix] = []
    heart_glyph = [
        "CC CC",
        "CCCCC",
        " CCC ",
        "  C  ",
    ]
    sparkle_glyph = [
        " G ",
        "GWG",
        " G ",
    ]
    for step, (by, sq, feet_mode, arms_mode, shadow_r) in enumerate([
        (9, 1, "stand", "neutral", 9),
        (6, 0, "jump", "up", 6),
        (5, 0, "jump", "up", 5),
        (7, 0, "stand", "up", 7),
    ]):
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=shadow_r, y=29)
        _draw_body_32(
            g,
            by=by,
            squash=sq,
            antenna_dx=(1 if step % 2 else -1),
            antenna_color="G",
            arms=arms_mode,
            feet=feet_mode,
        )
        _draw_face_32(g, fy=by + sq + 6, eyes="happy", mouth="open_happy")
        if step in (1, 2):
            _stamp(g, 2, 4 - (step - 1), sparkle_glyph)
            _stamp(g, 25, 3 - (step - 1), heart_glyph)
        elif step == 3:
            _stamp(g, 3, 2, sparkle_glyph)
        excited_frames.append(_grid_to_rgba(g))
    animations["excited"] = excited_frames

    # 6. HUNGRY (4-frame droopy ears, pleading eyes, sweat drop, tummy rumble)
    hungry_frames: list[FrameMatrix] = []
    for step in range(4):
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=9, y=29)
        _draw_body_32(
            g,
            by=9,
            squash=1 if step % 2 else 0,
            antenna_dx=0,
            antenna_color="R" if step % 2 == 0 else "A",
            ear_droop=True,
            arms="droop",
            feet="stand",
        )
        _draw_face_32(g, fy=15 + (1 if step % 2 else 0), eyes="plead", mouth="sad", blush=False)
        # Pixel sweat drop on temple
        _stamp(g, 25, 11 + (step % 2), [" T ", "TTT", " T "])
        hungry_frames.append(_grid_to_rgba(g))
    animations["hungry"] = hungry_frames

    # 7. EATING (4-frame snack munching with inked pixel berry held between paws)
    eating_frames: list[FrameMatrix] = []
    berry_full = [
        " OL ",
        "ORRO",
        "ORWO",
        " OO ",
    ]
    berry_half = [
        " OL ",
        "ORRO",
        " OO ",
    ]
    for step in range(4):
        g = _new_grid(32)
        _draw_shadow_32(g, width_radius=8, y=29)
        _draw_body_32(g, by=8, squash=(step % 2), antenna_dx=0, antenna_color="G", arms="hold", feet="stand")
        mouth_mode = "munch_open" if step % 2 == 0 else "munch_closed"
        eye_mode = "happy" if step >= 1 else "open"
        _draw_face_32(g, fy=14 + (step % 2), eyes=eye_mode, mouth=mouth_mode, blush=True)
        if step < 2:
            _stamp(g, 14, 19, berry_full)
        elif step == 2:
            _stamp(g, 14, 20, berry_half)
        else:
            # Happy crumbs + gold sparkle
            _put(g, 13, 21, "R")
            _put(g, 18, 20, "A")
        eating_frames.append(_grid_to_rgba(g))
    animations["eating"] = eating_frames

    return animations


# ---------------------------------------------------------------------------
# 16x16 Compact / Tray Pixel Matrices
# ---------------------------------------------------------------------------

def _build_16_base(
    *,
    by: int = 3,
    eyes: str = "open",
    antenna: str = "A",
    feet_shift: int = 0,
    extra: str = "",
) -> FrameMatrix:
    g = _new_grid(16)
    # Shadow
    _hline(g, 4, 11, 15, "s")
    # Ears + antenna
    _put(g, 4, by - 1, "O")
    _put(g, 11, by - 1, "O")
    _put(g, 7, max(0, by - 2), antenna)
    _put(g, 8, max(0, by - 2), "W")
    # Shell
    _hline(g, 4, 11, by, "O")
    _hline(g, 3, 12, by + 1, "O")
    _hline(g, 4, 11, by + 1, "L")
    for y in range(by + 2, by + 9):
        _put(g, 2, y, "O")
        _hline(g, 3, 12, y, "B")
        _put(g, 12, y, "D")
        _put(g, 13, y, "O")
    _hline(g, 3, 12, by + 9, "O")
    # Visor
    for y in range(by + 3, by + 8):
        _hline(g, 4, 11, y, "F")
    # Eyes
    ey = by + 4
    if eyes == "open":
        _put(g, 5, ey, "W")
        _put(g, 5, ey + 1, "E")
        _put(g, 10, ey, "W")
        _put(g, 10, ey + 1, "E")
    elif eyes == "closed":
        _hline(g, 5, 6, ey + 1, "E")
        _hline(g, 9, 10, ey + 1, "E")
    elif eyes == "happy":
        _put(g, 5, ey, "E")
        _put(g, 6, ey + 1, "E")
        _put(g, 9, ey + 1, "E")
        _put(g, 10, ey, "E")
    # Blush
    _put(g, 4, ey + 2, "C")
    _put(g, 11, ey + 2, "C")
    # Feet
    _hline(g, 4 + feet_shift, 5 + feet_shift, min(14, by + 10), "O")
    _hline(g, 10 - feet_shift, 11 - feet_shift, min(14, by + 10), "O")

    if extra == "z1":
        _put(g, 13, 2, "P")
        _put(g, 14, 1, "P")
    elif extra == "z2":
        _put(g, 14, 2, "P")
        _put(g, 13, 1, "P")
    elif extra == "spark":
        _put(g, 1, 2, "G")
        _put(g, 14, 2, "C")
    elif extra == "berry":
        _put(g, 7, ey + 2, "R")
        _put(g, 8, ey + 2, "R")

    # Ensure top corners remain strictly transparent
    _put(g, 0, 0, ".")
    _put(g, 15, 0, ".")
    return _grid_to_rgba(g)


def _build_16_animations() -> dict[str, list[FrameMatrix]]:
    return {
        "idle": [
            _build_16_base(by=3, eyes="open"),
            _build_16_base(by=4, eyes="open"),
        ],
        "blink": [
            _build_16_base(by=3, eyes="closed"),
            _build_16_base(by=3, eyes="open"),
        ],
        "walk": [
            _build_16_base(by=2, eyes="open", feet_shift=1),
            _build_16_base(by=3, eyes="open", feet_shift=0),
            _build_16_base(by=2, eyes="open", feet_shift=-1),
            _build_16_base(by=3, eyes="open", feet_shift=0),
        ],
        "sleep": [
            _build_16_base(by=4, eyes="closed", antenna="P", extra="z1"),
            _build_16_base(by=4, eyes="closed", antenna="P", extra="z2"),
        ],
        "excited": [
            _build_16_base(by=2, eyes="happy", antenna="G", extra="spark"),
            _build_16_base(by=3, eyes="happy", antenna="A"),
        ],
        "hungry": [
            _build_16_base(by=4, eyes="open", antenna="R"),
            _build_16_base(by=4, eyes="closed", antenna="A"),
        ],
        "eating": [
            _build_16_base(by=3, eyes="happy", antenna="G", extra="berry"),
            _build_16_base(by=4, eyes="happy", antenna="G"),
        ],
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
