"""Runs the AI chat model's PC tools (src/tools/ai_tools.py) for one PetWindow.

`run` is called on the chat's worker thread. File tools and OCR run right there; anything touching Qt
(clipboard, screen grab, Save dialog, pet actions) is queued to the Qt thread and the worker waits for its result.
Tools of the user's MCP servers (src/tools/mcp_client.py) join the specs; each server's first call in a chat
waits for the user to allow it in the chat.
edit_file, edit_files, run_command, start_background, add_mcp_server, add_skill, update_skill, remember and forget
also wait for the user's Allow in the chat (once, or for the whole chat). Background processes (`procs`) are
killed when the app quits.
"""

from __future__ import annotations

import ctypes
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QStandardPaths, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QImage
from PyQt6.QtWidgets import QFileDialog

from src.tools import computer
from src.tools.ai_tools import (calculate, COMPUTER_TOOLS, MAX_BATCH_STEPS, FILE_TOOLS, PET_ACTIONS, TOOL_SPECS, BackgroundProcs, apply_edits,
                                computer_action, destructive_reason, is_secret, load_skill, memory_path, parse_boxes, plan_edit,
                                plan_edits, plan_memory, plan_skill_update, print_to_pdf_paths, resolve,
                                PDF_WAIT_S, changed_media, file_stamp, media_stamps, wait_for_new_file, run_command, run_file_tool, save_args,
                                skill_text, skills_dir, task_list, write_memory, write_skill)
from src.tools.errands import describe as describe_errand
from src.tools.file_forge import scrub_metadata
from src.tools.macros import parse_recipe
from src.tools.mcp_client import SEP, McpHub, add_server, find_servers, safe_name
from src.ui.quick_tools import ocr_image_file

GUI_TIMEOUT_S = 15.0
SAVE_TIMEOUT_S = 600.0  # the Save dialog waits on the user
APPROVE_TIMEOUT_S = 300.0  # the user reads the MCP confirm, a diff or a command in the chat
ERRAND_APPROVE_S = 1800.0  # an errand runs while the user may be away, so its approvals wait longer
MAX_CLIP_CHARS = 12000
MAX_SCREEN_CHARS = 8000


def _rel_name(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _strings(value: Any) -> list[str]:
    """Every string value inside nested JSON (keys left out); a loop, so deep nesting cannot overflow the stack."""
    found, todo = [], [value]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found


def _front_window_shot(hwnd: int) -> QImage | None:
    """Only window `hwnd`, cut from a grab of the monitor it is on (None when that fails)."""
    found = computer.window_on_monitor(hwnd)
    if found is None:
        return None
    device, (left, top, right, bottom), (mon_w, mon_h) = found
    screen = next((s for s in QGuiApplication.screens() if s.name() == device), None)
    image = screen.grabWindow(0).toImage() if screen is not None else None
    if image is None or image.isNull() or not mon_w or not mon_h:
        return None
    sx, sy = image.width() / mon_w, image.height() / mon_h  # the grab may be in logical, not physical, pixels
    return image.copy(round(left * sx), round(top * sy), round((right - left) * sx), round((bottom - top) * sy))


def _ours(win: Any) -> list[Any]:
    return [w for w in (win, win._ai_chat) if w is not None and w.isVisible()]


def show_quietly(w: Any) -> None:
    """Show without taking the keyboard from the app the user (or the model) is in."""
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    w.show()
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)


def grab_screen(win: Any, done: Callable[[Any], None], primary: bool = False, window: bool = False) -> None:
    """Grab the screen under the cursor (or the primary one, with the front app's handle; or with `window`,
    just the front app's window) on the Qt thread, with the pet `win` and its chat left out of the shot.
    They stay on screen (excluded from capture); only where Windows cannot exclude them are they hidden."""
    shown = _ours(win)
    hidden = [w for w in shown if not computer.exclude_from_capture(int(w.winId()), True)]
    for w in hidden:
        w.hide()

    def grab() -> None:
        hwnd = computer.focus_front_app() if primary else computer.front_app()  # the app under the pet and chat
        image = _front_window_shot(hwnd) if window else None
        if image is None:
            screen = (None if primary else QGuiApplication.screenAt(QCursor.pos())) or QGuiApplication.primaryScreen()
            image = screen.grabWindow(0).toImage() if screen is not None else None
        for w in shown:
            if w in hidden:
                show_quietly(w)
            else:
                computer.exclude_from_capture(int(w.winId()), False)
        done((image, hwnd) if primary else image)

    QTimer.singleShot(180 if hidden else 30, grab)  # the compositor applies the affinity on its next frame


