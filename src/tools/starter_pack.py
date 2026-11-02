"""One-click starter pack: Node.js if missing, two MCP servers, two skills and a first set of global rules.

Everything is additive: an existing skill, MCP server or non-empty rules file is left alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from src.tools import mcp_client
from src.tools.ai_tools import RULES_HEADER, rules_path, rules_prompt, skill_text, skills_dir, write_skill

NODE_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
WINGET_NODE = ["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
               "--accept-package-agreements", "--accept-source-agreements"]

MCP_SERVERS = {
    "context7": ["-y", "@upstash/context7-mcp"],  # current library docs
    "playwright": ["-y", "@playwright/mcp@latest"],  # real browser through the page structure
}

SKILLS = {
    "verify-before-done": (
        "Before saying a change is done, check it actually works",
        "After editing code, run the project's tests or the exact command that exercises the change, and read the "
        "output. Report what you ran and what it showed. If you could not run anything, say so instead of claiming "
        "success."),
    "git-commit": (
        "Write a clear commit message and commit only what the user asked for",
        "Run git status and git diff first. Stage only the files that belong to the change. Write a one-line "
        "summary in the imperative mood (under 72 characters), then a short body saying why. Never commit secrets, "
        "never push, and never rewrite history unless the user asks."),
}

RULES = (
    "Run the tests or the relevant command after every code change and say what you verified.\n"
    "Ask before anything destructive: deleting files, force pushes, dropping data.\n"
    "Keep answers short and say plainly when something failed or was not checked.\n")


def find_npx() -> str | None:
    """npx on PATH, or in Node's default folder (a fresh winget install is not on this process's PATH yet)."""
    return shutil.which("npx") or (str(NODE_DIR / "npx.cmd") if (NODE_DIR / "npx.cmd").exists() else None)


def install_node(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """Install Node.js LTS with winget; True when npx can be found afterwards."""
    if not shutil.which("winget"):
        return False
    try:
        run(WINGET_NODE, timeout=900, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    if NODE_DIR.is_dir() and str(NODE_DIR) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}{os.pathsep}{NODE_DIR}"
    return find_npx() is not None


def set_up(install: Callable[[], bool] = install_node, config: Path | None = None) -> tuple[str, bool]:
    """Apply the pack. Returns (what happened, True when everything is in place)."""
    done = []
    for name, (description, instructions) in SKILLS.items():
        if write_skill(skills_dir(), name, skill_text(description, instructions)).startswith("added"):
            done.append(name)
    if not rules_prompt(rules_path()):
        rules_path().parent.mkdir(parents=True, exist_ok=True)
        rules_path().write_text(RULES_HEADER + RULES, encoding="utf-8")
        done.append("global rules")
    if not find_npx() and not install():
        return ("Node.js is missing and could not be installed; install it from nodejs.org, then run this again"
                + (f" (set up: {', '.join(done)})" if done else ""), False)
    for name, args in MCP_SERVERS.items():
        if mcp_client.add_server(name, "npx", args, {}, config or mcp_client.CONFIG_PATH).startswith("added"):
            done.append(name)
    return ("set up: " + ", ".join(done) if done else "nothing to add, the starter pack is already in place", True)
