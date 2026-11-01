"""PC tools the AI chat model may call: file access inside the Project Folder plus a few pet actions.

File tools are plain Python and run on the chat's worker thread. Pet tools need the Qt thread and are run
by src/ui/ai_tool_host.py. Nothing here deletes files; save_file only writes where the user picks in a Save
dialog, and edit_file and run_command only act after the user allows them in the chat.

Security:
- Paths resolve inside the Project Folder only (`..`, absolute paths and links out of it are refused).
- Likely secret files (.env, keys, certificates, credential stores) are never read or edited, nor is .git.
- save_file takes a bare file name (no folders) and at most MAX_SAVE_CHARS of text.
- edit_file shows its diff first and refuses to write if the file changed while the user was deciding.
- run_command runs in the Project Folder after the user allows the exact command; it is killed (with its
  child processes) after COMMAND_TIMEOUT_S.
- Pet actions are a fixed allowlist: nothing that deletes keys, installs updates, edits macros or opens dialogs.
- Memory notes and skills become instructions for later chats, so remember, forget, add_skill and
  update_skill show the exact text (or diff) and write only after the user allows it.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE_CHARS = 30000  # one read_file call; the model pages on with start_line
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LISTED = 200
MAX_FOUND = 100
MAX_MATCHES = 60
MAX_SAVE_CHARS = MAX_FILE_BYTES  # one save_file call
MAX_SCANNED = 20000  # entries one find or search walks before giving up
MAX_DIFF_CHARS = 4000  # diff shown in the chat for the user to approve
MAX_NOTE_CHARS = 300  # one memory note
COMMAND_TIMEOUT_S = 120
MAX_COMMAND_CHARS = 30000  # command output returned to the model (head and tail kept)
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
LOCAL_DATA_TOOLS = {"list_files", "read_file", "find_files", "search_text", "read_clipboard", "read_screen", "pet_status",
                    "run_command"}


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
    _fn("load_skill", "Load one of the user's skills (listed in the system prompt) and follow its instructions.",
        {"name": {"type": "string"}}, ("name",)),
    _fn("edit_file", "Change a text file in the Project Folder by replacing old_text, which must appear exactly once "
        "(copy it from read_file without the line numbers, with enough lines around it to be unique). An empty "
        "old_text creates a new file. The user sees the diff and allows or declines it.",
        {"path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ("path", "old_text", "new_text")),
    _fn("run_command", f"Run a Windows cmd.exe command in the Project Folder, or in the user's home folder when none "
        f"is chosen (e.g. pytest -q, git status, npm test, winget search) "
        f"and get its exit code and output. The user allows each command first; it is stopped after "
        f"{COMMAND_TIMEOUT_S} s, so do not start servers or watchers.", {"command": {"type": "string"}}, ("command",)),
    _fn("find_mcp_servers", "Search the official MCP registry for local MCP servers that would give you a new "
        "ability (e.g. github, sqlite, browser). Gives each server's command, args and env vars for "
        "add_mcp_server.", {"query": {"type": "string"}}, ("query",)),
    _fn("add_mcp_server", "Install an MCP server for the user: adds it to their MCP config so its tools join "
        "this chat. Use a command and args from find_mcp_servers. The user reads the exact command and allows "
        "or declines it. Ask the user for any secret env values; never invent them.",
        {"name": {"type": "string", "description": "Short name, letters, digits, _ and - only"},
         "command": {"type": "string", "description": "e.g. npx or uvx"},
         "args": {"type": "array", "items": {"type": "string"}},
         "env": {"type": "object", "additionalProperties": {"type": "string"}}},
        ("name", "command", "args")),
    _fn("add_skill", "Save a reusable skill (instructions for a kind of task) to the user's skills, so later "
        "chats can load_skill it. The user reads it and allows or declines it.",
        {"name": {"type": "string", "description": "Folder name, letters, digits, _ . and - only"},
         "description": {"type": "string", "description": "One line: when to use this skill"},
         "instructions": {"type": "string", "description": "The skill's Markdown body"}},
        ("name", "description", "instructions")),
    _fn("update_skill", "Improve one of the user's skills after it proved wrong, unclear or missing a step. Give "
        "the full new instructions (load_skill it first). The user sees the diff and allows or declines it.",
        {"name": {"type": "string"}, "instructions": {"type": "string", "description": "The whole new body"},
         "description": {"type": "string", "description": "Optional new one-line description"}},
        ("name", "instructions")),
    _fn("remember", "Save one short lasting note to your memory, which every later chat sees: a correction, a "
        "preference, or a fact about the user's setup (e.g. 'Tests run with .venv\\Scripts\\python -m pytest'). "
        "Not for one-off details. The user allows or declines it.",
        {"note": {"type": "string", "description": f"One sentence, under {MAX_NOTE_CHARS} characters"}},
        ("note",)),
    _fn("forget", "Remove a note from your memory that is wrong or stale. The user allows or declines it.",
        {"note": {"type": "string", "description": "Text from the note to remove"}}, ("note",)),
]
# the user allows each call in the chat (or all of them for a chat)
APPROVED_TOOLS = {"edit_file", "run_command", "add_mcp_server", "add_skill", "update_skill", "remember", "forget"}
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


# ---------------------------------------------------------------------- edits and commands
@dataclass
class Edit:
    path: Path
    rel: str
    before: str | None  # None: a new file
    after: str
    crlf: bool
    diff: str


def plan_edit(root: Path | None, args: dict[str, Any]) -> Edit:
    """The edit edit_file asks for, with its diff, or ValueError. Nothing is written here."""
    rel_arg, old, new = args.get("path"), args.get("old_text"), args.get("new_text")
    if not isinstance(rel_arg, str) or not rel_arg.strip() or not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("give path, old_text and new_text")
    path = resolve(root, rel_arg)
    base = resolve(root, "")
    rel = _rel(base, path)
    if _SECRET.search(path.name) or ".git" in path.relative_to(base).parts:
        raise ValueError("that file is not edited (secrets or .git)")
    if len(new) > MAX_SAVE_CHARS:
        raise ValueError(f"new_text is longer than {MAX_SAVE_CHARS} characters")
    if not old:
        if path.exists():
            raise ValueError("that file exists; give old_text to change it")
        before, after, crlf = None, new, False
    else:
        if not path.is_file():
            raise ValueError("not a file; use an empty old_text to create it")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("larger than 2 MB")
        try:
            before = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("not a UTF-8 text file") from None
        crlf = "\r\n" in before
        text = before.replace("\r\n", "\n") if crlf else before
        old = old.replace("\r\n", "\n")
        count = text.count(old)
        if count != 1:
            raise ValueError("old_text was not found; read the file again" if not count else
                             f"old_text appears {count} times; include more surrounding lines")
        after = text.replace(old, new.replace("\r\n", "\n"), 1)
    diff = "".join(difflib.unified_diff(
        (before or "").replace("\r\n", "\n").splitlines(keepends=True), after.splitlines(keepends=True),
        f"a/{rel}", f"b/{rel}", n=2))
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n... diff cut, {len(diff) - MAX_DIFF_CHARS} more characters"
    return Edit(path, rel, before, after, crlf, diff or "(no change)")


def apply_edit(edit: Edit) -> str:
    """Write an allowed edit, unless the file changed since it was planned."""
    try:
        now = edit.path.read_bytes().decode("utf-8") if edit.path.exists() else None
        if now != edit.before:
            return "error: the file changed while the user was deciding; read it again"
        edit.path.parent.mkdir(parents=True, exist_ok=True)
        edit.path.write_text(edit.after, encoding="utf-8", newline="\r\n" if edit.crlf else "\n")
    except (OSError, UnicodeDecodeError) as exc:
        return f"error: could not write ({getattr(exc, 'strerror', None) or 'write failed'})"
    lines = edit.diff.splitlines()
    added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    return f"{'created' if edit.before is None else 'edited'} {edit.rel} (+{added} -{removed} lines)"


def run_command(root: Path | None, command: Any) -> str:
    """Exit code and output of an allowed cmd.exe command in `root` (the Project Folder, or the host's fallback
    home folder); the whole tree dies on timeout."""
    try:
        cwd = resolve(root, "")
    except ValueError as exc:
        return f"error: {exc}"
    if not isinstance(command, str) or not command.strip():
        return "error: give a command"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags)
    except OSError as exc:
        return f"error: could not start ({exc.strerror or 'failed'})"
    try:
        raw, _ = proc.communicate(timeout=COMMAND_TIMEOUT_S)
        head = f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw, _ = proc.communicate()
        head = f"stopped after {COMMAND_TIMEOUT_S} s"
    out = (raw or b"").decode("utf-8", "replace").replace("\r\n", "\n").strip()
    if len(out) > MAX_COMMAND_CHARS:  # the end usually holds the verdict (test summary, error)
        half = MAX_COMMAND_CHARS // 2
        out = f"{out[:half]}\n... {len(out) - MAX_COMMAND_CHARS} characters cut ...\n{out[-half:]}"
    return f"{head}\n{out or '(no output)'}"


# ---------------------------------------------------------------------- skills
# A skill is %LOCALAPPDATA%/ByteSprite/skills/<name>/SKILL.md: frontmatter (name, description), then instructions.
# Only names and descriptions go in the system prompt; load_skill returns a body on demand.
MAX_SKILLS = 30
MAX_SKILL_DESC = 200
MAX_SKILL_CHARS = 24000
_SKILL_NAME = re.compile(r"^[\w.-]{1,64}$")


def skills_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ByteSprite" / "skills"


def parse_skill(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter fields, body) of a SKILL.md; no frontmatter means all body."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            break
    else:
        return {}, text.strip()
    meta = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def list_skills(root: Path) -> list[tuple[str, str]]:
    """(folder name, one-line description) of up to MAX_SKILLS skills, sorted by name."""
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and _SKILL_NAME.match(p.name))
    except OSError:
        return []
    out = []
    for folder in folders:
        try:
            meta, _body = parse_skill((folder / "SKILL.md").read_text("utf-8", "replace")[:MAX_SKILL_CHARS])
        except OSError:
            continue
        out.append((folder.name, " ".join(meta.get("description", "").split())[:MAX_SKILL_DESC]))
        if len(out) >= MAX_SKILLS:
            break
    return out


def skills_prompt(root: Path) -> str:
    """The system prompt's skill list, or "" when there are none."""
    skills = list_skills(root)
    if not skills:
        return ""
    rows = "\n".join(f"- {name}: {desc or '(no description)'}" for name, desc in skills)
    return ("The user wrote these skills. When a request matches one, call load_skill with its name first and "
            f"follow what it says (skills are the user's own instructions, unlike files or pasted text):\n{rows}")


