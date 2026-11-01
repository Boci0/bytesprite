"""AI Assist chat: a pill input bar with the conversation growing above it.

One window for everything: chatting, attachments (chips in the bar: images are sent as images plus their offline
Windows OCR text, text files by content) and the Setup page behind the gear. Nothing is sent until the
user presses Enter or the send button. Replies are plain text, never rendered or acted on.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QGuiApplication, QImage,
                         QKeyEvent, QPainter, QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QAbstractButton, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget,
                             QVBoxLayout, QWidget)

from src.tools.ai_assist import (BRAVE_KEY_PAGE, BRAVE_URL, KEY_PAGES, MAX_INPUT_CHARS, PRESETS,
                                 THINKING_LEVELS, AiAssistPlugin, AiJob, AiReply, ModelInfo, ToolHost, ask, chat_messages, delete_key, list_models, load_key, save_key, valid_key,
                                 valid_model, validate_base_url)
from src.ui.context_menu import style_menu
from src.ui.quick_tools import OcrJob
from src.ui.sfx import play_cue
from src.ui.theme import (DANGER_HEX, FONT_FAMILY, GLASS_RAISED_RGBA, GLASS_RGBA, HAIRLINE_RGBA, HOVER_RGBA, TEXT_DIM_HEX,
                          TEXT_HEX, qcolor, rgba_css, ui_font)

MAX_FILE_BYTES = 512 * 1024
MAX_ATTACHMENTS = 4
MAX_IMAGE_SIDE = 1568  # longest side sent to vision models: bigger only costs tokens
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# A saved file with these would run rather than open, so its row offers only Show in folder
_RUNNABLE_SUFFIXES = {".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".exe", ".com",
                      ".scr", ".msi", ".lnk", ".url", ".reg", ".py", ".pyw", ".jar", ".cpl", ".msc", ".pif"}
_FRIENDLY_ERRORS = {
    "bad key": "The API key was rejected. Check it in Setup (gear).",
    "no balance": "Your balance with the provider is empty. Top it up, then try again.",
    "rate limited": "The provider is rate limiting you. Wait a moment and try again.",
    "bad model or url": "Model or URL not found. Check them in Setup (gear).",
    "network": "Could not reach the provider. Check your connection.",
    "forbidden": "The provider refused this key for that model.",
    "bad request": "The provider rejected the request. If Thinking is on, this model may not support it: set it Off.",
    "cut off": "The model spent its whole token budget thinking. Try a lower Thinking level.",
}
_FIELD_CSS = (
    f"background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; border: 1px solid {rgba_css(HAIRLINE_RGBA)};"
    " border-radius: 6px; padding: 4px 6px;"
)
_BUTTON_CSS = (
    f"QPushButton {{ color: {TEXT_HEX}; background: transparent; border: 1px solid {rgba_css(HAIRLINE_RGBA)};"
    " border-radius: 6px; padding: 4px 12px; }"
    f" QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; }}"
)


def image_data_url(image: QImage) -> str:
    if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
        image = image.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return "data:image/png;base64," + bytes(data.toBase64().data()).decode("ascii")


def reply_details(reply: AiReply, asked_model: str, thinking: str, name: str = "") -> str:
    """One dim line under a reply: the model that answered (and the alias it came through) with its listed
    name, e.g. the version behind an alias like deepseek-flash, then thinking and tokens."""
    served = reply.model or asked_model
    parts = [(served if served == asked_model else f"{asked_model} > {served}")
             + (f" ({name})" if name and name != served else "")]
    if thinking != "off":
        parts.append(f"thinking {thinking}")
    if reply.total_tokens:
        split = [f"{reply.prompt_tokens:,} in"]
        if reply.reasoning_tokens:
            split.append(f"{reply.reasoning_tokens:,} thinking")
        split.append(f"{max(0, reply.completion_tokens - reply.reasoning_tokens):,} answer")
        parts.append(f"{reply.total_tokens:,} tokens ({', '.join(split)})")
    if reply.cut_off:
        parts.append("cut off at the limit")
    return "  -  ".join(parts)


def model_label(model: ModelInfo) -> str:
    label = f"{model.name}  ({model.id})" if model.name and model.name != model.id else model.id
    return label + ("  -  sees images" if model.vision else "")


def friendly_error(error: str) -> str:
    return _FRIENDLY_ERRORS.get(error, f"Request failed ({error}).")


def read_text_file(path: Path) -> str:
    """Content of a small text file; raises ValueError('too large' / 'not text')."""
    with path.open("rb") as f:  # never pull a multi-GB file into RAM on the GUI thread
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("too large")
    if b"\x00" in data[:8192]:
        raise ValueError("not text")
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("not text")


def _size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


@dataclass
class Attachment:
    kind: str  # "image" or "file"
    name: str
    text: str = ""
    detail: str = ""
    pending: bool = False
    thumb: QPixmap | None = None
    image_url: str = ""  # PNG data URL, sent as an image part

    def as_prompt(self) -> str:
        label = "Image (attached; its text read by OCR)" if self.kind == "image" else "File"
        return f"[{label}: {self.name}]\n{self.text}"


class _Frosted(QFrame):
    """Rounded opaque graphite panel with a white hairline, like the menus and HUD."""

    def __init__(self, radius: int, raised: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radius, self._raised = radius, raised

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(qcolor(HAIRLINE_RGBA), 1))
        painter.setBrush(qcolor(GLASS_RAISED_RGBA if self._raised else GLASS_RGBA))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
        painter.end()


class IconButton(QAbstractButton):
    """Small round button with a painted line icon (plus, send, gear, close): no font or emoji glyphs."""

    def __init__(self, kind: str, tip: str, size: int = 32, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = self.width() / 32.0
        painter.scale(s, s)
        if self.underMouse() and self.isEnabled():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(HOVER_RGBA))
            painter.drawEllipse(QRectF(1, 1, 30, 30))
        color = QColor(TEXT_HEX if self.isEnabled() else TEXT_DIM_HEX)
        painter.setPen(QPen(color, 1.6 / max(s, 0.6), cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "plus":
            painter.drawLine(QPointF(16, 10), QPointF(16, 22))
            painter.drawLine(QPointF(10, 16), QPointF(22, 16))
        elif self.kind == "send":  # return arrow, as on a keyboard
            path = QPainterPath(QPointF(21, 10))
            path.lineTo(21, 19)
            path.lineTo(11, 19)
            painter.drawPath(path)
            painter.drawLine(QPointF(11, 19), QPointF(14.5, 15.5))
            painter.drawLine(QPointF(11, 19), QPointF(14.5, 22.5))
        elif self.kind == "gear":
            painter.drawEllipse(QPointF(16, 16), 3.2, 3.2)
            painter.drawEllipse(QPointF(16, 16), 6.4, 6.4)
            for i in range(8):
                painter.save()
                painter.translate(16, 16)
                painter.rotate(i * 45)
                painter.drawLine(QPointF(0, -6.4), QPointF(0, -8.6))
                painter.restore()
        elif self.kind == "close":
            painter.drawLine(QPointF(11.5, 11.5), QPointF(20.5, 20.5))
            painter.drawLine(QPointF(20.5, 11.5), QPointF(11.5, 20.5))
        elif self.kind == "new":  # compose: open square with a pencil stroke
            path = QPainterPath(QPointF(16, 10))
            for x, y in ((10, 10), (10, 22), (22, 22), (22, 16)):
                path.lineTo(x, y)
            painter.drawPath(path)
            painter.drawLine(QPointF(15, 17), QPointF(22, 10))
        elif self.kind == "stop":
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(11.5, 11.5, 9, 9), 2, 2)
        painter.end()


class _Separator(QWidget):
    """Hairline like QMenu::separator (1px, 6px side margins)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(9)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(QRect(6, 4, self.width() - 12, 1), qcolor(HAIRLINE_RGBA))
        painter.end()


