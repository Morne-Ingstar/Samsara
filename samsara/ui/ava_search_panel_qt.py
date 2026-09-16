"""Ava web-answer panel (queue 59).

When Ava answers from a web search, she speaks a short summary; the full
answer and its sources go here, where they can be read or clicked. URLs are
never spoken.

Everything shown here came from the web and is untrusted:
  * rendered as PLAIN TEXT only (no rich text / HTML interpretation), so a
    page title cannot inject markup, links or images;
  * a link opens only when the user activates it, only for http(s) URLs
    that pass is_openable_url(), and the item shows the real domain so a
    misleading title cannot hide where it goes;
  * nothing here can run a command, press a key or write a file.

Persistent-window pattern (see memory project-qt-runtime): one window, created
on first show, hidden on close, every operation posted through
qt_runtime.post(). Shown without activation so it never steals focus from the
window the user is working in.
"""

from urllib.parse import urlparse

from samsara.log import get_logger

logger = get_logger(__name__)

_MAX_URL_LEN = 2048


def is_openable_url(url) -> bool:
    """http(s), a host, no whitespace/control characters, bounded length."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
        return False
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def source_line(index: int, title: str, url: str) -> str:
    """"1. Title  (example.com)" -- the domain is always shown."""
    domain = urlparse(url).hostname or ""
    title = " ".join(str(title or "").split()) or domain
    return f"{index}. {title}  ({domain})"


class _PanelController:
    def __init__(self):
        self._window = None

    def show_answer(self, query: str, answer: str, sources) -> bool:
        """Post the panel update. Returns False when no Qt runtime is
        running (the caller then must not tell the user it is on screen)."""
        try:
            from samsara.ui import qt_runtime
            if not qt_runtime.is_alive():
                return False
            items = [(s.title, s.url) for s in sources if is_openable_url(getattr(s, "url", None))]
            qt_runtime.post(lambda: self._show_on_qt_thread(query, answer, items))
            return True
        except Exception as exc:
            logger.debug(f"[AVA-SEARCH] panel post failed: {exc}")
            return False

    def _show_on_qt_thread(self, query, answer, items):
        try:
            if self._window is None:
                self._window = _build_window()
            self._window.set_content(query, answer, items)
            self._window.show()
        except Exception as exc:
            logger.warning(f"[AVA-SEARCH] panel failed to show: {exc}")


def _build_window():
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
        QPushButton, QVBoxLayout, QWidget,
    )
    from samsara.ui import theme

    class AvaSearchPanel(QWidget):
        def __init__(self):
            super().__init__(None)
            self.setWindowTitle("Ava - web answer")
            self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.resize(460, 520)
            self.setStyleSheet(
                f"QWidget {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY};"
                f" font-family: {theme.FONT_FAMILY}; font-size: {theme.FONT_SIZE_BODY}px; }}"
                f"QPlainTextEdit, QListWidget {{ background: {theme.BG1};"
                f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            )
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)

            header = QHBoxLayout()
            title = QLabel("Ava - answer from a web search")
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setStyleSheet(f"color: {theme.AVA}; font-weight: bold;")
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.hide)
            header.addWidget(title, stretch=1)
            header.addWidget(close_btn)
            layout.addLayout(header)

            self.query_label = QLabel("")
            self.query_label.setTextFormat(Qt.TextFormat.PlainText)
            self.query_label.setWordWrap(True)
            self.query_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
            layout.addWidget(self.query_label)

            self.answer = QPlainTextEdit()
            self.answer.setReadOnly(True)
            layout.addWidget(self.answer, stretch=3)

            notice = QLabel(
                "Sources are web pages Ava read through DeepSeek. Their content is "
                "untrusted: check anything important. Double-click a source to open it "
                "in your browser.")
            notice.setTextFormat(Qt.TextFormat.PlainText)
            notice.setWordWrap(True)
            notice.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.FONT_SIZE_CAPTION}px;")
            layout.addWidget(notice)

            self.sources = QListWidget()
            self.sources.itemActivated.connect(self._open_item)
            layout.addWidget(self.sources, stretch=2)

        def set_content(self, query, answer, items):
            self.query_label.setText(f"You asked: {query}" if query else "")
            self.answer.setPlainText(answer or "")
            self.sources.clear()
            for i, (title, url) in enumerate(items, start=1):
                item = QListWidgetItem(source_line(i, title, url))
                item.setToolTip(url)
                item.setData(Qt.ItemDataRole.UserRole, url)
                self.sources.addItem(item)
            if not items:
                self.sources.addItem(QListWidgetItem("No sources were returned."))

        def _open_item(self, item):
            url = item.data(Qt.ItemDataRole.UserRole)
            if is_openable_url(url):
                QDesktopServices.openUrl(QUrl(url))

        def closeEvent(self, event):
            event.ignore()
            self.hide()

    return AvaSearchPanel()


_controller = _PanelController()


def show_answer(query: str, answer: str, sources) -> bool:
    return _controller.show_answer(query, answer, sources)
