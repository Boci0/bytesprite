"""GitHub release check and in-place source update for an installed ByteSprite."""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from src import __version__

REPO = "Boci0/bytesprite"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
APP_DIR = Path(__file__).resolve().parents[1]
# install.ps1 writes this marker; a git checkout has none, so the updater never overwrites dev work
INSTALL_MARKER = ".installed"
_MAX_ZIP_BYTES = 20 * 1024 * 1024
_TIMEOUT_S = 15


def parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); non-numeric parts are ignored."""
    return tuple(int(n) for n in re.findall(r"\d+", tag)[:3])


def is_newer(tag: str, current: str = __version__) -> bool:
    return parse_version(tag) > parse_version(current)


def is_installed(app_dir: Path = APP_DIR) -> bool:
    return (app_dir / INSTALL_MARKER).is_file()


def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"ByteSprite/{__version__}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download too large")
    return data


def latest_release() -> tuple[str, str]:
    """(tag, source zipball url) of the newest published GitHub release."""
    info = json.loads(_get(LATEST_API, 1024 * 1024))
    return str(info["tag_name"]), str(info["zipball_url"])


def install_source_zip(data: bytes, app_dir: Path = APP_DIR) -> None:
    """Swap `app_dir/src` for the `src/` folder inside a GitHub source zipball.

    The new tree is fully extracted next to the old one before a rename swap, so a bad zip
    leaves the running install untouched.
    """
    new_dir, old_dir = app_dir / "src.new", app_dir / "src.old"
    shutil.rmtree(new_dir, ignore_errors=True)
    shutil.rmtree(old_dir, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            # GitHub zipballs wrap everything in one '<owner>-<repo>-<sha>/' folder
            if len(parts) < 3 or parts[1] != "src" or info.is_dir():
                continue
            rel = Path(*parts[2:])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe path in update: {info.filename}")
            dest = new_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    if not (new_dir / "main.py").is_file():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise ValueError("update has no src/main.py")
    (app_dir / "src").rename(old_dir)
    new_dir.rename(app_dir / "src")
    shutil.rmtree(old_dir, ignore_errors=True)


class UpdateJob(QObject):
    """Checks GitHub (and, when `install`, applies the update) on a worker thread.

    `finished` delivers (status, tag) on the Qt main thread; status is one of
    'current', 'available', 'installed', 'error'.
    """

    finished = pyqtSignal(str, str)

    def start(self, *, install: bool) -> None:
        threading.Thread(target=self._run, args=(install,), daemon=True).start()

    def _run(self, install: bool) -> None:
        tag = ""
        try:
            tag, zip_url = latest_release()
            if not is_newer(tag):
                self.finished.emit("current", tag)
                return
            if not install or not is_installed():
                self.finished.emit("available", tag)
                return
            install_source_zip(_get(zip_url, _MAX_ZIP_BYTES))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            self.finished.emit("error", str(exc) or tag)
            return
        self.finished.emit("installed", tag)
