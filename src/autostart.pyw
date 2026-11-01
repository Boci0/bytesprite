"""Start with Windows entry. It lives in src/ because in-place updates replace src/ only, so every copy has it."""

import sys
from pathlib import Path

# Running a file puts its own folder (src/) first on sys.path; swap in the app root so `import src` works
sys.path[0] = str(Path(__file__).resolve().parents[1])

from src.main import main  # noqa: E402

raise SystemExit(main())
