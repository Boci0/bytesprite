"""PC tools the AI chat model may call: read-only file access inside the Project Folder plus a few pet actions.

File tools are plain Python and run on the chat's worker thread. Pet tools need the Qt thread and are run
by src/ui/ai_tool_host.py. Nothing here deletes files or runs commands; save_file only writes where the user
picks in a Save dialog.

Security:
- Paths resolve inside the Project Folder only (`..`, absolute paths and links out of it are refused).
- Likely secret files (.env, keys, certificates, credential stores) are never read.
- save_file takes a bare file name (no folders) and at most MAX_SAVE_CHARS of text.
- Pet actions are a fixed allowlist: nothing that deletes keys, installs updates, edits macros or opens dialogs.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

MAX_FILE_CHARS = 12000  # one read_file call; the model pages on with start_line
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LISTED = 200
MAX_FOUND = 100
MAX_MATCHES = 60
MAX_SAVE_CHARS = MAX_FILE_BYTES  # one save_file call
MAX_SCANNED = 20000  # entries one find or search walks before giving up
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build"}
_SECRET = re.compile(r"^\.env|\.(pem|key|pfx|p12|kdbx|ppk)$|^id_(rsa|dsa|ecdsa|ed25519)|credential|secret",
                     re.I)

# Allowlisted pet actions -> what they do (the model sees this list)
PET_ACTIONS: dict[str, str] = {
    "pomodoro:toggle": "start or pause the focus timer",
    "pomodoro:break": "start a short break",
    "pomodoro:reset": "reset the focus timer",
    "hints:toggle": "turn smart suggestions on or off",
    "lyrics:toggle": "turn song lyrics on or off",
    "pet:mute": "mute or unmute the system volume",
    "project:code": "open the Project Folder in VS Code",
    "project:terminal": "open a terminal in the Project Folder",
    "project:explorer": "open the Project Folder in Explorer",
    "project:refresh": "refresh the Project Folder's git status",
    "sysmon:refresh": "refresh CPU and RAM readings",
}
# Tools whose output is the user's own data; after one runs, fetch_page may only open links already in the chat
LOCAL_DATA_TOOLS = {"list_files", "read_file", "find_files", "search_text", "read_clipboard", "read_screen", "pet_status"}


def _fn(name: str, description: str, props: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


_PATH = {"type": "string", "description": "Path relative to the Project Folder; empty for the folder itself"}
TOOL_SPECS: list[dict[str, Any]] = [
    _fn("list_files", "List a folder inside the user's Project Folder (folders end with /).", {"path": _PATH}),
    _fn("read_file", "Read a text file inside the Project Folder, with line numbers.",
        {"path": _PATH, "start_line": {"type": "integer", "description": "First line to show, default 1"}}, ("path",)),
    _fn("find_files", "Find files in the Project Folder by name pattern, e.g. *.py or *config*.",
        {"pattern": {"type": "string"}}, ("pattern",)),
    _fn("search_text", "Find lines containing some text in the Project Folder's files (case-insensitive).",
        {"text": {"type": "string"}, "pattern": {"type": "string", "description": "Optional file name pattern"}},
        ("text",)),
    _fn("read_clipboard", "Read the text currently on the user's clipboard."),
    _fn("copy_text", "Put text on the user's clipboard.", {"text": {"type": "string"}}, ("text",)),
    _fn("read_screen", "Read the visible text on the user's screen (OCR, the chat is hidden for the grab)."),
    _fn("pet_status", "The pet's tool readouts: CPU/RAM, focus timer, Project Folder and git status, clipboard vault."),
    _fn("pet_action", "Run one ByteSprite action.",
        {"action": {"type": "string", "enum": list(PET_ACTIONS),
                    "description": "; ".join(f"{k}: {v}" for k, v in PET_ACTIONS.items())}}, ("action",)),
    _fn("run_macro", "Run one of the user's saved ByteSprite macros by name.", {"name": {"type": "string"}}, ("name",)),
    _fn("save_file", "Give the user a text file (code, CSV, Markdown, JSON...). They choose where to save it in a "
        "Save dialog, or cancel.", {"name": {"type": "string", "description": "Suggested file name, e.g. report.md"},
                                    "content": {"type": "string", "description": "The whole file's text"}},
        ("name", "content")),
    _fn("open_link", "Open an https link in the user's browser.", {"url": {"type": "string"}}, ("url",)),
]
FILE_TOOLS = {"list_files", "read_file", "find_files", "search_text"}
NO_FOLDER = ("error: no Project Folder chosen. Ask the user to pick one: right-click the pet, Project, "
             "Choose Project Folder.")


def save_args(args: dict[str, Any]) -> tuple[str, str]:
    """save_file's (file name, content), or ValueError. Characters Windows refuses in names become _."""
    name, content = args.get("name"), args.get("content")
    if not isinstance(name, str) or not name.strip() or not isinstance(content, str):
        raise ValueError("give a file name and its content")
    if "/" in name or "\\" in name or name.strip() in (".", ".."):
        raise ValueError("give a bare file name, without folders")
    if len(content) > MAX_SAVE_CHARS:
        raise ValueError(f"content is longer than {MAX_SAVE_CHARS} characters")
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", name.strip()).rstrip(". ") or "file.txt", content


