# ByteSprite

<p align="center">
  <img src="docs/images/idle.png" width="144" alt="ByteSprite idling">
  <img src="docs/images/music.png" width="144" alt="ByteSprite grooving to music">
  <img src="docs/images/spin.png" width="144" alt="ByteSprite spinning">
</p>

A small frosted-glass paperclip that lives on your Windows desktop. It shows synced lyrics for whatever
Pear Music is playing, carries a pocket full of developer and file tools behind a click, a hold
or a drag, and can open an AI chat with the model and key you bring.

Windows 10 / 11, Python 3.11+, PyQt6. No telemetry, no asset files: every sprite is generated in code.

## Install

Per-user, no admin rights. In PowerShell:

```powershell
irm https://raw.githubusercontent.com/Boci0/bytesprite/main/install.ps1 | iex
```

Or from a clone:

```powershell
git clone https://github.com/Boci0/bytesprite.git
cd bytesprite
powershell -ExecutionPolicy Bypass -File .\install.ps1 -DesktopShortcut
```

Options: `-DesktopShortcut`, `-StartWithWindows`, `-NoLaunch`. If Python is missing:
`winget install Python.Python.3.12`.

| Location | Contents |
| :--- | :--- |
| `%LOCALAPPDATA%\ByteSprite\app` | Program files (`src`, `assets`, `uninstall.ps1`) |
| `%LOCALAPPDATA%\ByteSprite\venv` | Private Python environment |
| `%USERPROFILE%\.bytesprite` | Settings and state (kept on update and uninstall) |
| `Downloads\ByteSprite` | Images dropped from a browser |
| `Pictures\ByteSprite` | Screen snips |

The installer adds a Start Menu shortcut and an entry in **Settings > Apps**, which runs the uninstaller.
There is no `.exe`: the shortcut runs `pythonw.exe -m src.main` from the private environment. Launch it
from the Start Menu (search "ByteSprite"), or re-run the installer with `-DesktopShortcut` for a desktop icon.

### Updating

Right-click the pet, then **Settings > Check for Updates**. It downloads the newest GitHub release,
swaps in the new code and restarts. Updates are signed: the app only installs a release whose
signature matches the ByteSprite release key built into it (toast `BAD SIG` or `UNSIGNED` otherwise),
so a hijacked GitHub account or an intercepted download cannot push code to you. An installed copy
also checks quietly once per launch and shows a toast when a release is out. Re-running `install.ps1` updates too (and refreshes dependencies).
A copy run straight from a git clone has no install marker, so **Check for Updates** only opens the
release page there; `git pull` instead.

### Uninstalling

**Settings > Apps > ByteSprite > Uninstall**, or:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ByteSprite\app\uninstall.ps1" -Purge
```

`-Purge` also deletes your settings.

## Pear Music lyrics

Right-click > **Lyrics > Show Song Lyrics**. ByteSprite reads the playing track from Windows media
controls (no Pear plugin needed), then shows the current line in a small card. Lyrics
come from Pear's own lyrics cache first, so anything Pear can show works, then from
[LRCLIB](https://lrclib.net). Turning it on sends the song title and artist to LRCLIB. While a song
plays, the paperclip grooves along.

## AI chat (opt-in)

Hold the pet and pick **Ask AI**, use **AI Assist > Open AI Chat**, or press `Ctrl+Shift+Alt+A`. The
first time it opens on Setup: pick a provider (DeepSeek, OpenAI, OpenRouter, Google Gemini, Claude, or any
OpenAI-compatible URL), a model and your API key. The key lives in Windows Credential Manager, never
in the settings file. Nothing is sent until you press Enter, and nothing runs in the background.

Attach with `+`, drag-and-drop or paste: text files up to 512 KB go as text, images are read with
local OCR and only that text is sent, never the pixels.

While answering, the model can search the web (through DeepSeek's own search when you have a DeepSeek key,
else an optional Brave Search key) and use a few PC tools: read files in your **Project Folder** (read-only,
never `.env` or key files), read your clipboard or the text on screen, check the pet's readouts, and run
safe pet actions, your macros or https links. It cannot write files or run commands. The header shows
the real model behind an alias, e.g. `DeepSeek-V4.1-Flash` for `deepseek-flash`.

<p align="center">
  <img src="docs/images/moods.png" width="720" alt="Idle, music, focus, spin, sleep and cheer poses">
  <br><sub>Idle, music, focus, spin, sleep, cheer</sub>
</p>

## What else it does

- **Click** cycles the HUD: CPU / RAM / Disk, Pomodoro, File Forge, Clip Vault, Project (git), Lyrics, Hints.
- **Double-click** snips a screen region to the clipboard.
- **Hold** opens a tool wheel: OCR, clipboard history, search, focus timer, mute, Ask AI, web shortcuts.
  Drag onto a slice and release to run it; release over the center or off the ring to close it.
  **Settings > Customize Wheel** adds extra tools (Snip, Color, JSON, UUID, VS Code, Terminal, Lyrics)
  or your own website and app slices, up to 10.
- **Smart hints** (offline, no network): copy a JWT, an epoch timestamp or one-line JSON and the pet
  offers to decode or format it; ten minutes in a code editor suggests a focus session. Click to run,
  right-click to mute that hint. Turn off under **Hints > Smart Suggestions**.
- **Drag files onto it** to convert or compress: WebP / PNG / JPG / ICO / BMP, CSV / TSV / JSON,
  ZIP / TAR.XZ / TAR.GZ / GZIP, extract, SHA-256. Drag an image out of a browser to save a clean copy.
  The output folder opens behind your current window (unless it is already open).
- **Right-click** for everything else: ports inspector, dev transforms (JWT, URL, timestamps, case,
  UUID), recent files, Pomodoro, settings.
- **Hotkeys:** `Ctrl+Shift+Alt+S` snip image, `T` snip text (OCR), `C` pick a color, `A` AI chat.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m src.main --console
.\.venv\Scripts\pytest.exe
```

### Releasing

1. Bump `__version__` in `src/__init__.py` (the single version source) and commit.
2. Build, sign and publish: `python scripts/release.py publish vX.Y.Z`. It refuses a dirty tree, builds
   `dist/bytesprite.zip` with `git archive`, signs it with the key at
   `~/.bytesprite-signing/release_ed25519.key`, tags `HEAD`, pushes the branch and tag, and runs
   `gh release create --verify-tag` with the zip and its `.sig`.
   A release without both files cannot be installed by the updater.

The signing key is created once with `python scripts/release.py keygen`; its public half is
`RELEASE_PUBLIC_KEY` in `src/updater.py`. Never commit or upload the private key, and back it up
offline: if it is lost, installs can only move to a new key by re-running `install.ps1`.

Installed copies pick the release up through **Check for Updates**. The updater replaces `src/` only;
when `requirements.txt` changes, say in the release notes to re-run `install.ps1`.

## License

MIT, see [LICENSE](LICENSE).