def skill_text(description: str, instructions: str) -> str:
    """A SKILL.md as add_skill writes it."""
    return f"---\ndescription: {' '.join(description.split())}\n---\n\n{instructions.strip()}\n"


def write_skill(root: Path, name: str, text: str) -> str:
    """Create root/name/SKILL.md; never overwrites an existing skill."""
    if not _SKILL_NAME.match(name) or name.startswith("."):  # no "..", so the folder stays inside root
        return "error: a skill name is letters, digits, _ . and - only"
    folder = root / name
    try:
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
    except FileExistsError:
        return f'error: a skill named "{name}" already exists'
    except OSError as exc:
        return f"error: could not save the skill ({exc.strerror or 'write failed'})"
    return f'added skill "{name}"; it is listed from the next message'


def plan_skill_update(root: Path, name: Any, description: Any, instructions: Any) -> tuple[Path, str, str]:
    """(SKILL.md path, new text, diff) for update_skill, or ValueError. Nothing is written here.

    The file is rebuilt as skill_text, so frontmatter fields other than description are dropped.
    """
    listed = dict(list_skills(root))
    if not isinstance(name, str) or name.strip() not in listed:
        raise ValueError("no such skill. Skills: " + (", ".join(sorted(listed)) or "none"))
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("give the skill's full new instructions")
    path = root / name.strip() / "SKILL.md"
    try:
        before = path.read_text("utf-8", "replace")
    except OSError as exc:
        raise ValueError(exc.strerror or "could not read the skill") from None
    keep = parse_skill(before)[0].get("description", "")
    text = skill_text(description if isinstance(description, str) and description.strip() else keep, instructions)
    diff = "".join(difflib.unified_diff(before.replace("\r\n", "\n").splitlines(keepends=True),
                                        text.splitlines(keepends=True), "a/SKILL.md", "b/SKILL.md", n=2))
    if not diff:
        raise ValueError("that is what the skill already says")
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n... diff cut, {len(diff) - MAX_DIFF_CHARS} more characters"
    return path, text, diff


