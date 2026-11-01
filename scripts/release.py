"""Build, sign and publish a ByteSprite release.

    python scripts/release.py keygen            create the signing key (once) and print its public key
    python scripts/release.py build vX.Y.Z      dist/bytesprite.zip + .sig from a clean git HEAD
    python scripts/release.py publish vX.Y.Z    build, tag HEAD, push branch + tag, then `gh release create`

The private key lives at ~/.bytesprite-signing/release_ed25519.key, outside the repo, and must
never be committed or uploaded. Back it up offline: losing it means installed copies can only be
updated by re-running install.ps1 after a new key is shipped.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.signing import public_key, sign, verify  # noqa: E402
from src.updater import ASSET_NAME, RELEASE_PUBLIC_KEY, SIG_NAME, parse_version, zip_version  # noqa: E402

KEY_FILE = Path.home() / ".bytesprite-signing" / "release_ed25519.key"
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


def build(tag: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        sys.exit("Tag must look like v1.2.3")
    if git("status", "--porcelain").strip():
        sys.exit("Working tree is not clean; commit first so the release matches a commit")
    secret = load_secret()
    DIST.mkdir(exist_ok=True)
    zip_path, sig_path = DIST / ASSET_NAME, DIST / SIG_NAME
    git("archive", "--format=zip", "--prefix=bytesprite/", "-o", str(zip_path), "HEAD", *BUNDLE_PATHS)
    data = zip_path.read_bytes()
    if parse_version(zip_version(data)) != parse_version(tag):
        sys.exit(f"src/__init__.__version__ is {zip_version(data)!r}, not {tag}")
    signature = sign(secret, data)
    if not verify(RELEASE_PUBLIC_KEY, data, signature):
        sys.exit("Signature self-check failed")
    sig_path.write_text(signature.hex() + "\n", encoding="ascii")
    print(f"Built {zip_path} ({len(data)} bytes) and {sig_path}")
    return zip_path, sig_path


def publish(tag: str) -> None:
    zip_path, sig_path = build(tag)
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
    subprocess.run(["gh", "release", "create", tag, "--verify-tag", "--generate-notes", str(zip_path), str(sig_path)],
                   cwd=ROOT, check=True)


if __name__ == "__main__":
    commands = {"keygen": lambda _a: keygen(), "build": lambda a: build(a[0]), "publish": lambda a: publish(a[0])}
    if len(sys.argv) < 2 or sys.argv[1] not in commands or (sys.argv[1] != "keygen" and len(sys.argv) != 3):
        sys.exit(__doc__)
    commands[sys.argv[1]](sys.argv[2:])