def resolve(root: Path | None, rel: Any) -> Path:
    """`rel` inside `root`, or ValueError. Symlinks are resolved first, so a link out of the folder is refused."""
    if root is None:
        raise ValueError(NO_FOLDER.removeprefix("error: "))
    base = root.resolve()
    rel = rel.strip() if isinstance(rel, str) else ""
    target = (base / rel).resolve() if rel else base
    if not target.is_relative_to(base):
        raise ValueError("that path is outside the Project Folder")
    return target


def _rel(base: Path, path: Path) -> str:
    return path.relative_to(base).as_posix() or "."


def _walk(base: Path):
    """Files under base, skipping build and VCS folders, stopping after MAX_SCANNED entries."""
    seen = 0
    for top, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            seen += 1
            if seen > MAX_SCANNED:
                return
            yield Path(top) / name


def _text(path: Path) -> str | None:
    """File text, or None when it is binary or too big."""
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    raw = path.read_bytes()
    return None if b"\0" in raw[:8192] else raw.decode("utf-8", "replace")


def run_file_tool(root: Path | None, name: str, args: dict[str, Any]) -> str:
    """One file tool's output for the model; every failure is a short `error: ...` line, never raised."""
    try:
        base = resolve(root, "")
        if name == "list_files":
            folder = resolve(root, args.get("path"))
            if not folder.is_dir():
                return "error: not a folder"
            rows = [f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)"
                    for p in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:MAX_LISTED]]
            return f"{_rel(base, folder)}:\n" + ("\n".join(rows) or "(empty)")
        if name == "read_file":
            path = resolve(root, args.get("path"))
            if not path.is_file():
                return "error: not a file"
            if _SECRET.search(path.name):
                return "error: that looks like a secrets file, so it is not read"
            text = _text(path)
            if text is None:
                return "error: binary or larger than 2 MB"
            start = args.get("start_line")
            start = start if isinstance(start, int) and start > 0 else 1
            out, size = [], 0
            lines = text.splitlines()
            for n, line in enumerate(lines[start - 1:], start):
                row = f"{n}: {line}"
                if size + len(row) > MAX_FILE_CHARS:
                    out.append(f"... more from line {n} (of {len(lines)}); call again with start_line={n}")
                    break
                out.append(row)
                size += len(row) + 1
            return "\n".join(out) or "(empty file)"
        pattern = args.get("pattern") if isinstance(args.get("pattern"), str) and args["pattern"].strip() else "*"
        if name == "find_files":
            found = [_rel(base, p) for p in _walk(base) if fnmatch.fnmatch(p.name.lower(), pattern.strip().lower())]
            return "\n".join(found[:MAX_FOUND]) + (f"\n... {len(found) - MAX_FOUND} more" if len(found) > MAX_FOUND
                                                   else "") if found else "no files match"
        if name == "search_text":
            needle = args.get("text")
            if not isinstance(needle, str) or not needle.strip():
                return "error: give some text to search for"
            needle = needle.strip().lower()
            hits: list[str] = []
            for path in _walk(base):
                if _SECRET.search(path.name) or not fnmatch.fnmatch(path.name.lower(), pattern.strip().lower()):
                    continue
                text = _text(path)
                for n, line in enumerate((text or "").splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{_rel(base, path)}:{n}: {line.strip()[:200]}")
                        if len(hits) >= MAX_MATCHES:
                            return "\n".join(hits) + "\n... more matches, narrow the search"
            return "\n".join(hits) or "no matches"
    except ValueError as exc:
        return f"error: {exc}"
    except OSError as exc:
        return f"error: {exc.strerror or 'could not read'}"
    return "error: unknown tool"
