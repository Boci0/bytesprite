"""Most-recent-first list of files Paperduck produced (snips, web drops, File Forge outputs)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def format_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


class RecentFiles:
    """Bounded, deduplicated recent output list; files deleted on disk drop out on read."""

    MAX_ENTRIES: int = 10

    def __init__(self) -> None:
        self._items: list[tuple[str, float]] = []

    def add(self, path: Path | str, *, when: float | None = None) -> None:
        key = str(Path(path))
        self._items = [(key, time.time() if when is None else when)] + [
            item for item in self._items if item[0] != key
        ]
        del self._items[self.MAX_ENTRIES :]

    def entries(self) -> list[tuple[Path, float]]:
        """Existing files with their add time, newest first."""
        return [(Path(p), t) for p, t in self._items if Path(p).is_file()]

    def to_dict(self) -> dict[str, Any]:
        return {"files": [[p, t] for p, t in self._items]}

    def load_dict(self, data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            return
        items: list[tuple[str, float]] = []
        for raw in data["files"]:
            if isinstance(raw, list) and len(raw) == 2 and isinstance(raw[0], str):
                try:
                    items.append((raw[0], float(raw[1])))
                except (TypeError, ValueError):
                    continue
        self._items = items[: self.MAX_ENTRIES]
