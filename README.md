# ByteSprite

ByteSprite is a lightweight, frameless, translucent floating pixel-art desktop companion for Windows 10 and Windows 11. Built on Python and PyQt6 using a Model-View-Controller architecture, it runs with zero external asset files by generating crisp 16x16 and 32x32 pixel-art animations directly from embedded RGBA palettes.

## Features

- **Headless Simulation Engine:** Discrete-tick state machine tracking Food (`hunger`), Energy (`energy`), Happiness (`happiness`), Experience, and Level across `Idle`, `Playing`, `Hungry`, and `Sleeping` moods.
- **Crisp High-DPI Pixel Art:** Nearest-neighbor (`FastTransformation` / unsmoothed `QImage`) rendering preserves sharp pixel edges across 100%, 125%, and 150% Windows display scaling.
- **Zero-Clutter Hover HUD:** Stays cleanly transparent on the desktop while idle, revealing a retro pixel status card (`LV`, `FD`, `EN`, `HP`) on cursor hover or care interactions.
- **Multi-Monitor Drag & Clamping:** Smooth left-click dragging across single or multi-monitor setups (including negative-coordinate secondary displays) with automatic desktop work-area boundary clamping.
- **Interactive Care & System Tray:** Left-click to pet, drag to reposition, or right-click (on the sprite or system tray icon) to Feed, Play, Put to Sleep / Wake Up, toggle Always on Top, toggle Gentle Wander, Reset Position, or Quit.
- **Resilient Atomic Persistence:** Auto-saves every 60 seconds and on application or OS shutdown to `~/.bytesprite/state.json` via atomic temporary-file replacement (`os.replace`) with automatic fallback recovery from corrupted JSON.

## Project Structure

```text
desktop_pet/
├── pyproject.toml
├── requirements.txt
├── src/
│   ├── main.py                      # QApplication assembly, timers, system tray, shutdown
│   ├── engine/
│   │   ├── models.py                # PetStats dataclass and PetState enum
│   │   └── pet_state.py             # PetStateMachine simulation logic and callbacks
│   ├── graphics/
│   │   ├── sprite_matrix.py         # Embedded 16x16 and 32x32 RGBA pixel matrices
│   │   └── animation_controller.py  # Frame duration playback, looping, and oneshots
│   ├── ui/
│   │   ├── pet_window.py            # Frameless translucent QWidget, HUD, and screen clamping
│   │   └── context_menu.py          # Dark pixel-styled right-click QMenu
│   └── storage/
│       └── storage_manager.py       # Atomic JSON save/load and corruption recovery
└── tests/
    ├── test_pet_state.py
    ├── test_sprites.py
    ├── test_window_geometry.py
    ├── test_storage.py
    └── test_app_integration.py
```

## Setup

Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Or with `uv`:

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\python.exe -m pip install PyQt6 pytest
```

## Running ByteSprite

Launch the desktop companion from the repository root:

```powershell
.\.venv\Scripts\python.exe -m src.main
```

Optional custom state path:

```powershell
.\.venv\Scripts\python.exe -m src.main --state-file .\custom_state.json
```

## Controls

- **Left-Click (no drag):** Pet ByteSprite (`+HAPPY` and XP).
- **Left-Click + Drag:** Move ByteSprite anywhere across visible monitors.
- **Hover:** Reveal the floating pixel HUD (`LV`, `FD`, `EN`, `HP`, current mood).
- **Right-Click (Sprite or System Tray):** Open the dark pixel context menu (`Feed Berry`, `Play Game`, `Pet Companion`, `Put to Sleep` / `Wake Up`, `Always on Top`, `Gentle Wander`, `Reset Position`, `Quit`).

## Running Tests

Execute the unit and integration test suite:

```powershell
.\.venv\Scripts\pytest.exe -v
```
