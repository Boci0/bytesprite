"""Unit tests for the GitHub release updater."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from src.updater import install_source_zip, is_installed, is_newer, parse_version


def _zipball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"Boci0-bytesprite-abc123/{name}", text)
    return buf.getvalue()


def _app(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("old")
    return tmp_path


def test_version_compare() -> None:
    assert parse_version("v1.10.2") == (1, 10, 2)
    assert is_newer("v0.10.0", "0.9.0")
    assert not is_newer("v0.9.0", "0.9.0")
    assert not is_newer("0.8.5", "0.9.0")


def test_install_swaps_src_only(tmp_path: Path) -> None:
    app = _app(tmp_path)
    install_source_zip(_zipball({"src/main.py": "new", "src/ui/x.py": "x", "README.md": "skip"}), app)
    assert (app / "src" / "main.py").read_text() == "new"
    assert (app / "src" / "ui" / "x.py").read_text() == "x"
    assert not (app / "README.md").exists()
    assert not (app / "src.old").exists() and not (app / "src.new").exists()


def test_bad_update_keeps_running_install(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(ValueError):
        install_source_zip(_zipball({"src/other.py": "no main"}), app)
    with pytest.raises(ValueError):
        install_source_zip(_zipball({"src/../../evil.py": "x", "src/main.py": "new"}), app)
    # On Windows 'src.new' / 'Q:evil.py' is just 'Q:evil.py': another drive's working folder
    other_drive = "Q" if not str(tmp_path).upper().startswith("Q") else "R"
    with pytest.raises(ValueError):
        install_source_zip(_zipball({f"src/{other_drive}:evil.py": "x", "src/main.py": "new"}), app)
    assert (app / "src" / "main.py").read_text() == "old"
    assert not (tmp_path.parent / "evil.py").exists()


def test_locked_new_src_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path)
    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "src.new":
            raise PermissionError("in use by antivirus")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        install_source_zip(_zipball({"src/main.py": "new"}), app)
    assert (app / "src" / "main.py").read_text() == "old"


def test_git_checkout_is_not_an_install(tmp_path: Path) -> None:
    assert not is_installed(tmp_path)
    (tmp_path / ".installed").write_text("")
    assert is_installed(tmp_path)


# --- Signed releases -------------------------------------------------------

import json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from src import signing, updater  # noqa: E402

_TEST_SECRET = bytes(range(32))
_TEST_PUBLIC = signing.public_key(_TEST_SECRET)


def test_ed25519_matches_rfc8032_vectors() -> None:
    sk = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    pk = signing.public_key(sk)
    assert pk.hex() == "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
    sig = signing.sign(sk, b"\x72")
    assert sig.hex() == ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
    assert signing.verify(pk, b"\x72", sig)
    assert not signing.verify(pk, b"\x73", sig)
    assert not signing.verify(pk, b"\x72", sig[:63] + bytes([sig[63] ^ 1]))
    assert not signing.verify(pk, b"\x72", sig[:32] + (2**253).to_bytes(32, "little"))  # s >= L
    assert not signing.verify(b"\x00" * 31, b"", sig) and not signing.verify(pk, b"", b"short")


def _bundle(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bytesprite/src/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("bytesprite/src/main.py", f"v{version}")
    return buf.getvalue()


def _sig(data: bytes, secret: bytes = _TEST_SECRET) -> bytes:
    return signing.sign(secret, data).hex().encode() + b"\n"


def test_verified_bundle_rejects_tampering_wrong_key_and_replays() -> None:
    data = _bundle("1.0.0")
    assert updater.verified_bundle(data, _sig(data), "v1.0.0", _TEST_PUBLIC, "0.9.2") == data
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data + b"x", _sig(data), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data, _sig(data, bytes(32)), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data, b"not hex", "v1.0.0", _TEST_PUBLIC, "0.9.2")
    old = _bundle("0.9.0")  # genuinely signed but older: replayed under a new tag to downgrade
    with pytest.raises(ValueError, match="version mismatch"):
        updater.verified_bundle(old, _sig(old), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="version mismatch"):
        updater.verified_bundle(old, _sig(old), "v0.9.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):  # the real key never signed test data
        updater.verified_bundle(data, _sig(data), "v1.0.0", None, "0.9.2")


class _FakeGitHub(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self) -> None:  # noqa: N802
        body = self.files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.mark.parametrize("case", ["signed", "unsigned", "tampered"])
def test_update_job_installs_only_signed_releases(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    data = _bundle("99.0.0")
    assets = [] if case == "unsigned" else [
        {"name": "bytesprite.zip", "browser_download_url": f"{base}/zip"},
        {"name": "bytesprite.zip.sig", "browser_download_url": f"{base}/sig"},
    ]
    _FakeGitHub.files = {
        "/latest": json.dumps({"tag_name": "v99.0.0", "zipball_url": f"{base}/zip", "assets": assets}).encode(),
        "/zip": data + (b"evil" if case == "tampered" else b""),
        "/sig": _sig(data),
    }
    app = _app(tmp_path)
    (app / ".installed").write_text("")
    monkeypatch.setattr(updater, "LATEST_API", f"{base}/latest")
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", _TEST_PUBLIC)
    monkeypatch.setattr(updater, "APP_DIR", app)
    monkeypatch.setattr(updater, "is_installed", lambda: True)
    real_install = updater.install_source_zip
    monkeypatch.setattr(updater, "install_source_zip", lambda d: real_install(d, app))
    results: list[tuple[str, str]] = []
    job = updater.UpdateJob()
    job.finished.connect(lambda status, tag: results.append((status, tag)))
    try:
        job._run(install=True)
    finally:
        server.shutdown()
    expected = {"signed": ("installed", "v99.0.0"), "unsigned": ("error", "unsigned release"),
                "tampered": ("error", "bad signature")}[case]
    assert results == [expected]
    assert (app / "src" / "main.py").read_text() == ("v99.0.0" if case == "signed" else "old")


def test_update_job_reports_a_broken_http_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """http.client.HTTPException killed the worker before `finished`: CHECKING, then silence."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        conn, _addr = server.accept()
        conn.recv(65536)
        conn.sendall(b"NOT-HTTP\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(updater, "LATEST_API", f"http://127.0.0.1:{server.getsockname()[1]}/latest")
    results: list[tuple[str, str]] = []
    job = updater.UpdateJob()
    job.finished.connect(lambda status, tag: results.append((status, tag)))
    try:
        job._run(install=True)
    finally:
        server.close()
    assert [status for status, _tag in results] == ["error"]


def test_install_ps1_pins_release_key_and_verifier() -> None:
    import hashlib
    import re

    from src.updater import RELEASE_PUBLIC_KEY

    root = Path(__file__).resolve().parents[1]
    script = (root / "install.ps1").read_text(encoding="utf-8")
    key = re.search(r'\$ReleasePublicKey = "([0-9a-f]+)"', script).group(1)
    pinned = re.search(r'\$SigningPySha256 = "([0-9A-F]+)"', script).group(1)
    # raw.githubusercontent serves the LF bytes git stores, not a CRLF working copy
    served = (root / "src" / "signing.py").read_bytes().replace(b"\r\n", b"\n")
    assert bytes.fromhex(key) == RELEASE_PUBLIC_KEY
    assert hashlib.sha256(served).hexdigest().upper() == pinned


def test_changed_library_pins_need_the_installer(tmp_path: Path) -> None:
    app = _app(tmp_path)
    (app / "requirements.txt").write_text("PyQt6==6.12.0 \\n    --hash=sha256:aa\nPyQt6_sip==13.13.0\n")
    same_new_hashes = "pyqt6==6.12.0 \\n    --hash=sha256:bb\nPyQt6-sip==13.13.0\n"  # case, _ vs - and hashes do not count
    install_source_zip(_zipball({"src/main.py": "new", "requirements.txt": same_new_hashes}), app)
    assert (app / "src" / "main.py").read_text() == "new" and "bb" in (app / "requirements.txt").read_text()
    with pytest.raises(ValueError, match="needs installer"):
        install_source_zip(_zipball({"src/main.py": "newer", "requirements.txt": "PyQt6==6.13.0\n"}), app)
    assert (app / "src" / "main.py").read_text() == "new" and not (app / "src.new").exists()
    install_source_zip(_zipball({"src/main.py": "no reqs file in bundle"}), app)  # nothing to compare: allowed
