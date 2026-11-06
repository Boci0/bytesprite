"""Start with Windows entry: running this file puts its folder (the project root) on sys.path."""

import sys

if "--self-check" in sys.argv:  # the update helper's test launch: exit 1, not the frozen build's error dialog
    try:
        import src.main  # noqa: F401  loads Qt and every module the pet needs
    except BaseException:
        raise SystemExit(1) from None
    raise SystemExit(0)

from src.main import main

raise SystemExit(main())
