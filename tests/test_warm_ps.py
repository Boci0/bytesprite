import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")


def test_warm_worker_runs_scripts_with_named_args_and_survives_a_failure() -> None:
    from src.tools import warm_ps

    script = 'param([string]$Word, [switch]$Twice)\n$line = "x" * 5000 + $Word\nif ($Twice) { $line; $line } else { $line }'
    one = warm_ps.run("test", script, {"Word": "end"})
    assert one == "x" * 5000 + "end"  # one long line, not wrapped
    assert warm_ps.run("test", script, {"Word": "é", "Twice": True}).splitlines() == ["x" * 5000 + "é"] * 2
    assert warm_ps.run("test", "throw 'boom'", {}) == ""  # a failing script is '' like a non-zero exit was
    assert warm_ps.run("test", "'still here'", {}) == "still here"
    assert warm_ps.run("test", "Start-Sleep 5", {}, timeout_s=1) == ""  # hung: killed, and the next call restarts it
    assert warm_ps.run("test", "'again'", {}) == "again"


def test_warm_ui_automation_lists_desktop_controls() -> None:
    import ctypes
    import re

    from src.tools import computer

    lines = computer.ui_elements(ctypes.windll.user32.GetDesktopWindow()).splitlines()
    good = [line for line in lines if re.fullmatch(r"-?\d+,-?\d+,\d+,\d+	\w+	.*", line)]
    assert len(good) * 2 > len(lines)  # a name with a line break (tray tooltips) splits as it did before
