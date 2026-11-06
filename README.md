# ByteSprite

<p align="center">
  <img src="docs/images/idle.png" width="144" alt="ByteSprite idling">
  <img src="docs/images/spin.png" width="144" alt="ByteSprite quacking">
</p>

A small origami rubber duck that lives on your Windows desktop, ready for rubber duck debugging. It carries a
pocket full of developer and file tools behind a click, a hold or a drag, and can open an AI chat with the model
and key you bring.

Windows 10 / 11, PyQt6 (the installed app bundles its own Python). No telemetry, no asset files: the duck
is drawn and animated in code.

## Install

Per-user, no admin rights. In PowerShell:

```powershell
irm https://raw.githubusercontent.com/Boci0/bytesprite/main/install.ps1 | iex
```

It downloads the latest signed release (a standalone `ByteSprite.exe` build, no Python needed) and
refuses to install it unless the signature checks out. Re-running it over an older install replaces it.

Or from a clone, which installs from source into a private Python 3.11+ environment:

```powershell
git clone https://github.com/Boci0/bytesprite.git
cd bytesprite
powershell -ExecutionPolicy Bypass -File .\install.ps1 -DesktopShortcut
```

Options: `-DesktopShortcut`, `-StartWithWindows`, `-NoLaunch`. A clone install needs Python; if it is
missing: `winget install Python.Python.3.12`.

| Location | Contents |
| :--- | :--- |
| `%LOCALAPPDATA%\ByteSprite\app` | Program files (`ByteSprite.exe` and `_internal`, or `src` and `assets` for a clone install, plus `uninstall.ps1`) |
| `%LOCALAPPDATA%\ByteSprite\venv` | Private Python environment (clone installs only) |
| `%USERPROFILE%\.bytesprite` | Settings and state (kept on update and uninstall) |
| `Downloads\ByteSprite` | Images dropped from a browser |
| `Pictures\ByteSprite` | Screen snips |

The installer adds a Start Menu shortcut and an entry in **Settings > Apps**, which runs the uninstaller.
Launch it from the Start Menu (search "ByteSprite"), or re-run the installer with `-DesktopShortcut` for a
desktop icon. The `.exe` is not code-signed, so antivirus tools may be wary of it on first run.

### Updating

Right-click the pet, then **Settings > Check for Updates**. It downloads the newest GitHub release
(about 36 MB for the standalone build, which ships its whole runtime), swaps in the new version and restarts. Updates are signed: the app only installs a release whose
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

## AI chat (opt-in)

Hold the pet and pick **Ask AI**, use **AI Assist > Open AI Chat**, or press `Ctrl+Shift+Alt+A`. The
first time it opens on Setup: pick a provider (DeepSeek, OpenAI, OpenRouter, Google Gemini, Claude, or any
OpenAI-compatible URL), a model and your API key. The key lives in Windows Credential Manager, never
in the settings file. Nothing is sent until you press Enter, and nothing runs in the background.

Attach with `+`, drag-and-drop or paste: text files up to 512 KB go as text, images are read with
local OCR and only that text is sent, never the pixels.

While answering, the model can search the web (through DeepSeek's own search when you have a DeepSeek key,
else an optional Brave Search key) and use your PC: read and search files anywhere (relative paths start
in your **Project Folder**, or your home folder when none is set), read your clipboard or the screen, click
and type, and run pet actions, your macros or links. Anything that changes something asks you first in the
chat: every file edit shows its diff, every command shows the exact command line, and a `.env` or key file
is read only after you allow that file. It opens and reads web links freely; once it has read your files,
clipboard or screen, a link it made up itself shows you the full address first, so your data cannot leave
inside a URL without you seeing it. The reach is the same for every model; how well it uses it depends
on the model. The header shows the real model behind an alias, e.g. `DeepSeek-V4.1-Flash` for `deepseek-flash`.

<p align="center">
  <img src="docs/images/moods.png" width="720" alt="Idle, music, focus, quack, sleep and cheer poses">
  <br><sub>Idle, music, focus, quack, sleep, cheer</sub>
</p>

## What else it does

- **Click** cycles the HUD: CPU / RAM / Disk, Pomodoro, File Forge, Clip Vault, Project (git), Hints.
- **Double-click** snips a screen region to the clipboard.
- **Hold** opens a tool wheel: OCR, clipboard history, search, focus timer, mute, Ask AI, web shortcuts.
  Drag onto a slice and release to run it; release over the center or off the ring to close it.
  **Settings > Customize Wheel** adds extra tools (Snip, Color, JSON, UUID, VS Code, Terminal)
  or your own website and app slices, up to 10.
- **Smart hints** (offline, no network): copy a JWT, an epoch timestamp or one-line JSON and the pet
  offers to decode or format it; ten minutes in a code editor suggests a focus session. Click to run,
  right-click to mute that hint. Turn off under **Hints > Smart Suggestions**.
- **Watch mode** (opt-in, uses AI, **Hints > Watch Mode**): copy an error, a foreign text, a long article
  or an odd-looking link, or sit on an error window, and the pet offers to explain, translate, summarize or
  check it. When you click, it can act too: for a copied error it opens the file and proposes the fix as a
  diff you approve. Capped at 40 calls a day; `Alt+`` asks about the front window any time.
- **Drag files onto it** to convert or compress: WebP / PNG / JPG / ICO / BMP, CSV / TSV / JSON,
  ZIP / TAR.XZ / TAR.GZ / GZIP, extract, SHA-256. Drag an image out of a browser to save a clean copy.
  The output folder opens behind your current window (unless it is already open).
- **Right-click** for everything else: ports inspector, dev transforms (JWT, URL, timestamps, case,
  UUID), recent files, Pomodoro, settings.
- **Hotkeys:** `Ctrl+Shift+Alt+S` snip image, `T` snip text (OCR), `C` pick a color, `A` AI chat.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe -m src.main --console
.\.venv\Scripts\pytest.exe
```

### Releasing

1. Bump `__version__` in `src/__init__.py` (the single version source) and commit.
2. Build, sign and publish with Python 3.12 (the standalone build bundles the Python that runs the
   script): `python scripts/release.py publish vX.Y.Z`. It refuses a dirty tree, builds
   `dist/bytesprite.zip` (source) with `git archive` and `dist/bytesprite-win64.zip` (PyInstaller, from
   `bytesprite.spec` in a fresh venv with the hash-pinned `requirements-build.txt`, launched once as a
   smoke test), signs both with the key at `~/.bytesprite-signing/release_ed25519.key`, tags `HEAD`,
   pushes the branch and tag, and runs `gh release create --verify-tag` with both zips and their `.sig`
   files. A release without them cannot be installed by the updater.

The signing key is created once with `python scripts/release.py keygen`; its public half is
`RELEASE_PUBLIC_KEY` in `src/updater.py`. Never commit or upload the private key, and back it up
offline: if it is lost, installs can only move to a new key by re-running `install.ps1`.

Installed copies pick the release up through **Check for Updates**. A standalone install swaps its
whole app folder. A clone install replaces `src/` only; when `requirements.txt` changes, it asks the user
to re-run `install.ps1`. Every pin carries sha256 hashes, which pip checks automatically, so a version
bump must also update its hashes (all files PyPI lists for that version). The one-line installer
verifies the release zip against `RELEASE_PUBLIC_KEY` pinned in `install.ps1` (in PowerShell for the
standalone build; with a hash-pinned `src/signing.py` for the source fallback); if either changes,
update the pins there (`tests/test_updater.py` fails until they match).

## License

MIT, see [LICENSE](LICENSE).
