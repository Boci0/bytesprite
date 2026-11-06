"""Build, sign and publish a Paperduck release.

    python scripts/release.py keygen            create the signing key (once) and print its public key
    python scripts/release.py build vX.Y.Z      from a clean git HEAD: dist/paperduck.zip (source) and
                                                dist/paperduck-win64.zip (PyInstaller build, launched once
                                                as a smoke test), each with a .sig
    python scripts/release.py publish vX.Y.Z    build, tag HEAD, push branch + tag, then `gh release create`

The standalone build bundles the Python that runs this script, so run it with Python 3.12 (the app's runtime).

The private key lives at ~/.paperduck-signing/release_ed25519.key, outside the repo, and must
never be committed or uploaded. Back it up offline: losing it means installed copies can only be
updated by re-running install.ps1 after a new key is shipped.
"""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.signing import public_key, sign, verify  # noqa: E402
from src.updater import FROZEN_ASSET, FROZEN_EXE, RELEASE_PUBLIC_KEY, SOURCE_ASSET, parse_version, zip_version  # noqa: E402

KEY_FILE = Path.home() / ".paperduck-signing" / "release_ed25519.key"
DIST = ROOT / "dist"
BUNDLE_PATHS = ["src", "assets", "requirements.txt", "install.ps1", "uninstall.ps1", "README.md", "LICENSE"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def load_secret() -> bytes:
    if not KEY_FILE.is_file():
        sys.exit(f"No signing key at {KEY_FILE}; run: python scripts/release.py keygen")
    secret = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
    if public_key(secret) != RELEASE_PUBLIC_KEY:
        sys.exit("Signing key does not match RELEASE_PUBLIC_KEY in src/updater.py")
    return secret


def keygen() -> None:
    if KEY_FILE.exists():
        sys.exit(f"{KEY_FILE} already exists; refusing to overwrite a release key")
    import secrets

    secret = secrets.token_bytes(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(secret.hex() + "\n", encoding="ascii")
    print(f"Wrote {KEY_FILE} (back it up offline, never commit it)")
    print(f"Public key for RELEASE_PUBLIC_KEY in src/updater.py:\n{public_key(secret).hex()}")


def sign_bundle(secret: bytes, zip_path: Path, tag: str) -> Path:
    data = zip_path.read_bytes()
    if parse_version(zip_version(data)) != parse_version(tag):
        sys.exit(f"{zip_path.name} declares version {zip_version(data)!r}, not {tag}")
    signature = sign(secret, data)
    if not verify(RELEASE_PUBLIC_KEY, data, signature):
        sys.exit("Signature self-check failed")
    sig_path = zip_path.with_name(zip_path.name + ".sig")
    sig_path.write_text(signature.hex() + "\n", encoding="ascii")
    print(f"Built {zip_path} ({len(data)} bytes) and {sig_path}")
    return sig_path


def _shows_window(pid: int) -> bool:
    user32, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def check(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(check, 0)
    return bool(found)


def smoke_test(exe: Path, work: Path) -> None:
    """Launch the standalone app on a throwaway state file: it must show the pet and close cleanly."""
    proc = subprocess.Popen([str(exe), "--state-file", str(work / "smoke_state.json")], cwd=work)
    deadline = time.monotonic() + 30
    while proc.poll() is None and not _shows_window(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    shown = proc.poll() is None and _shows_window(proc.pid)
    time.sleep(3)  # let sound and hotkey setup run: a crash there must stop the release too
    subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True)  # graceful: WM_CLOSE -> quit()
    try:
        code = proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = None
    if not shown or code != 0:
        sys.exit(f"Smoke test failed: window shown={shown}, exit code={code}")
    print("Smoke test passed: pet shown, clean exit")


def build_frozen(tag: str, secret: bytes) -> tuple[Path, Path]:
    """PyInstaller build of HEAD in a fresh hash-checked venv, zipped as Paperduck/ plus a VERSION file."""
    with tempfile.TemporaryDirectory(prefix="paperduck_build_") as tmp:
        work = Path(tmp)
        tree = work / "tree"
        git("archive", "--format=zip", "-o", str(work / "head.zip"), "HEAD")  # committed files only
        with zipfile.ZipFile(work / "head.zip") as zf:
            zf.extractall(tree)
        venv_py = work / "venv" / "Scripts" / "python.exe"
        subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True)
        subprocess.run([str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "-r", str(tree / "requirements-build.txt")], check=True)
        dist = work / "dist"
        subprocess.run([str(venv_py), "-m", "PyInstaller", "paperduck.spec", "--noconfirm",
                        "--distpath", str(dist), "--workpath", str(work / "build")], cwd=tree, check=True)
        app = dist / "Paperduck"
        (app / "VERSION").write_text(tag.lstrip("v") + "\n", encoding="ascii")
        shutil.copy2(tree / "uninstall.ps1", app)  # an update replaces the whole folder, so it ships inside
        smoke_test(app / FROZEN_EXE, work)
        zip_path = DIST / FROZEN_ASSET
        zip_path.unlink(missing_ok=True)
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", dist, "Paperduck")
    return zip_path, sign_bundle(secret, zip_path, tag)


def build(tag: str) -> list[Path]:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        sys.exit("Tag must look like v1.2.3")
    if git("status", "--porcelain").strip():
        sys.exit("Working tree is not clean; commit first so the release matches a commit")
    secret = load_secret()
    DIST.mkdir(exist_ok=True)
    zip_path = DIST / SOURCE_ASSET
    git("archive", "--format=zip", "--prefix=paperduck/", "-o", str(zip_path), "HEAD", *BUNDLE_PATHS)
    sig_path = sign_bundle(secret, zip_path, tag)
    print(f"Building the standalone app with Python {sys.version.split()[0]}...")
    return [zip_path, sig_path, *build_frozen(tag, secret)]


def publish(tag: str) -> None:
    assets = build(tag)
    head = git("rev-parse", "HEAD").strip()
    tagged = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if tagged and tagged != head:
        sys.exit(f"{tag} already points at {tagged[:7]}, not HEAD {head[:7]}")
    if not tagged:
        git("tag", tag)
    # Push first: without a pushed tag, `gh release create` tags whatever the remote branch holds (v0.9.6 bug)
    pushed = subprocess.run(["git", "push", "--atomic", "origin", "HEAD", f"refs/tags/{tag}"],
                            cwd=ROOT, capture_output=True, text=True)
    if pushed.returncode != 0:
        sys.exit(f"git push failed, nothing published:\n{pushed.stderr}")
    subprocess.run(["gh", "release", "create", tag, "--verify-tag", "--generate-notes", *map(str, assets)],
                   cwd=ROOT, check=True)


if __name__ == "__main__":
    commands = {"keygen": lambda _a: keygen(), "build": lambda a: build(a[0]), "publish": lambda a: publish(a[0])}
    if len(sys.argv) < 2 or sys.argv[1] not in commands or (sys.argv[1] != "keygen" and len(sys.argv) != 3):
        sys.exit(__doc__)
    commands[sys.argv[1]](sys.argv[2:])
