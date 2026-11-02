"""GitHub release check and in-place source update for an installed ByteSprite.

Updates must be signed: a release carries `bytesprite.zip` plus `bytesprite.zip.sig`, an Ed25519
signature made with the maintainer's offline key (scripts/release.py). HTTPS only proves the file
came from GitHub; the signature proves it came from the key holder, so a hijacked GitHub account
or an intercepted download cannot ship code. The version inside the zip must match the tag and be
newer, so an old signed release cannot be replayed under a new tag to downgrade an install.
"""

from __future__ import annotations

import http.client
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
from src.signing import verify

REPO = "Boci0/bytesprite"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
APP_DIR = Path(__file__).resolve().parents[1]
# install.ps1 writes this marker; a git checkout has none, so the updater never overwrites dev work
INSTALL_MARKER = ".installed"
_MAX_ZIP_BYTES = 20 * 1024 * 1024
ASSET_NAME = "bytesprite.zip"
SIG_NAME = ASSET_NAME + ".sig"
# Public half of the release signing key; the private half never leaves the maintainer's PC
RELEASE_PUBLIC_KEY = bytes.fromhex("f0cc212a80d53fe877827253c020e2c01d023c4280eebf47423c6032a0259316")
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


def latest_release() -> tuple[str, str, str]:
    """(tag, zip url, signature url) of the newest release; urls are '' when it is not signed."""
    info = json.loads(_get(LATEST_API, 1024 * 1024))
    assets = {a.get("name"): str(a.get("browser_download_url") or "") for a in info.get("assets") or []
              if isinstance(a, dict)}
    return str(info["tag_name"]), assets.get(ASSET_NAME, ""), assets.get(SIG_NAME, "")


def zip_version(data: bytes) -> str:
    """`__version__` declared in the bundle's src/__init__.py ('' if missing)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            parts = Path(name).parts
            if len(parts) == 3 and parts[1:] == ("src", "__init__.py"):
                match = re.search(r"__version__\s*=\s*[\"']([^\"']+)", zf.read(name).decode("utf-8", "replace"))
                return match.group(1) if match else ""
    return ""


def verified_bundle(data: bytes, sig_text: bytes, tag: str, public: bytes | None = None,
                    current: str = __version__) -> bytes:
    """Return `data` only if it is signed by the release key and really is the newer `tag`; else raise."""
    try:
        signature = bytes.fromhex(sig_text.decode("ascii").strip())
    except ValueError:
        raise ValueError("bad signature") from None
    if not verify(public or RELEASE_PUBLIC_KEY, data, signature):
        raise ValueError("bad signature")
    version = zip_version(data)
    if not version or parse_version(version) != parse_version(tag) or not is_newer(version, current):
        raise ValueError("version mismatch")
    return data


def _bundle_requirements(data: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if len(Path(name).parts) == 2 and Path(name).name == "requirements.txt":
                return zf.read(name).decode("utf-8", "replace")
    return None


def _pins(text: str) -> set[str]:
    """The `name==version` pins of a requirements file (hash lines and comments ignored, names case-folded)."""
    found = (re.match(r"\s*([A-Za-z0-9_.\-\[\]]+==[^\s\;]+)", line) for line in text.splitlines())
    return {m[1].lower().replace("_", "-") for m in found if m}


def install_source_zip(data: bytes, app_dir: Path = APP_DIR) -> None:
    """Swap `app_dir/src` for the `src/` folder inside a verified release bundle.

    The new tree is fully extracted next to the old one before a rename swap, so a bad zip
    leaves the running install untouched. A bundle whose pinned libraries differ from the installed
    ones is refused ("needs installer"): new code on old libraries can crash at start, and the running
    app cannot safely replace its own libraries, so the user re-runs install.ps1 for that release.
    """
    new_dir, old_dir = app_dir / "src.new", app_dir / "src.old"
    shutil.rmtree(new_dir, ignore_errors=True)
    shutil.rmtree(old_dir, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            # Bundles wrap everything in one top folder ('bytesprite/', or '<owner>-<repo>-<sha>/')
            if len(parts) < 3 or parts[1] != "src" or info.is_dir():
                continue
            rel = Path(*parts[2:])
            if rel.is_absolute() or rel.drive or ".." in rel.parts:  # 'D:x' is drive-relative, not absolute
                raise ValueError(f"unsafe path in update: {info.filename}")
            dest = new_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    if not (new_dir / "main.py").is_file():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise ValueError("update has no src/main.py")
    wanted = _bundle_requirements(data)
    have = app_dir / "requirements.txt"
    if wanted is not None and have.is_file() and _pins(wanted) != _pins(have.read_text(encoding="utf-8", errors="replace")):
        shutil.rmtree(new_dir, ignore_errors=True)
        raise ValueError("needs installer")
    (app_dir / "src").rename(old_dir)
    try:
        new_dir.rename(app_dir / "src")
    except OSError:
        old_dir.rename(app_dir / "src")  # e.g. antivirus still scanning src.new: keep the old code, not none
        raise
    shutil.rmtree(old_dir, ignore_errors=True)
    if wanted is not None:
        have.write_text(wanted, encoding="utf-8", newline="")  # same pins, maybe new hashes: keep the file current


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
            tag, zip_url, sig_url = latest_release()
            if not is_newer(tag):
                self.finished.emit("current", tag)
                return
            if not install or not is_installed():
                self.finished.emit("available", tag)
                return
            if not zip_url or not sig_url:
                raise ValueError("unsigned release")
            install_source_zip(verified_bundle(_get(zip_url, _MAX_ZIP_BYTES), _get(sig_url, 1024), tag))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, http.client.HTTPException) as exc:
            self.finished.emit("error", str(exc) or tag)
            return
        self.finished.emit("installed", tag)
