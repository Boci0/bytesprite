"""Runs the AI chat model's PC tools (src/tools/ai_tools.py) for one PetWindow.

`run` is called on the chat's worker thread. File tools and OCR run right there; anything touching Qt
(clipboard, screen grab, pet actions) is queued to the Qt thread and the worker waits for its result.
Tools of the user's MCP servers (src/tools/mcp_client.py) join the specs; each server's first call in a chat
waits for the user to allow it in the chat.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QImage

from src.tools.ai_tools import FILE_TOOLS, PET_ACTIONS, TOOL_SPECS, run_file_tool
from src.tools.macros import parse_recipe
from src.tools.mcp_client import McpHub
from src.ui.quick_tools import ocr_image_file

GUI_TIMEOUT_S = 15.0
APPROVE_TIMEOUT_S = 120.0  # the user reads the MCP confirm in the chat
MAX_CLIP_CHARS = 12000
MAX_SCREEN_CHARS = 8000


class AiToolHost(QObject):
    """The ToolHost the chat hands to ask(); `window` is the PetWindow."""

    _call = pyqtSignal(object)

    def __init__(self, window: Any, mcp: McpHub | None = None) -> None:
        super().__init__(window)  # lives on the Qt thread, so the queued signal lands there
        self._win = window
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
            project = self._win._state_machine.registry.get_plugin("project")
            return run_file_tool(getattr(project, "repo", None), name, args)
        if name == "read_screen":
            image = self._on_gui(self._grab_screen)
            return self._ocr(image) if isinstance(image, QImage) and not image.isNull() else "error: no screen grab"
        out = self._on_gui(lambda done: done(self._pet_tool(name, args)))
        return out if isinstance(out, str) else "error: the pet did not answer in time"

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
