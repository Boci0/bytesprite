from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools import starter_pack
from src.tools.ai_tools import rules_path, skills_dir


def servers(config: Path) -> dict:
    return json.loads(config.read_text("utf-8")).get("mcpServers", {}) if config.exists() else {}


def test_sets_everything_up_once_and_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(starter_pack, "find_npx", lambda: "npx")
    config = tmp_path / "mcp.json"
    text, ok = starter_pack.set_up(config=config)
    assert ok and "context7" in text and "verify-before-done" in text
    assert set(servers(config)) == {"context7", "playwright"}
    assert (skills_dir() / "git-commit" / "SKILL.md").exists()
    assert "tests" in rules_path().read_text("utf-8")

    rules_path().write_text("my own rule\n", encoding="utf-8")
    assert starter_pack.set_up(config=config) == ("nothing to add, the starter pack is already in place", True)
    assert rules_path().read_text("utf-8") == "my own rule\n"


def test_missing_node_is_installed_then_servers_are_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = []
    monkeypatch.setattr(starter_pack, "find_npx", lambda: "npx" if found else None)
    config = tmp_path / "mcp.json"
    text, ok = starter_pack.set_up(install=lambda: found.append(1) or True, config=config)
    assert ok and len(found) == 1 and set(servers(config)) == {"context7", "playwright"}


def test_failed_node_install_still_keeps_skills_and_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(starter_pack, "find_npx", lambda: None)
    config = tmp_path / "mcp.json"
    text, ok = starter_pack.set_up(install=lambda: False, config=config)
    assert not ok and "nodejs.org" in text and "git-commit" in text
    assert servers(config) == {}
