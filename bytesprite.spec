# PyInstaller spec for a standalone one-folder ByteSprite build (prototype; releases still ship the venv bundle).
#   python -m PyInstaller bytesprite.spec --noconfirm     ->  dist/ByteSprite/ByteSprite.exe
# Build from a venv that has requirements.txt plus pyinstaller installed.
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from src.ui.pet_window import export_windows_ico  # noqa: E402

icon = export_windows_ico(Path(workpath) / "bytesprite.ico")

a = Analysis(
    [str(ROOT / "bytesprite.pyw")],
    pathex=[str(ROOT)],
    # Not used by ByteSprite. Keep QtNetwork: QtMultimedia imports it, and sfx swallows that ImportError (no sound)
    excludes=["tkinter", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtPdf", "PyQt6.QtWebEngineCore"],
)
# Raster-only QWidget app: no software OpenGL fallback (20 MB); English UI: no Qt translations (7 MB)
a.binaries = [b for b in a.binaries if not b[0].lower().endswith("opengl32sw.dll")]
a.datas = [d for d in a.datas if "translations" not in d[0].replace("\\", "/").split("/")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ByteSprite",
    icon=str(icon),
    console=False,  # GUI subsystem, like pythonw.exe
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ByteSprite", upx=False)
