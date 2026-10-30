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
    assert (app / "src" / "main.py").read_text() == "old"
    assert not (tmp_path.parent / "evil.py").exists()


def test_git_checkout_is_not_an_install(tmp_path: Path) -> None:
    assert not is_installed(tmp_path)
    (tmp_path / ".installed").write_text("")
    assert is_installed(tmp_path)