class AiToolHost(QObject):
    """The ToolHost the chat hands to ask(); `window` is the PetWindow."""

    _call = pyqtSignal(object)
    activity = pyqtSignal(str)  # the tool that runs now, "approval" while one waits on the user, "" when idle

    def __init__(self, window: Any, mcp: McpHub | None = None) -> None:
        super().__init__(window)  # lives on the Qt thread, so the queued signal lands there
        self._win = window
        self.saved: list[Path] = []
        self._shot = ""
        self.mcp = mcp or McpHub()
        self.procs = BackgroundProcs()
        self.cancel = threading.Event()  # the chat swaps in a fresh one per answer and sets it on Stop
        self.approve_s = APPROVE_TIMEOUT_S  # the chat raises it for an errand's answer
        self._call.connect(self._on_call)
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._close_idle_mcp)
        self._idle_timer.start(60_000)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.mcp.close)
            app.aboutToQuit.connect(self.procs.stop_all)

    def _close_idle_mcp(self) -> None:
        if getattr(self._win._ai_chat, "_job", None) is None:  # never mid-answer
            threading.Thread(target=self.mcp.close_idle, daemon=True).start()  # taskkill blocks briefly

    @property
    def specs(self) -> list[dict[str, Any]]:
        """Built-in tools plus the MCP servers' (started on first use, on the worker thread)."""
        return TOOL_SPECS + self.mcp.specs()

    def run(self, name: str, args: dict[str, Any]) -> str:
        """One tool call; the pet acts it out meanwhile (`activity`)."""
        self._tool = name  # what the pet goes back to acting out after an approval
        self.activity.emit(name)
        try:
            return self._run(name, args)
        finally:
            self.activity.emit("")

    def _run(self, name: str, args: dict[str, Any]) -> str:
        server = self.mcp.server_of(name)
        if server is not None:
            chat = self._win._ai_chat
            shown = json.dumps(args, ensure_ascii=False)
            # Values only: an argument named "format" or "truncate" is not a destructive command
            risk = "destructive tool" if self.mcp.is_destructive(name) else destructive_reason("\n".join(_strings(args)))
            allowed = self._waiting(lambda done: chat.confirm_mcp(server, name, done, shown[:600], risk))
            return self.mcp.call(name, args) if allowed is True else "error: the user did not allow this MCP server"
        if name in FILE_TOOLS:
            secrets = False
            if name == "read_file":
                try:
                    path = resolve(self._project(), args.get("path"))
                except (OSError, ValueError):
                    path = None
                if path is not None and is_secret(path) and path.is_file():
                    secrets = self._allowed("read_secret", f"The model wants to read {path}, which looks like a "
                                            "secrets file (keys, passwords or tokens).", "secret files",
                                            "secrets file")
            return run_file_tool(self._project(), name, args, secrets=secrets)
        if name in ("edit_file", "edit_files"):
            try:
                edits = plan_edits(self._project(), args) if name == "edit_files" else [plan_edit(self._project(), args)]
            except ValueError as exc:
                return f"error: {exc}"
            if len(edits) == 1:
                verb = "create" if edits[0].before is None else "edit"
                ask = f"The model wants to {verb} {edits[0].rel}:\n\n{edits[0].diff}"
            else:
                diffs = "\n\n".join(f"{'new file' if e.before is None else 'edit'} {e.rel}\n{e.diff}" for e in edits)
                ask = f"The model wants to change {len(edits)} files, all or none:\n\n{diffs}"
            if not self._allowed("edit_file", ask, "file edits"):
                return "error: the user declined this edit"
            return apply_edits(edits)
        if name in ("set_errand", "cancel_errand"):
            return self._errand(name, args)
        if name == "calculate":  # pure arithmetic, nothing to allow
            return calculate(args.get("expression"))
        if name == "plan_tasks":
            try:
                tasks = task_list(args)
            except ValueError as exc:
                return f"error: {exc}"
            self._on_gui(lambda done: done(self._win._ai_chat.set_tasks(tasks) or "ok"))
            return f"task list shown ({sum(1 for _t, s in tasks if s == 'done')} of {len(tasks)} done)"
        if name == "run_command":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            risk = destructive_reason(command)
            if not self._allowed("run_command", f"The model wants to run in {where}:\n\n{command.strip()}", "commands",
                                 risk):
                return "error: the user declined this command"
            pdfs = {pdf: file_stamp(pdf) for pdf in print_to_pdf_paths(command, cwd)}
            media = media_stamps(command, cwd)
            out = run_command(cwd, command, self.cancel)
            for pdf, before in pdfs.items():  # headless Edge stamps HeadlessChrome, Skia and dates
                # msedge.exe hands off to a child and returns before the PDF is written: wait for the new file
                if wait_for_new_file(pdf, before, PDF_WAIT_S, self.cancel):
                    out += f"\n[Paperduck: {pdf.name}: metadata {scrub_metadata(pdf)}]"
                else:
                    out += f"\n[Paperduck: {pdf.name} was not written within {PDF_WAIT_S:g} s; call clean_metadata on it]"
            # Other PDFs and images the command made may carry metadata too; they are the user's to keep as they
            # are (a build's own fixtures, say), so the model is told rather than the files changed
            unclean = [p for p in changed_media(media, cwd, command)
                       if p not in pdfs and scrub_metadata(p, dry_run=True) == "would clean"]
            if unclean:
                names = ", ".join(_rel_name(p, cwd) for p in unclean[:8])
                out += (f"\n[Paperduck: this made or changed {names}, which still carry metadata (author, creating app, "
                        "dates). Call clean_metadata on each one you hand to the user.]")
            return out
        if name == "start_background":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            # its own key: allowing background starts for this chat must not also allow every foreground command
            if not self._allowed("start_background", f"The model wants to start this in the background in {where}. It keeps "
                                 f"running until you stop it:\n\n{command.strip()}", "background commands",
                                 destructive_reason(command)):
                return "error: the user declined this command"
            out = self.procs.start(cwd, command)
            self._on_gui(lambda done: done(self._win._ai_chat.watch_procs() or "ok"))
            return out
        if name == "read_background":
            return self.procs.read(args.get("id"))
        if name == "stop_background":
            return self.procs.stop(args.get("id"))
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
        if name == "update_skill":
            plan = lambda: plan_skill_update(skills_dir(), args.get("name"), args.get("description"),  # noqa: E731
                                             args.get("instructions"))
            try:
                path, text, diff = plan()
                if not self._allowed("update_skill", f'The model wants to change your skill "{path.parent.name}", '
                                     f"which later chats follow as your own instructions:\n\n{diff}", "skill updates"):
                    return "error: the user declined this skill change"
                if plan()[2] != diff:
                    return "error: the skill changed while the user was deciding; load_skill it again"
                path.write_text(text, encoding="utf-8")
            except ValueError as exc:
                return f"error: {exc}"
            except OSError as exc:
                return f"error: could not save the skill ({exc.strerror or 'write failed'})"
            return f'updated skill "{path.parent.name}"'
        if name in ("remember", "forget"):
            try:
                _text, note = plan_memory(memory_path(), name, args.get("note"))
            except ValueError as exc:
                return f"error: {exc}"
            verb = "remember this in every later chat" if name == "remember" else "forget this note"
            if not self._allowed("memory", f"The model wants to {verb}:\n\n{note}", "memory changes"):
                return "error: the user declined this memory change"
            try:
                text, again = plan_memory(memory_path(), name, args.get("note"))  # the file may have changed meanwhile
            except ValueError as exc:
                return f"error: {exc}"
            if again != note:
                return "error: memory changed while the user was deciding; try again"
            return write_memory(memory_path(), text)
        if name == "read_screen":
            image = self._on_gui(self._grab_screen)
            return self._ocr(image) if isinstance(image, QImage) and not image.isNull() else "error: no screen grab"
        if name == "screen_map":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            image, hwnd = got
            with ThreadPoolExecutor(max_workers=2) as pool:  # OCR and the control tree overlap
                controls = pool.submit(computer.ui_elements, hwnd)
                lines = pool.submit(self._ocr, image, True).result()
                elements = controls.result()
            from src.ui.ai_chat import MAX_IMAGE_SIDE, image_data_url

            side = max(image.width(), image.height())
            self._shot = image_data_url(image)
            note = (f"The screenshot you see is scaled down {side / MAX_IMAGE_SIDE:.2f}x: do not read pixels from "
                    "it, use the (x, y) listed here." if side > MAX_IMAGE_SIDE else "The screenshot is at full size.")
            return parse_boxes(lines, image.width(), image.height(), elements, note)
        if name in COMPUTER_TOOLS:
            return self._computer(name, args)
        if name == "computer_batch":
            return self._batch(args.get("steps"))
        if name == "clean_metadata":  # only blanks metadata in a PDF/PNG/JPEG, so it needs no approval
            try:
                path = resolve(self._project(), args.get("path"))
            except (OSError, ValueError):
                return "error: bad path"
            return scrub_metadata(path) if path.is_file() else "error: not a file"
        if name == "save_file":
            try:
                file_name, content = save_args(args)
            except ValueError as exc:
                return f"error: {exc}"
            out = self._on_gui(lambda done: done(self._save_file(file_name, content)), SAVE_TIMEOUT_S)
            return out if isinstance(out, str) else "error: the user did not answer the Save dialog in time"
        out = self._on_gui(lambda done: done(self._pet_tool(name, args)))
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _errand(self, name: str, args: dict[str, Any]) -> str:
        """set_errand asks every time (it is an instruction that runs later, maybe while the user is away);
        cancel_errand only removes one. The plugin is touched on the Qt thread, where the pet polls it."""
        errands = self._win._state_machine.registry.get_plugin("errands")
        if errands is None:
            return "error: errands are not available"
        if name == "cancel_errand":
            out = self._on_gui(lambda done: done(errands.cancel(args.get("id"))))
            return out if isinstance(out, str) else "error: the pet did not answer in time"
        try:
            from src.tools.errands import parse_when

            kind, arg = parse_when(args.get("when"))
        except ValueError as exc:
            return f"error: {exc}"
        instruction = args.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return "error: give the instruction to follow when it fires"
        if not self._allowed(f"errand:{time.monotonic()}", f"The model wants to set an errand: {describe_errand(kind, arg)}, "
                             "it will run this in this chat (edits and commands still ask you):\n\n"
                             f"{instruction.strip()}", "errand"):
            return "error: the user declined this errand"

        def add(done: Callable[[Any], None]) -> None:
            try:
                errand = errands.add(args.get("when"), instruction, self._win._ai_chat.ensure_sid())
            except ValueError as exc:
                done(f"error: {exc}")
                return
            self._win.update()  # the errand tag
            done(f"errand {errand.id} set: {describe_errand(errand.kind, errand.arg)}. It runs in this chat when "
                 "that happens; tell the user they can leave it to you.")

        out = self._on_gui(add)
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _computer(self, name: str, args: dict[str, Any]) -> str:
        """Mouse and keyboard on the primary monitor. Every action needs the user's Allow (once or for the chat); a
        delete key or a destructive typed command asks every time."""
        try:
            what, risk = computer_action(name, args)
        except ValueError as exc:
            return f"error: {exc}"
        if not self._allowed("computer", f"The model wants to control your computer:\n\n{what}", "computer control",
                             risk):
            return "error: the user declined this action"
        out = self._on_gui(lambda done: self._act(done, name, args), 90.0)  # up to 2000 chars at 20 ms
        return out if isinstance(out, str) else "error: the action did not finish in time"

    def _batch(self, steps: Any) -> str:
        """computer_batch: each step through `_computer` (same approvals), stopping at the first error or Stop."""
        if not isinstance(steps, list) or not steps or len(steps) > MAX_BATCH_STEPS:
            return f"error: give 1 to {MAX_BATCH_STEPS} steps"
        cancel, lines = self.cancel, []
        for i, step in enumerate(steps, 1):
            tool = step.get("tool") if isinstance(step, dict) else None
            if tool not in COMPUTER_TOOLS:
                return "\n".join([*lines, f"error: step {i} needs \"tool\" set to one of {', '.join(sorted(COMPUTER_TOOLS))}"])
            if cancel.is_set():
                return "\n".join([*lines, "error: stopped by the user"])
            out = self._computer(tool, {k: v for k, v in step.items() if k != "tool"})
            lines.append(f"{i}. {out}")
            if out.startswith("error"):
                return "\n".join([*lines, f"error: stopped at step {i} of {len(steps)}"])
        return "\n".join(lines)

    def _act(self, done: Callable[[Any], None], name: str, args: dict[str, Any]) -> None:
        """Make the pet and the chat click-through (hidden only where Windows cannot), give the keyboard to the
        app under them, act, then restore them."""
        shown = _ours(self._win)
        hidden = [w for w in shown if not computer.click_through(int(w.winId()), True)]
        for w in hidden:
            w.hide()
        before = [("", False)]

        def act() -> str:  # on a worker thread: typing takes 20 ms a character and must not freeze the pet
            computer.focus_front_app()  # the chat has the keyboard after the user pressed Enter
            before[0] = computer.front_state()
            try:
                if name == "mouse_click":
                    computer.click(args["x"], args["y"], args.get("button") or "left", bool(args.get("double")))
                    result = "clicked"
                elif name == "type_text":
                    computer.type_text(args["text"])
                    result = f"typed {min(len(args['text']), computer.MAX_TYPE_CHARS)} characters"
                elif name == "press_key":
                    computer.press(computer.parse_keys(args["keys"]))
                    result = f"pressed {args['keys']}"
                else:
                    computer.scroll(args["amount"], args.get("x"), args.get("y"))
                    result = "scrolled"
            except (ValueError, OSError, TypeError, ctypes.ArgumentError) as exc:  # a bad arg must not hang the call
                result = f"error: {exc}"
            return result

        def finish(result: str) -> None:
            for w in shown:  # back without taking the keyboard from the app the model is driving
                if w in hidden:
                    show_quietly(w)
                else:
                    computer.click_through(int(w.winId()), False)
            done(result + "." + computer.front_note(before[0], computer.front_state()) + " Call screen_map for details."
                 if not result.startswith("error") else result)

        def work() -> None:
            result = act()
            # back on the Qt thread; the app needs a moment to react before the next look
            self._call.emit((lambda _done: QTimer.singleShot(250, lambda: finish(result)),
                             {"event": threading.Event(), "result": None}))

        QTimer.singleShot(180 if hidden else 0, lambda: threading.Thread(target=work, daemon=True).start())

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

    def _allowed(self, key: str, text: str, what: str, risk: str = "") -> bool:
        """The user's answer in the chat; no answer within APPROVE_TIMEOUT_S counts as declined.
        A `risk` (destructive command) is asked every time, even when the chat allowed the tool before."""
        chat = self._win._ai_chat
        ask = (lambda done: chat.confirm(key, text, what, done, danger=risk)) if risk else (
            lambda done: chat.confirm(key, text, what, done))
        return self._waiting(ask) is True

    def _waiting(self, ask: Callable[[Callable[[Any], None]], None]) -> Any:
        """An approval on the Qt thread: the pet waits with the user (`activity` "approval") until it is answered."""
        self.activity.emit("approval")
        try:
            return self._on_gui(ask, self.approve_s)
        finally:
            self.activity.emit(getattr(self, "_tool", ""))

    def allow_link(self, name: str, url: str) -> bool:
        """A link the model built after reading local data: the user checks the full URL for their data."""
        verb = "open this link in your browser" if name == "open_link" else "fetch this page"
        return self._allowed("link", f"The model wants to {verb} after reading your files, clipboard or screen. "
                             f"Check the address does not carry your data:\n\n{url[:2000]}", "links",
                             "new link after reading your data")

    def take_image(self) -> str:
        """The screenshot (data URL) of the last screen_map, once: the request loop sends it to a model that sees."""
        shot, self._shot = self._shot, ""
        return shot

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

    def _grab_screen(self, done: Callable[[Any], None], primary: bool = False) -> None:
        grab_screen(self._win, done, primary)

    def _save_file(self, file_name: str, content: str) -> str:
        """Nothing is written unless the user picks a path; the dialog itself confirms overwrites."""
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        path, _filter = QFileDialog.getSaveFileName(self._win._ai_chat, "Save file from Paperduck AI",
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
            for plugin in self._win._state_machine.registry.plugins:
                hud = plugin.get_hud_data()
                bars = ", ".join(f"{b.label} {(b.display_text or f'{b.value:.0f}%').strip()}" for b in hud.bars)
                rows.append(f"{hud.badge_left} [{hud.badge_right}] {bars}. {hud.detail}".strip())
            project = self._win._state_machine.registry.get_plugin("project")
            repo = getattr(project, "repo", None)
            errands = self._win._state_machine.registry.get_plugin("errands")
            waiting = errands.summary() if errands is not None else ""
            return "\n".join([*rows, f"Project Folder: {repo or 'none chosen'}",
                              "Errands waiting:\n" + waiting if waiting else "No errands waiting"])
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
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                return "error: web links (http or https) only"
            self._win.trigger_tool_action("web:" + url)
            return "opened in the browser"
        return "error: unknown tool"

    # ------------------------------------------------------------------ worker-thread OCR

    @staticmethod
    def _ocr(image: QImage, boxes: bool = False) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="paperduck_ocr_"))
        png = tmp / "screen.png"
        try:
            image.save(str(png), "PNG")
            text = ocr_image_file(png, boxes=boxes)
        finally:
            for leftover in (png, png.with_suffix(".ps1")):
                leftover.unlink(missing_ok=True)
            tmp.rmdir()
        if boxes:
            return text.strip()[:MAX_SCREEN_CHARS * 2]
        return text.strip()[:MAX_SCREEN_CHARS] or "no text found on screen"