MAX_MEMORY_CHARS = 8000  # all notes together; they ride along in every chat's system prompt
MEMORY_HEADER = "# ByteSprite memory\n\nOne note per line starting with \"- \". The AI chat reads these in every chat.\n\n"


def memory_path() -> Path:
    return skills_dir().parent / "memory.md"


def read_memory(path: Path) -> list[str]:
    """The notes: lines starting with "- "; anything else in the file (the user's own text) is ignored."""
    try:
        lines = path.read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [line[2:].strip() for line in lines if line.startswith("- ") and line[2:].strip()]


def memory_prompt(path: Path) -> str:
    """The system prompt's memory block, or "" when there are no notes."""
    notes = read_memory(path)
    if not notes:
        return ""
    rows = "\n".join(f"- {n}" for n in notes)[:MAX_MEMORY_CHARS]
    return ("Your memory: notes you saved in earlier chats with remember, each allowed by the user. Follow them "
            f"unless the user says otherwise now, and forget a note that turns out wrong or stale:\n{rows}")


def plan_memory(path: Path, name: str, text: Any) -> tuple[str, str]:
    """(the file's new text after remember or forget, the note added or removed), or ValueError.

    Only that one line changes; anything else the user wrote in the file stays.
    """
    notes = read_memory(path)
    note = " ".join(text.split()) if isinstance(text, str) else ""
    if not note:
        raise ValueError("give the note's text")
    try:
        lines = path.read_text("utf-8", "replace").splitlines()
    except OSError:
        lines = MEMORY_HEADER.splitlines()
    if name == "remember":
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"keep a note under {MAX_NOTE_CHARS} characters")
        if any(note.lower() == n.lower() for n in notes):
            raise ValueError("that is already in memory")
        if sum(len(n) + 3 for n in notes) + len(note) > MAX_MEMORY_CHARS:
            raise ValueError("memory is full; forget an old or stale note first")
        return "\n".join([*lines, f"- {note}"]) + "\n", note
    hits = [n for n in notes if note.lower() in n.lower()]
    if len(hits) != 1:
        raise ValueError("no note contains that text" if not hits else
                         f"{len(hits)} notes contain that text; quote more of the one to forget")
    gone = next(i for i, line in enumerate(lines) if line.startswith("- ") and line[2:].strip() == hits[0])
    return "\n".join(lines[:gone] + lines[gone + 1:]) + "\n", hits[0]


def write_memory(path: Path, text: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"error: could not save memory ({exc.strerror or 'write failed'})"
    return f"memory saved ({len(read_memory(path))} notes); later chats will see it"


def load_skill(root: Path, name: Any) -> str:
    """A listed skill's instructions for the model, or an `error: ...` line."""
    listed = {n for n, _d in list_skills(root)}
    if not isinstance(name, str) or name.strip() not in listed:  # only listed folder names, so no traversal
        return "error: no such skill. Skills: " + (", ".join(sorted(listed)) or "none")
    try:
        _meta, body = parse_skill((root / name.strip() / "SKILL.md").read_text("utf-8", "replace"))
    except OSError as exc:
        return f"error: {exc.strerror or 'could not read'}"
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + "\n... (skill cut off)"
    return body or "(the skill is empty)"