class _FileGlyph(QWidget):
    """Folded-corner document icon for file chips."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(TEXT_DIM_HEX), 1.4, join=Qt.PenJoinStyle.RoundJoin))
        page = QPainterPath(QPointF(11, 6))
        for x, y in ((21, 6), (27, 12), (27, 30), (11, 30), (11, 6)):
            page.lineTo(x, y)
        painter.drawPath(page)
        painter.drawPolyline([QPointF(21, 6), QPointF(21, 12), QPointF(27, 12)])
        for y in (18, 22, 26):
            painter.drawLine(QPointF(15, y), QPointF(23, y))
        painter.end()


class Chip(_Frosted):
    """One attachment: thumbnail or document icon, name, status, and an x when removable."""

    def __init__(self, item: Attachment, on_remove: Callable[[], None] | None = None) -> None:
        super().__init__(8, raised=True)
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 2 if on_remove else 8, 4)
        row.setSpacing(6)
        if item.thumb is not None:
            thumb = QLabel()
            thumb.setFixedSize(36, 36)
            thumb.setPixmap(item.thumb.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                              Qt.TransformationMode.SmoothTransformation).copy(0, 0, 36, 36))
            row.addWidget(thumb)
        else:
            row.addWidget(_FileGlyph())
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(item.name if len(item.name) <= 18 else item.name[:15] + "...")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setFont(ui_font(8.5, bold=True))
        name.setStyleSheet(f"color: {TEXT_HEX};")
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setFont(ui_font(7.5))
        text.addWidget(name)
        text.addWidget(self.status)
        row.addLayout(text)
        if on_remove is not None:
            close = IconButton("close", "Remove", size=22)
            close.clicked.connect(lambda _checked=False: on_remove())  # never leak `checked` into on_remove's defaults
            row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        bad = item.detail.startswith("Can't")
        self.status.setText("reading..." if item.pending else item.detail)
        self.status.setStyleSheet(f"color: {DANGER_HEX if bad else TEXT_DIM_HEX};")


class _Draft(QPlainTextEdit):
    """Message box: Enter sends, Shift+Enter adds a line, grows to 5 lines; pasted images or files attach."""

    submitted = pyqtSignal()
    attach_mime = pyqtSignal(QMimeData)
    MAX_LINES = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(ui_font(10.5))
        self.setPlaceholderText("Ask anything")
        self.setStyleSheet(f"background: transparent; color: {TEXT_HEX};")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self._fit)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if (event is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source: QMimeData | None) -> bool:
        return source is not None and (source.hasImage() or source.hasUrls() or super().canInsertFromMimeData(source))

    def insertFromMimeData(self, source: QMimeData | None) -> None:
        if source is not None and (source.hasImage() or any(u.isLocalFile() for u in source.urls())):
            self.attach_mime.emit(source)
            return
        super().insertFromMimeData(source)

    def _fit(self) -> None:
        line = self.fontMetrics().lineSpacing()
        lines = max(1, min(self.MAX_LINES, int(self.document().size().height())))
        self.setFixedHeight(lines * line + 10)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if lines >= self.MAX_LINES
                                        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class _ClickLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class AiChat(QWidget):
    """The chat bar. `thinking_changed` lets the pet animate while a reply is pending."""

    thinking_changed = pyqtSignal(bool)

    WIDTH = 380
    THREAD_MAX_H = 320
    GAP = 6  # between the pet's sprite and the card
    MAX_H = 560  # tallest card: header, a full thread or Setup, chips and a 5-line draft (picks its side)

    def __init__(self, plugin: AiAssistPlugin, on_attach_screen: Callable[[], None] | None = None,
                 tool_host: ToolHost | None = None) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setWindowTitle("ByteSprite AI")
        self.setFixedWidth(self.WIDTH)
        self._plugin = plugin
        self._on_attach_screen = on_attach_screen
        self._tool_host = tool_host  # PC tools the model may call (None: web tools only)
        self._history: list[dict[str, str]] = []
        self._attachments: list[Attachment] = []
        self._chips: list[Chip] = []
        self._ocr_jobs: list[OcrJob] = []
        self._job: AiJob | None = None
        self._auto_send = False  # a macro's `ask` step: send once images are read and no reply is pending
        self._allowed: set[str] = set()  # what the user allowed for this chat: "mcp:<server>", "edit_file", ...
        self._pending: Callable[[str | None], None] | None = None  # the open Allow/Deny prompt
        self._models_job: AiJob | None = None
        self._models: dict[str, list[ModelInfo]] = {}  # base URL -> its /models list, fetched on first open
        self._testing = False
        self._asked = ("", "off")  # model and thinking level of the request in flight
        self._anchor: QRect | None = None  # the pet's sprite, in global coordinates
        self._above: bool | None = None  # which side of the sprite the card sits on, until the next open_near
        self._dots = 0
        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(350)
        self._dots_timer.timeout.connect(self._tick_dots)
        self._thinking_row: QWidget | None = None
        self._thinking_label: QLabel | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        # One card styled like the right-click menu: header, conversation or setup, then the input
        card = self._card = _Frosted(10)
        root.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(12, 2, 0, 0)
        header.setSpacing(0)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._name = QLabel()
        self._name.setTextFormat(Qt.TextFormat.RichText)
        self._title = _ClickLabel()
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title.setToolTip("Switch model or thinking level")
        self._title.clicked.connect(self._open_model_menu)
        self._title.setFont(ui_font(8.5))
        self._title.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        titles.addWidget(self._name)
        titles.addWidget(self._title)
        header.addLayout(titles, 1)
        self._new_btn = IconButton("new", "New chat", size=28)
        self._new_btn.clicked.connect(self.new_chat)
        self._gear = IconButton("gear", "Setup: provider, model, API key", size=28)
        self._gear.clicked.connect(self.toggle_setup)
        close_btn = IconButton("close", "Close (Esc)", size=28)
        close_btn.clicked.connect(self.hide)
        for button in (self._new_btn, self._gear, close_btn):
            header.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addWidget(_Separator())

        # Conversation or setup, only shown once there is something in it
        self._panel = QWidget()
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(4)
        self._stack = QStackedWidget()
        self._thread = QWidget()
        self._thread_layout = QVBoxLayout(self._thread)
        self._thread_layout.setContentsMargins(6, 2, 6, 2)
        self._thread_layout.setSpacing(8)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._thread)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            " QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            f" QScrollBar::handle:vertical {{ background: {rgba_css(HAIRLINE_RGBA)}; border-radius: 3px;"
            " min-height: 24px; }"
            " QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page"
            " { background: none; height: 0; }")
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._build_setup())
        panel_layout.addWidget(self._stack)
        panel_layout.addWidget(_Separator())
        layout.addWidget(self._panel)

        # Input: attachment chips (when any) above a raised field holding + / draft / send
        self._chip_box = QWidget()
        self._chip_row = QHBoxLayout(self._chip_box)
        self._chip_row.setContentsMargins(2, 2, 2, 0)
        self._chip_row.setSpacing(6)
        self._chip_row.addStretch(1)
        self._chip_box.hide()
        layout.addWidget(self._chip_box)
        field = _Frosted(8, raised=True)
        bar = QHBoxLayout(field)
        bar.setContentsMargins(2, 2, 2, 2)
        bar.setSpacing(2)
        self._plus = IconButton("plus", "Attach: clipboard, screen region or file", size=28)
        self._plus.clicked.connect(self._attach_menu)
        self._draft = _Draft()
        self._draft.submitted.connect(self.send)
        self._draft.attach_mime.connect(self.attach_mime)
        self._send_btn = IconButton("send", "Send (Enter)", size=28)
        self._send_btn.clicked.connect(self._send_or_stop)
        bar.addWidget(self._plus, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addWidget(self._draft, 1, Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(field)
        self._draft.textChanged.connect(self._relayout)
        self._refresh_title()
        self._panel.hide()

    # ------------------------------------------------------------------ setup page

    def _build_setup(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(2, 4, 8, 2)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._provider = QComboBox()
        for key, (label, _url, _model) in PRESETS.items():
            self._provider.addItem(label, key)
        self._provider.addItem("Custom (OpenAI-compatible)", "custom")
        self._provider.currentIndexChanged.connect(self._on_provider_picked)
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://... or http://localhost:port/v1")
        self._model = QLineEdit()
        self._model.setPlaceholderText("model name, e.g. deepseek-flash")
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setToolTip("Stored in Windows Credential Manager, never in state.json")
        self._get_key = QPushButton("Get a key")
        self._get_key.clicked.connect(self._open_key_page)
        self._remove_key = QPushButton("Remove")
        self._remove_key.clicked.connect(lambda: self._forget_key(False))
        key_row = QHBoxLayout()
        key_row.addWidget(self._key, 1)
        key_row.addWidget(self._get_key)
        key_row.addWidget(self._remove_key)
        self._search_key = QLineEdit()
        self._search_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._search_key.setToolTip("Optional Brave Search API key (free tier). A saved DeepSeek key searches through DeepSeek first; with neither, web search uses DuckDuckGo, which often blocks it.")
        get_search = QPushButton("Get a key")
        get_search.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(BRAVE_KEY_PAGE)))
        self._remove_search = QPushButton("Remove")
        self._remove_search.clicked.connect(lambda: self._forget_key(True))
        search_row = QHBoxLayout()
        search_row.addWidget(self._search_key, 1)
        search_row.addWidget(get_search)
        search_row.addWidget(self._remove_search)
        self._thinking = QComboBox()
        for level in THINKING_LEVELS:
            self._thinking.addItem(level.title(), level)
        self._thinking.setToolTip("Think before answering: slower and more tokens, better at math and science")
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._save_btn = QPushButton("Save and test")
        self._save_btn.clicked.connect(self._save_and_test)
        for widget in (self._provider, self._url, self._model, self._key, self._search_key, self._thinking):
            widget.setStyleSheet(_FIELD_CSS)
        for widget in (self._get_key, get_search, self._remove_key, self._remove_search, self._save_btn):
            widget.setStyleSheet(_BUTTON_CSS)
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
        for row_label, field in (("Provider", self._provider), ("Base URL", self._url), ("Model", self._model)):
            form.addRow(self._form_label(row_label), field)
        form.addRow(self._form_label("API key"), key_row)
        form.addRow(self._form_label("Search key"), search_row)
        form.addRow(self._form_label("Thinking"), self._thinking)
        form.addRow("", self._save_btn)
        form.addRow("", self._status)
        return page

    @staticmethod
    def _form_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(8.5))
        label.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        return label

    def _refresh_keys(self) -> None:
        """Short placeholders (they must fit beside two buttons) and Remove only when a key is saved."""
        try:
            has_key = bool(load_key(validate_base_url(self._url.text())))
        except ValueError:
            has_key = False
        has_search = bool(load_key(BRAVE_URL))
        self._key.setPlaceholderText("Saved" if has_key else "Paste API key")
        self._search_key.setPlaceholderText("Saved" if has_search else "Optional")
        self._remove_key.setEnabled(has_key)
        self._remove_search.setEnabled(has_search)

    def _forget_key(self, search: bool) -> None:
        try:
            url = BRAVE_URL if search else validate_base_url(self._url.text())
        except ValueError:
            return
        delete_key(url)
        self._refresh_keys()
        self._refresh_title()
        self._set_status("Search key removed." if search else "API key removed.")

    def _load_setup(self) -> None:
        p = self._plugin
        self._provider.blockSignals(True)
        self._provider.setCurrentIndex(max(0, self._provider.findData(p.provider)))
        self._provider.blockSignals(False)
        self._url.setText(p.base_url)
        self._url.setEnabled(p.provider == "custom")
        self._model.setText(p.model)
        self._key.clear()
        self._search_key.clear()
        self._refresh_keys()
        self._get_key.setEnabled(p.provider in KEY_PAGES)
        self._thinking.setCurrentIndex(max(0, self._thinking.findData(p.thinking)))

    def _on_provider_picked(self) -> None:
        key = self._provider.currentData()
        if key in PRESETS:
            _label, url, model = PRESETS[key]
            self._url.setText(url)
            self._model.setText(model)
        self._url.setEnabled(key == "custom")
        self._get_key.setEnabled(key in KEY_PAGES)
        self._refresh_keys()

    def _open_key_page(self) -> None:
        page = KEY_PAGES.get(self._provider.currentData())
        if page:
            QDesktopServices.openUrl(QUrl(page))

    def _set_status(self, text: str, ok: bool | None = None) -> None:
        color = TEXT_DIM_HEX if ok is None else ("#38B764" if ok else DANGER_HEX)
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(text)
        self._relayout()

    def _save_and_test(self) -> None:
        if self._job is not None:
            return
        provider = self._provider.currentData()
        try:
            url = validate_base_url(self._url.text())
        except ValueError:
            self._set_status("Use an https:// URL (plain http only for localhost).", False)
            return
        model = self._model.text().strip()
        if not valid_model(model):
            self._set_status("Enter a model name (letters, digits, - _ . : / only).", False)
            return
        search_key = self._search_key.text().strip()
        if search_key:
            if not valid_key(search_key) or not save_key(BRAVE_URL, search_key):
                self._set_status("That search key could not be saved.", False)
                return
            self._search_key.clear()
        key = self._key.text().strip()
        if key:
            if not valid_key(key):
                self._set_status("That key does not look right (no spaces, 8 to 512 characters).", False)
                return
            if not save_key(url, key):
                self._set_status("Could not save the key to Windows Credential Manager.", False)
                return
            self._key.clear()
        elif not load_key(url):
            self._set_status("Paste your API key first.", False)
            return
        p = self._plugin
        if provider in PRESETS:
            p.set_provider(provider)
        else:
            p.set_custom_url(url)
        p.model, p.thinking = model, self._thinking.currentData()
        self._refresh_keys()
        self._refresh_title()
        self._set_status("Saved. Testing with a tiny request...")
        self._testing = True
        self._start_job([{"role": "user", "content": "Reply with the single word OK."}])

    # ------------------------------------------------------------------ attachments

    def _attach_menu(self) -> None:
        menu = style_menu(QMenu(self))
        cb = QGuiApplication.clipboard()
        mime = cb.mimeData() if cb is not None else None
        has_clip = mime is not None and (mime.hasImage() or mime.hasUrls() or bool(mime.text().strip()))
        clip = menu.addAction("Clipboard")
        clip.setEnabled(has_clip)
        clip.triggered.connect(lambda _c=False: self.attach_mime(mime) if mime is not None else None)
        region = menu.addAction("Screen region")
        region.setEnabled(self._on_attach_screen is not None)
        region.triggered.connect(lambda _c=False: self._on_attach_screen() if self._on_attach_screen else None)
        menu.addAction("File...").triggered.connect(lambda _c=False: self._pick_files())
        menu.exec(self._plus.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))
        menu.deleteLater()

    def _pick_files(self) -> None:
        paths, _flt = QFileDialog.getOpenFileNames(self, "ByteSprite AI - Attach Files")
        for path in paths:
            self.attach_path(Path(path))

    def attach_mime(self, mime: QMimeData) -> None:
        """Clipboard, paste or drop: images and local files become chips, plain text goes into the draft."""
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.attach_image(image, "Pasted image")
                return
        files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        if files:
            for path in files:
                self.attach_path(path)
            return
        self.insert_text(mime.text())

    def attach_path(self, path: Path) -> None:
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            image = QImage(str(path))
            if not image.isNull():
                self.attach_image(image, path.name)
                return
        item = Attachment("file", path.name)
        try:
            item.text = read_text_file(path)
            lines = item.text.count("\n") + 1
            item.detail = f"{_size_text(path.stat().st_size)}, {lines} lines"
        except ValueError as exc:
            item.detail = "Can't attach: over 512 KB" if str(exc) == "too large" else "Can't read: not a text file"
        except OSError:
            item.detail = "Can't read this file"
        self._add_attachment(item)

    def attach_image(self, image: QImage, name: str) -> None:
        """Image chips are sent as images, plus their offline Windows OCR text for models without vision."""
        item = Attachment("image", name, pending=True, thumb=QPixmap.fromImage(image), image_url=image_data_url(image))
        if not self._add_attachment(item):
            return
        job = OcrJob(self)
        job.finished.connect(lambda text, j=job, it=item: self._on_ocr(j, it, text))
        self._ocr_jobs.append(job)
        job.start(image)

    def _on_ocr(self, job: OcrJob, item: Attachment, text: str) -> None:
        if job in self._ocr_jobs:
            self._ocr_jobs.remove(job)
        job.deleteLater()
        item.pending = False
        item.text = text.strip()
        words = len(item.text.split())
        item.detail = f"{words} words read" if words else "Image, no text found"
        for chip in self._chips:
            if chip.item is item:
                chip.refresh()
        self._try_auto_send()

    def _add_attachment(self, item: Attachment) -> bool:
        if len(self._attachments) >= MAX_ATTACHMENTS:
            return False
        self._attachments.append(item)
        chip = Chip(item, on_remove=lambda it=item: self._remove_attachment(it))
        self._chips.append(chip)
        self._chip_row.insertWidget(self._chip_row.count() - 1, chip)
        self._chip_box.show()
        self._relayout()
        self._draft.setFocus()
        return True

    def _remove_attachment(self, item: Attachment) -> None:
        for chip in [c for c in self._chips if c.item is item]:
            self._chips.remove(chip)
            chip.deleteLater()
        self._attachments = [a for a in self._attachments if a is not item]
        self._chip_box.setVisible(bool(self._attachments))
        QTimer.singleShot(0, self._relayout)

    def _clear_attachments(self) -> None:
        for chip in self._chips:
            chip.deleteLater()
        self._chips.clear()
        self._attachments.clear()
        self._chip_box.hide()

    def insert_text(self, text: str) -> None:
        """Add text to the draft (never sends): the user still reviews it and presses Enter."""
        text = text.strip()[:MAX_INPUT_CHARS]
        if not text:
            return
        current = self._draft.toPlainText()
        self._draft.setPlainText(f"{current}\n\n{text}" if current.strip() else text)
        cursor = self._draft.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._draft.setTextCursor(cursor)
        self._draft.setFocus()

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is not None and event.mimeData() is not None and (
                event.mimeData().hasImage() or event.mimeData().hasUrls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is not None and event.mimeData() is not None:
            self.attach_mime(event.mimeData())
            event.acceptProposedAction()

    # ------------------------------------------------------------------ chat

    def _ready(self) -> bool:
        return bool(self._plugin.model) and self._plugin.has_key()

    def send(self) -> None:
        text = self._draft.toPlainText().strip()
        usable = [a for a in self._attachments if a.text or a.image_url]
        if (not text and not usable) or self._job is not None:
            return
        if any(a.pending for a in self._attachments):
            self._add_bubble("error", "Still reading an image. Send again in a moment.")
            return
        if not self._ready():
            self.show_setup("Set up a provider, model and API key first. Nothing has been sent.")
            return
        content = "\n\n".join([text, *(a.as_prompt() for a in usable)]).strip()[:MAX_INPUT_CHARS]
        self._draft.clear()
        self._history.append({"role": "user", "content": content, "images": [a.image_url for a in usable if a.image_url]})
        self._add_bubble("user", text, attachments=usable)
        self._clear_attachments()
        self._thinking_row, self._thinking_label = self._add_row("assistant", "")
        self._dots_timer.start()
        self._tick_dots()
        play_cue("send")
        self._start_job(chat_messages(self._history))

    def _send_or_stop(self) -> None:
        if self._job is not None and not self._testing:
            self.stop()
        else:
            self.send()

    def stop(self) -> None:
        """Drop the pending reply (the request may still finish, but its answer is ignored)."""
        if self._job is None or self._testing:
            return
        if self._pending is not None:  # the worker is waiting on it
            self._pending(None)
        job, self._job = self._job, None
        job.finished.disconnect(self._on_done)
        job.finished.connect(job.deleteLater)  # its thread still emits: deleting it now races that emit
        self._on_done(None, "stopped")

    def _start_job(self, messages: list[dict]) -> None:
        p = self._plugin
        url, key, model, thinking = p.base_url, load_key(p.base_url), p.model, p.thinking
        self._asked = (model, thinking)
        job = self._job = AiJob(self)
        job.finished.connect(self._on_done)
        host = None if self._testing else self._tool_host
        job.start(lambda: ask(url, key, model, messages, thinking=thinking, host=host))
        self._set_busy(True)

    def _on_done(self, reply: AiReply | None, error: str) -> None:
        if self._job is not None:
            self._job.deleteLater()
        self._job = None
        self._set_busy(False)
        if self._testing:
            self._testing = False
            if error or reply is None:
                self._set_status(friendly_error(error), False)
            else:
                served = reply.model if reply is not None and reply.model else self._asked[0]
                self._set_status(f"Connected: {served} answered. You can chat now.", True)
                if self._plugin.base_url not in self._models:
                    self._fetch_models(show=False)
                self._refresh_title()
                QTimer.singleShot(900, self._leave_setup_if_ok)
            return
        self._dots_timer.stop()
        if self._thinking_row is not None:
            self._thinking_row.deleteLater()
            self._thinking_row = self._thinking_label = None
        for path in getattr(self._tool_host, "take_saved", list)():
            self._add_saved_row(path)
        if error or reply is None:
            failed = self._history.pop() if self._history and self._history[-1]["role"] == "user" else None
            if error != "stopped":
                play_cue("error")
                self._add_bubble("error", friendly_error(error))
            else:
                self._relayout()
            if failed and not self._draft.toPlainText():
                self._draft.setPlainText(failed["content"])  # nothing lost: edit and press Enter again
            return
        self._history.append({"role": "assistant", "content": reply.text, "tools": reply.tools})
        self._plugin.record(reply.total_tokens)
        row, _label = self._add_row("assistant", reply.text)
        self._add_details(row, reply)
        play_cue("reply")
        self._refresh_title()
        self._try_auto_send()

    def send_when_ready(self) -> None:
        self._auto_send = True
        self._try_auto_send()

    def _try_auto_send(self) -> None:
        if self._auto_send and self._job is None and not any(a.pending for a in self._attachments):
            self._auto_send = False
            self.send()

    def _add_details(self, row: QWidget, reply: AiReply) -> None:
        """Dim line under a reply, plus a Show reasoning toggle when the model returned its thinking."""
        layout = row.layout()
        info = QLabel(reply_details(reply, *self._asked, name=self._model_name(reply.model or self._asked[0])))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        info.setFont(ui_font(7.5))
        info.setStyleSheet(f"color: {TEXT_DIM_HEX}; padding: 0 2px;")
        layout.addWidget(info)
        if not reply.reasoning:
            return
        toggle = _ClickLabel(f"Show reasoning ({reply.reasoning_tokens:,} tokens)" if reply.reasoning_tokens
                             else "Show reasoning")
        toggle.setFont(ui_font(7.5))
        toggle.setStyleSheet(f"color: {TEXT_HEX}; padding: 0 2px; text-decoration: underline;")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        body = QLabel(reply.reasoning)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setFont(ui_font(8))
        body.setStyleSheet(f"color: {TEXT_DIM_HEX}; padding: 4px 6px; border-left: 2px solid {rgba_css(HAIRLINE_RGBA)};")
        body.hide()
        shown_text, hidden_text = "Hide reasoning", toggle.text()

        def flip() -> None:
            body.setHidden(not body.isHidden())  # not isVisible(): that is False while the card itself is hidden
            toggle.setText(hidden_text if body.isHidden() else shown_text)
            QTimer.singleShot(0, self._relayout)

        toggle.clicked.connect(flip)
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(body)

    def _add_saved_row(self, path: Path) -> None:
        """A file the model saved (the user picked where): Open, unless it would run as a program, and Show."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(10)
        name = QLabel(f"Saved {path.name}")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setToolTip(str(path))
        name.setFont(ui_font(8))
        name.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        layout.addWidget(name)
        links = [("Show in folder", lambda: subprocess.Popen(f'explorer /select,"{path}"'))]
        if path.suffix.lower() not in _RUNNABLE_SUFFIXES:
            links.insert(0, ("Open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))))
        for text, action in links:
            link = _ClickLabel(text)
            link.setFont(ui_font(8))
            link.setStyleSheet(f"color: {TEXT_HEX}; text-decoration: underline;")
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.clicked.connect(action)
            layout.addWidget(link)
        layout.addStretch(1)
        self._thread_layout.addWidget(row)

    # ------------------------------------------------------------------ model switcher

    def _open_model_menu(self) -> None:
        """Header click: the provider's real model list (GET /models, fetched once per URL) and thinking levels."""
        p = self._plugin
        if p.base_url in self._models or not p.has_key():
            self._show_model_menu(self._models.get(p.base_url, []), "" if p.has_key() else "no key")
            return
        self._fetch_models(show=True)

    def _fetch_models(self, show: bool) -> None:
        """GET /models in the background (free, no tokens); `show` opens the menu when it lands. Without the
        menu it only fills in display names, since replies name just the alias (deepseek-flash, not its version)."""
        p = self._plugin
        if self._models_job is not None or not p.has_key():
            return
        url, key = p.base_url, load_key(p.base_url)
        job = self._models_job = AiJob(self)
        job.finished.connect(lambda models, error, u=url: self._on_models(u, models, error, show))
        job.start(lambda: list_models(url, key))
        if show:
            self._title.setText("Loading models...")

    def _model_name(self, model_id: str) -> str:
        """The provider's display name for a model id ("" until the list has loaded or when it gives none)."""
        return next((m.name for m in self._models.get(self._plugin.base_url, []) if m.id == model_id), "")

    def _on_models(self, url: str, models: list[ModelInfo] | None, error: str, show: bool = True) -> None:
        if self._models_job is not None:
            self._models_job.deleteLater()
        self._models_job = None
        if models:
            self._models[url] = models
        self._refresh_title()
        if show and url == self._plugin.base_url and self.isVisible():
            self._show_model_menu(models or [], error or ("" if models else "empty list"))

    def _show_model_menu(self, models: list[ModelInfo], error: str = "") -> None:
        p = self._plugin
        menu = style_menu(QMenu(self))
        menu.addSection("Model")
        if p.model and all(m.id != p.model for m in models):
            models = [ModelInfo(p.model), *models]
        for model in models:
            action = menu.addAction(model_label(model))
            action.setCheckable(True)
            action.setChecked(model.id == p.model)
            action.triggered.connect(lambda _c=False, m=model.id: self._pick(model=m))
        if error:
            menu.addAction(f"Could not list models ({friendly_error(error)})").setEnabled(False)
        menu.addSection("Thinking")
        for level in THINKING_LEVELS:
            action = menu.addAction(level.title())
            action.setCheckable(True)
            action.setChecked(level == p.thinking)
            action.triggered.connect(lambda _c=False, t=level: self._pick(thinking=t))
        menu.addSeparator()
        menu.addAction("Setup...").triggered.connect(lambda _c=False: self.show_setup())
        menu.exec(self._title.mapToGlobal(QPoint(0, self._title.height() + 2)))
        menu.deleteLater()

    def _pick(self, model: str | None = None, thinking: str | None = None) -> None:
        p = self._plugin
        if model is not None and valid_model(model):
            p.model = model
        if thinking in THINKING_LEVELS:
            p.thinking = thinking
        self._refresh_title()

    def _leave_setup_if_ok(self) -> None:
        if self._stack.currentIndex() == 1 and self._ready():
            self._stack.setCurrentIndex(0)
            self._panel.setVisible(bool(self._history))
            self._relayout()
            self._draft.setFocus()

    def _tick_dots(self) -> None:
        self._dots = self._dots % 3 + 1
        if self._thinking_label is not None:
            self._thinking_label.setText("." * self._dots)

    def _add_row(self, role: str, text: str, attachments: list[Attachment] | None = None) -> tuple[QWidget, QLabel]:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        align = Qt.AlignmentFlag.AlignRight if role == "user" else Qt.AlignmentFlag.AlignLeft
        if attachments:
            chips = QHBoxLayout()
            chips.setSpacing(6)
            chips.addStretch(1)
            for item in attachments:
                chips.addWidget(Chip(item))
            layout.addLayout(chips)
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setFont(ui_font(8.5 if role == "error" else 9.5))
        if role == "user":
            label.setStyleSheet(f"color: {TEXT_HEX}; background: {rgba_css(HOVER_RGBA)};"
                                " border-radius: 12px; padding: 6px 10px;")
            # Wrapped labels under an alignment get a guessed width; pin it so the bubble hugs its text
            longest = max((label.fontMetrics().horizontalAdvance(line) for line in text.splitlines()), default=0)
            label.setFixedWidth(min(int(self.WIDTH * 0.72), longest + 26))
            label.setVisible(bool(text))
            layout.addWidget(label, 0, align)
        else:
            label.setStyleSheet(f"color: {DANGER_HEX if role == 'error' else TEXT_HEX}; padding: 2px 2px;")
            layout.addWidget(label)
        self._thread_layout.addWidget(row)
        self._stack.setCurrentIndex(0)
        self._panel.show()
        self._relayout()
        QTimer.singleShot(0, self._relayout_and_scroll)  # wrapped heights settle after the first layout pass
        return row, label

    def _relayout_and_scroll(self) -> None:
        self._relayout()
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_bubble(self, role: str, text: str, attachments: list[Attachment] | None = None) -> QLabel:
        return self._add_row(role, text, attachments)[1]

    def confirm_mcp(self, server: str, tool: str, done: Callable[[bool], None]) -> None:
        """Ask before an MCP server's first tool call in this chat; the worker thread waits for done(allowed)."""
        self.confirm(f"mcp:{server}", f'The model wants to use your MCP server "{server}" ({tool}). '
                     "Allow it for this chat?", f'MCP server "{server}"', done, once=False)

    def confirm(self, key: str, text: str, what: str, done: Callable[[bool], None], once: bool = True) -> None:
        """Allow/Deny row in the chat; the worker thread waits for done(allowed).

        `key` already allowed for this chat answers at once. With `once`, the row offers Allow once, Allow for
        this chat and Deny; without, Allow (for this chat) and Deny.
        """
        if key in self._allowed:
            done(True)
            return
        row, label = self._add_row("assistant", text)
        buttons = QHBoxLayout()
        choices = ([("Allow once", "once"), ("Allow for this chat", "chat")] if once else [("Allow", "chat")]) + [
            ("Deny", None)]
        btns: list[QPushButton] = []

        def answer(choice: str | None) -> None:
            if self._pending is not answer:
                return
            self._pending = None
            if choice == "chat":
                self._allowed.add(key)
            label.setText(f"{text}\n\n{'Denied' if choice is None else 'Allowed'}: {what}"
                          + (" for this chat." if choice == "chat" else "."))
            for btn in btns:
                btn.hide()
            done(choice is not None)

        for title, choice in choices:
            btn = QPushButton(title)
            btn.setStyleSheet(_BUTTON_CSS)
            btn.clicked.connect(lambda _c=False, choice=choice: answer(choice))
            buttons.addWidget(btn)
            btns.append(btn)
        buttons.addStretch(1)
        row.layout().addLayout(buttons)
        self._pending = answer
        self.show()
        self._relayout()

    def new_chat(self) -> None:
        """Fresh start: drops a pending reply, the thread, the draft and any attachments."""
        self.stop()
        self._history.clear()
        self._allowed.clear()
        self._draft.clear()
        self._clear_attachments()
        while self._thread_layout.count():
            item = self._thread_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self._thinking_row = self._thinking_label = None
        self._panel.hide()
        self._refresh_title()
        self._relayout()
        self._draft.setFocus()

    def _set_busy(self, busy: bool) -> None:
        self._send_btn.kind = "stop" if busy and not self._testing else "send"
        self._send_btn.setToolTip("Stop waiting for the reply" if self._send_btn.kind == "stop" else "Send (Enter)")
        self._send_btn.setEnabled(not (busy and self._testing))
        self._send_btn.update()
        self._refresh_title()
        self.thinking_changed.emit(busy)

    def _refresh_title(self) -> None:
        """Menu-style header: name with a status dot, then the model, thinking level and host (click to switch)."""
        p = self._plugin
        if self._job is not None:
            state, color = "Thinking", "#E8D29F"
        elif self._ready():
            state, color = "Ready", "#9FD3A8"
        else:
            state, color = "Setup needed", "#E6A0A0"
        self._name.setText(
            f'<span style="font-family:\'{FONT_FAMILY}\'; font-size:10.5pt; font-weight:600; color:{TEXT_HEX};">'
            f'ByteSprite AI</span>&nbsp;&nbsp;<span style="font-family:\'{FONT_FAMILY}\'; font-size:9pt;'
            f' color:{color};">&#9679; {state}</span>')
        thinking = "" if p.thinking == "off" else f", thinking {p.thinking}"
        name = self._model_name(p.model)
        host = f" @ {p.host}" if p.provider == "custom" else ""  # a preset's host is in the tooltip; it cut the line
        self._title.setText(f"{name or p.model or 'No model'}{thinking}{host}  \u25BE")
        self._title.setToolTip((f"{name}, requested as {p.model}" if name and name != p.model else p.model)
                               + f" @ {p.host}. Switch model or thinking level")

    def show_setup(self, message: str = "") -> None:
        self._load_setup()
        self._stack.setCurrentIndex(1)
        self._panel.show()
        self._set_status(message)
        self._relayout()

    def toggle_setup(self) -> None:
        if self._panel.isVisible() and self._stack.currentIndex() == 1:
            self._stack.setCurrentIndex(0)
            self._panel.setVisible(bool(self._history))
            self._relayout()
        else:
            self.show_setup()

    # ------------------------------------------------------------------ window

    def open_near(self, anchor: QRect | None = None) -> None:
        """Show beside the pet's sprite (above it when there is room, else below), like its menu."""
        if anchor is not None:
            self._anchor = QRect(anchor)
        self._above = None
        if not self._ready():
            self.show_setup("Pick a provider, paste your API key and press Save and test.")
        if self._ready() and self._plugin.base_url not in self._models:
            self._fetch_models(show=False)
        self._refresh_title()
        self.show()
        self._relayout()
        self.raise_()
        self.activateWindow()
        self._draft.setFocus()

    def _relayout(self) -> None:
        """Size the thread to its content (capped) and keep the edge nearest the pet in place."""
        if self._stack.currentIndex() == 0:
            width = self.WIDTH - 12 - 6  # card margins and the scrollbar gutter
            content = self._thread.heightForWidth(width) if self._thread_layout.count() else 0
            self._stack.setFixedHeight(max(0, min(self.THREAD_MAX_H, content + 4)))
        else:
            self._stack.setFixedHeight(self._stack.widget(1).sizeHint().height())
        # adjustSize() never shrinks while the layouts are stale (a just-hidden panel), so the slack stretched the card;
        # activate inner to outer so the size hint is current
        for layout in (self._panel.layout(), self._chip_box.layout(), self._card.layout(), self.layout()):
            if layout is not None:
                layout.activate()
        self.resize(self.WIDTH, self.sizeHint().height())
        anchor = self._anchor
        screen = QGuiApplication.screenAt(anchor.center()) if anchor is not None else None
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area: QRect = screen.availableGeometry()
        if anchor is None:
            anchor = QRect(area.center().x(), area.bottom() - 80, 1, 1)
        x = max(area.left() + 8, min(anchor.center().x() - self.WIDTH // 2, area.right() - self.WIDTH - 8))
        if self._above is None:  # picked once per open_near, so a resize never flips the card across the pet
            room_above = anchor.top() - self.GAP - area.top()
            room_below = area.bottom() - anchor.bottom() - self.GAP
            self._above = room_above >= self.MAX_H or room_above >= room_below
        if self._above:
            y = anchor.top() - self.GAP - self.height()  # grows upward, bottom edge stays by the pet
        else:
            y = anchor.bottom() + self.GAP
        self.move(x, max(area.top() + 8, min(y, area.bottom() - self.height() - 8)))

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is not None and event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
