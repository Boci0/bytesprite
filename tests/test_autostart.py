"""Start with Windows: the launcher ships inside src/ (updates replace src/ only) and stale Run values get repaired."""

import shutil
import subprocess
import sys
from pathlib import Path

from src.storage import storage_manager
from src.storage.storage_manager import autostart_command, stale_autostart

ROOT = Path(__file__).resolve().parents[1]


def test_command_points_at_launcher_inside_src() -> None:
    command = autostart_command()
    launcher = ROOT / "src" / "autostart.pyw"
    assert launcher.is_file() and f'"{launcher}"' in command
    assert len(command) < 260  # Run values are capped


def test_launcher_finds_app_root_from_inside_src(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    shutil.copy(ROOT / "src" / "autostart.pyw", app / "src" / "autostart.pyw")
    (app / "src" / "__init__.py").write_text("", encoding="utf-8")
    (app / "src" / "main.py").write_text("def main():\n    return 7\n", encoding="utf-8")
    # Run as Windows does: no working directory of ours, the file path alone
    done = subprocess.run([sys.executable, str(app / "src" / "autostart.pyw")], cwd=tmp_path, timeout=30)
    assert done.returncode == 7


def test_stale_values_are_this_copys_only() -> None:
    root = Path(r"C:\Users\me\AppData\Local\Paperduck\app")
    new = f'"C:\\py\\pythonw.exe" "{root}\\src\\autostart.pyw"'
    assert not stale_autostart(new, root, new)
    assert stale_autostart(f'"C:\\py\\pythonw.exe" "{root}\\paperduck.pyw"', root, new)  # never shipped by updates
    assert stale_autostart(f'"C:\\py\\pythonw.exe" -c "import sys;sys.path.insert(0,r\'{root}\')..."', root, new)
    other = r'"C:\py\pythonw.exe" "C:\Users\me\Downloads\desktop_pet\paperduck.pyw"'
    assert not stale_autostart(other, root, new)  # a dev checkout never takes over the installed copy's value


def test_repair_rewrites_only_stale_values(monkeypatch) -> None:
    writes: list[bool] = []
    monkeypatch.setattr(storage_manager, "set_autostart", lambda enabled: writes.append(enabled) or True)
    monkeypatch.setattr(storage_manager.sys, "platform", "win32")

    class Key:
        def __init__(self, value: str) -> None:
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            pass

    import winreg

    current = f'"x" "{storage_manager._PROJECT_ROOT}\\paperduck.pyw"'
    monkeypatch.setattr(winreg, "OpenKey", lambda *_a: Key(current))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, _name: (key.value, 1))
    assert storage_manager.repair_autostart() and writes == [True]
    current = autostart_command()
    assert not storage_manager.repair_autostart() and writes == [True]  # already current: no write
