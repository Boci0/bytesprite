"""scripts/mine_failures.py: finds failed calls, retry chains, stops and pushback, worst first."""

import importlib.util
import sys
from pathlib import Path

from src.tools.ai_sessions import Session

_spec = importlib.util.spec_from_file_location("mine_failures", Path(__file__).parents[1] / "scripts" / "mine_failures.py")
mf = sys.modules["mine_failures"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)


def _session(*turns):
    return Session("abc", "chat", 0, 0, list(turns))


def test_ranks_retry_chain_with_pushback_first() -> None:
    retry = [(f"run_command(powershell -File _poem_fix{i}.ps1)", "exit code 1\nboom") for i in range(1, 6)]
    s = _session({"role": "user", "content": "style my poem"},
                 {"role": "assistant", "content": "done", "tools": retry},
                 {"role": "user", "content": "it's still wrong"},
                 {"role": "assistant", "content": "fine", "tools": [("read_file(a)", "1 | ok")]},
                 {"role": "user", "content": "thanks"},
                 {"role": "assistant", "content": "(stopped by the user before answering)"})
    found = mf.cases([s])
    assert len(found) == 2  # the clean "fine" answer is not a case
    worst = found[0]
    assert worst.request == "style my poem" and worst.pushback and len(worst.failed) == 5
    assert worst.retries == [("run_command(powershell -file _poem_fix#.ps#)", 5)]
    assert found[1].stopped and not found[1].pushback


def test_error_kinds_group_paths_and_numbers() -> None:
    assert mf._error_kind("error: C:\\x\\a.txt not found") == mf._error_kind("error: C:\\y\\b.txt not found")
    assert mf._error_kind("exit code 1\nlong") == "exit code #"


def test_stray_files_skip_app_files(tmp_path: Path) -> None:
    for name in ("app", "sessions", "memory.md", "_poem_fix.ps1", "preview.png"):
        (tmp_path / name).touch()
    assert mf.stray_files(tmp_path) == ["_poem_fix.ps1", "preview.png"]


def test_report_escapes_chat_text() -> None:
    s = _session({"role": "user", "content": "<script>x</script>"},
                 {"role": "assistant", "content": "ok", "tools": [("read_file(a)", "error: nope")]})
    found = mf.cases([s])
    page = mf.report_html(found, 1, 1, *mf.tool_failures(found), mf.Counter(), [])
    assert "&lt;script&gt;" in page and "<script>x" not in page
