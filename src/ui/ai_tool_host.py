"""Runs the AI chat model's PC tools (src/tools/ai_tools.py) for one PetWindow.

`run` is called on the chat's worker thread. File tools and OCR run right there; anything touching Qt
(clipboard, screen grab, Save dialog, pet actions) is queued to the Qt thread and the worker waits for its result.
Tools of the user's MCP servers (src/tools/mcp_client.py) join the specs; each server's first call in a chat
waits for the user to allow it in the chat.
edit_file, run_command, add_mcp_server and add_skill also wait for the user's Allow in the chat (once, or for the
whole chat).
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QStandardPaths, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QImage
from PyQt6.QtWidgets import QFileDialog

from src.tools.ai_tools import (FILE_TOOLS, PET_ACTIONS, TOOL_SPECS, apply_edit, load_skill, plan_edit, run_command,
                                run_file_tool, save_args, skill_text, skills_dir, write_skill)
from src.tools.macros import parse_recipe
from src.tools.mcp_client import SEP, McpHub, add_server, find_servers, safe_name
from src.ui.quick_tools import ocr_image_file

GUI_TIMEOUT_S = 15.0
SAVE_TIMEOUT_S = 600.0  # the Save dialog waits on the user
APPROVE_TIMEOUT_S = 300.0  # the user reads the MCP confirm, a diff or a command in the chat
MAX_CLIP_CHARS = 12000
MAX_SCREEN_CHARS = 8000


class AiToolHost(QObject):
    """The ToolHost the chat hands to ask(); `window` is the PetWindow."""

    _call = pyqtSignal(object)

    def __init__(self, window: Any, mcp: McpHub | None = None) -> None:
        super().__init__(window)  # lives on the Qt thread, so the queued signal lands there
        self._win = window
        self.saved: list[Path] = []
        self.mcp = mcp or McpHub()
        self._call.connect(self._on_call)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.mcp.close)

    @property
    def specs(self) -> list[dict[str, Any]]:
        """Built-in tools plus the MCP servers' (started on first use, on the worker thread)."""
        return TOOL_SPECS + self.mcp.specs()

    def run(self, name: str, args: dict[str, Any]) -> str:
        server = self.mcp.server_of(name)
        if server is not None:
            chat = self._win._ai_chat
            allowed = self._on_gui(lambda done: chat.confirm_mcp(server, name, done), APPROVE_TIMEOUT_S)
            return self.mcp.call(name, args) if allowed is True else "error: the user did not allow this MCP server"
        if name in FILE_TOOLS:
            return run_file_tool(self._project(), name, args)
        if name == "edit_file":
            try:
                edit = plan_edit(self._project(), args)
            except ValueError as exc:
                return f"error: {exc}"
            verb = "create" if edit.before is None else "edit"
            if not self._allowed("edit_file", f"The model wants to {verb} {edit.rel}:\n\n{edit.diff}", "file edits"):
                return "error: the user declined this edit"
            return apply_edit(edit)
        if name == "run_command":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            if not self._allowed("run_command", f"The model wants to run in {where}:\n\n{command.strip()}", "commands"):
                return "error: the user declined this command"
            return run_command(cwd, command)
        if name == "load_skill":
            return load_skill(skills_dir(), args.get("name"))
        if name == "find_mcp_servers":
            query = args.get("query")
            return find_servers(query.strip()) if isinstance(query, str) and query.strip() else "error: give a query"
        if name == "add_mcp_server":
            return self._add_mcp_server(args)
        if name == "add_skill":
            fields = [args.get(k) for k in ("name", "description", "instructions")]
            if not all(isinstance(f, str) and f.strip() for f in fields):
                return "error: give a name, a description and instructions"
            skill, description, instructions = (f.strip() for f in fields)
            text = skill_text(description, instructions)
            if not self._allowed("add_skill", f'The model wants to save a skill "{skill}" that later chats will '
                                 f"follow as your own instructions:\n\n{text}", "saving skills"):
                return "error: the user declined this skill"
            return write_skill(skills_dir(), skill, text)
        if name == "read_screen":
            image = self._on_gui(self._grab_screen)
            return self._ocr(image) if isinstance(image, QImage) and not image.isNull() else "error: no screen grab"
        if name == "save_file":
            try:
                file_name, content = save_args(args)
            except ValueError as exc:
                return f"error: {exc}"
            out = self._on_gui(lambda done: done(self._save_file(file_name, content)), SAVE_TIMEOUT_S)
            return out if isinstance(out, str) else "error: the user did not answer the Save dialog in time"
        out = self._on_gui(lambda done: done(self._pet_tool(name, args)))
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _add_mcp_server(self, args: dict[str, Any]) -> str:
        server, command = args.get("name"), args.get("command")
        cmd_args, env = args.get("args") or [], args.get("env") or {}
        if (not isinstance(server, str) or not server or safe_name(server) != server or SEP in server
                or not isinstance(command, str) or not command.strip()
                or not isinstance(cmd_args, list) or not all(isinstance(a, str) for a in cmd_args)
                or not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values())):
            return "error: give a name (letters, digits, single _ and -), a command, string args and string env values"
        line = " ".join([command.strip(), *cmd_args])
        env_note = f"\nwith env: {', '.join(env)}" if env else ""  # names only, values may be secrets
        if not self._allowed("add_mcp_server", f'The model wants to install MCP server "{server}". It will run this '
                             f"on your PC with your permissions whenever the AI chat starts:\n\n{line}{env_note}",
                             "MCP installs"):
            return "error: the user declined this MCP server"
        out = add_server(server, command.strip(), cmd_args, {str(k): v for k, v in env.items()}, self.mcp.path)
        if out.startswith("error"):
            return out
        tools = [s["function"]["name"] for s in self.mcp.specs() if self.mcp.server_of(s["function"]["name"]) == server]
        if not tools:
            return out + ", but it did not start (wrong command, missing npx / uvx, or a missing env value)"
        return out + (f"; it started with these tools: {', '.join(tools)}. If they are not in your tool list yet, "
                      "they are from the user's next message, so tell the user to send one.")

    def _project(self) -> Path | None:
        return getattr(self._win._state_machine.registry.get_plugin("project"), "repo", None)

    def _allowed(self, key: str, text: str, what: str) -> bool:
        """The user's answer in the chat; no answer within APPROVE_TIMEOUT_S counts as declined."""
        chat = self._win._ai_chat
        return self._on_gui(lambda done: chat.confirm(key, text, what, done), APPROVE_TIMEOUT_S) is True

    def take_saved(self) -> list[Path]:
        """Files saved since the last call (the chat shows a row for each)."""
        saved, self.saved = self.saved, []
        return saved

    # ------------------------------------------------------------------ thread bridge

    def _on_gui(self, fn: Callable[[Callable[[Any], None]], None], timeout: float = GUI_TIMEOUT_S) -> Any:
        """Run fn(done) on the Qt thread and wait for done(result)."""
        box: dict[str, Any] = {"event": threading.Event(), "result": None}
        self._call.emit((fn, box))
        box["event"].wait(timeout)
        return box["result"]

    def _on_call(self, item: tuple[Callable, dict[str, Any]]) -> None:
        fn, box = item

        def done(result: Any) -> None:
            box["result"] = result
            box["event"].set()

        try:
            fn(done)
        except Exception as exc:  # a tool bug must not leave the worker waiting
            done(f"error: {type(exc).__name__}")

    # ------------------------------------------------------------------ Qt-thread tools

    def _grab_screen(self, done: Callable[[Any], None]) -> None:
        chat = self._win._ai_chat
        shown = [w for w in (self._win, chat) if w is not None and w.isVisible()]
        for w in shown:  # keep the pet and the chat out of the grab
            w.hide()

        def grab() -> None:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            image = screen.grabWindow(0).toImage() if screen is not None else None
            for w in shown:
                w.show()
            done(image)

        QTimer.singleShot(180, grab)

    def _save_file(self, file_name: str, content: str) -> str:
        """Nothing is written unless the user picks a path; the dialog itself confirms overwrites."""
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        path, _filter = QFileDialog.getSaveFileName(self._win._ai_chat, "Save file from ByteSprite AI",
                                                    str(Path(downloads) / file_name))
        if not path:
            return "the user cancelled the save"
        try:
            Path(path).write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return f"error: could not save ({exc.strerror or 'write failed'})"
        self.saved.append(Path(path))
        return f"saved to {path}"

    def _pet_tool(self, name: str, args: dict[str, Any]) -> str:
        cb = QGuiApplication.clipboard()
        if name == "read_clipboard":
            text = cb.text() if cb is not None else ""
            return text[:MAX_CLIP_CHARS] if text.strip() else "the clipboard has no text"
        if name == "copy_text":
            text = args.get("text")
            if not isinstance(text, str) or not text or cb is None:
                return "error: nothing to copy"
            cb.setText(text)
            return "copied to the clipboard"
        if name == "pet_status":
            rows = []
            for plugin in self._win._state_machine.registry.plugins():
                hud = plugin.get_hud_data()
                bars = ", ".join(f"{b.label} {(b.display_text or f'{b.value:.0f}%').strip()}" for b in hud.bars)
                rows.append(f"{hud.badge_left} [{hud.badge_right}] {bars}. {hud.detail}".strip())
            project = self._win._state_machine.registry.get_plugin("project")
            repo = getattr(project, "repo", None)
            return "\n".join([*rows, f"Project Folder: {repo or 'none chosen'}"])
        if name == "pet_action":
            action = args.get("action")
            if action not in PET_ACTIONS:
                return "error: not an allowed action"
            self._win.trigger_tool_action(action)
            return f"done: {PET_ACTIONS[action]}"
        if name == "run_macro":
            macro = self._win.macros.find(args.get("name") if isinstance(args.get("name"), str) else "")
            if macro is None:
                return "error: no such macro. Saved: " + ", ".join(m.name for m in self._win.macros.macros)
            if any(step in ("ask", "chat") for step, _arg in parse_recipe(macro.recipe)):  # it would use this busy chat
                return "error: that macro uses the AI chat itself, so the user has to run it"
            self._win._macro_runner.run(macro)
            return f"started macro {macro.name}: {macro.recipe}"
        if name == "open_link":
            url = args.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                return "error: https links only"
            self._win.trigger_tool_action("web:" + url)
            return "opened in the browser"
        return "error: unknown tool"

    # ------------------------------------------------------------------ worker-thread OCR

    @staticmethod
    def _ocr(image: QImage) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="bytesprite_ocr_"))
        png = tmp / "screen.png"
        try:
            image.save(str(png), "PNG")
            text = ocr_image_file(png)
        finally:
            for leftover in (png, png.with_suffix(".ps1")):
                leftover.unlink(missing_ok=True)
            tmp.rmdir()
        return text.strip()[:MAX_SCREEN_CHARS] or "no text found on screen"
