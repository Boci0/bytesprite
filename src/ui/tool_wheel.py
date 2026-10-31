"""Long-press radial tool wheel: hold the pet, drag onto a slice, release to run it."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import urllib.parse
from dataclasses import dataclass

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget

from src.ui.drop_wheel import RadialDropWheel
from src.ui.theme import TEXT_DIM_HEX, TEXT_HEX


# (label, url, accent): edit this tuple to change the web shortcut slices
WEB_SHORTCUTS: tuple[tuple[str, str, str], ...] = (
    ("YouTube", "https://www.youtube.com", "#EF7D57"),
    ("Gemini", "https://gemini.google.com", "#41A6F6"),
    ("GitHub", "https://github.com", "#C9D1D9"),
)


def google_search_url(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


@dataclass(frozen=True)
class ToolWheelOption:
    """One tool slice; `callback_id` goes through PetWindow's normal tool dispatch."""

    label: str
    subtitle: str
    color_hex: str
    callback_id: str

    @property
    def preview_suffix(self) -> str:
        return self.subtitle


class ToolWheel(RadialDropWheel):
    """Tool launcher wheel sharing the drop wheel's look and open animation.

    Hold-drag-release picks a slice; releasing over the hub or off the ring closes it.
    """

    HUB_LABEL: str = "Close"

    def __init__(
        self,
        *,
        on_selected: Callable[[ToolWheelOption], None],
        always_on_top: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            on_option_dropped=lambda _paths, _opt: None,
            always_on_top=always_on_top,
            enable_watchdog=False,
            parent=parent,
        )
        self._on_selected = on_selected
        self.setAcceptDrops(False)
        self.setWindowTitle("ByteSprite Tool Wheel")

    def open_tools(
        self,
        options: Sequence[ToolWheelOption],
        *,
        window_top_left: QPoint,
        initial_global_cursor: QPoint | None = None,
    ) -> None:
        self.open_for_paths(
            [], options, window_top_left=window_top_left, initial_global_cursor=initial_global_cursor
        )

    def release_at(self, global_pos: QPoint) -> ToolWheelOption | None:
        """End a hold-drag: run the slice under the cursor, or just close."""
        chosen = self.update_hover_from_global(global_pos)
        if chosen is None:
            self.dismiss()
            return None
        self._fire(chosen)  # type: ignore[arg-type]
        return chosen  # type: ignore[return-value]

    def _fire(self, option: ToolWheelOption) -> None:
        self.dismiss()
        self._on_selected(option)

    def _header_preview_text(self) -> tuple[str, QColor]:
        chosen = self.selected_option()
        if chosen is not None:
            return (f"{chosen.label}  ->  {chosen.preview_suffix}", QColor(TEXT_HEX))
        return ("Drag onto a tool, release", QColor(TEXT_DIM_HEX))
