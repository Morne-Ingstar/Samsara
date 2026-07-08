"""
PySide6 dictation history window for Samsara.

Wispr-Flow-style list, not a table: no gridlines, no per-cell boxes, rows
grouped under day-section headers. Spacing and type hierarchy do the work,
not cell borders.

Reads through app.history_store (samsara/history_store.py, a thin façade
over the SQLite-backed HistoryManager) with a fallback to the legacy
app.history list only when history_store itself is unavailable (not merely
when a query returns zero rows -- an empty-but-working history should show
the empty state, not synthesize from a possibly-stale legacy list).
"""

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QLineEdit, QComboBox, QPushButton, QLabel,
    QMenu, QMessageBox, QSizePolicy,
)

from samsara.ui import qt_runtime, theme

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_PAGE_SIZE = 200
_ROW_HEIGHT = 44          # >= 40px accessibility minimum
_HEADER_HEIGHT = 28
_LOAD_OLDER_HEIGHT = 36
_TIME_COL_WIDTH = 56
_TOAST_MS = 1500          # transient "Copied" status duration

_TYPE_PILL_LABELS = {
    'command': 'Command',
    'wake_command': 'Wake',
    'failed': 'Failed',
}


# ---------------------------------------------------------------------------
# Pure helpers -- no Qt dependency, directly unit-testable
# ---------------------------------------------------------------------------

def day_label(dt: datetime, now: "datetime | None" = None) -> str:
    """Section-header label for the day `dt` falls on, relative to `now`.

    "Today" / "Yesterday" / "Mon, Jul 6" (this year) / "Jul 6, 2025"
    (other years). Built from plain int day-of-month (dt.day) rather than
    a platform-specific strftime no-leading-zero flag (%-d is glibc-only,
    %#d is the MSVCRT equivalent -- neither is portable, so this avoids
    the flag entirely).
    """
    if now is None:
        now = datetime.now()
    d, today = dt.date(), now.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    month = dt.strftime("%b")
    if d.year == today.year:
        weekday = dt.strftime("%a")
        return f"{weekday}, {month} {dt.day}"
    return f"{month} {dt.day}, {d.year}"


def _pill_for_row(entry_type: str, status: str):
    """Return (label, color) for the row's type pill, or None to show no
    pill at all (the common case -- plain successful dictation)."""
    if entry_type in _TYPE_PILL_LABELS:
        color = theme.ERROR if entry_type == 'failed' else theme.ACCENT
        return _TYPE_PILL_LABELS[entry_type], color
    if status == 'failed':
        return 'Failed', theme.ERROR
    return None


def _parse_ts(raw_ts: str) -> "datetime | None":
    try:
        return datetime.fromisoformat(raw_ts)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stylesheet -- zero visible cell borders/gridlines; hover/selected via QSS
# on the QListWidget item itself (custom row widgets are transparent so this
# shows through).
# ---------------------------------------------------------------------------

def _build_stylesheet() -> str:
    return f"""
QMainWindow, QWidget {{
    background-color: {theme.BG0};
    color: {theme.TEXT_PRIMARY};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QListWidget {{
    background-color: {theme.BG0};
    border: none;
    outline: none;
}}
QListWidget::item {{
    border: none;
    padding: 0px;
}}
QListWidget::item:hover {{
    background-color: {theme.BG2};
}}
QListWidget::item:selected {{
    background-color: rgba(92, 196, 212, 0.14);
}}
QLineEdit {{
    background-color: {theme.BG1};
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QLineEdit:focus {{ border-color: {theme.ACCENT}; }}
QComboBox {{
    background-color: {theme.BG1};
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.FONT_SIZE_BODY}px;
    min-width: 110px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background-color: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    selection-background-color: rgba(92, 196, 212, 0.2);
    border: 1px solid {theme.BORDER};
}}
QPushButton {{
    background-color: {theme.BG1};
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    color: {theme.TEXT_PRIMARY};
    padding: 8px 16px;
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QPushButton:hover {{
    background-color: {theme.BG2};
    border-color: {theme.ACCENT};
}}
QPushButton:pressed {{
    background-color: rgba(92, 196, 212, 0.18);
}}
QPushButton[class="danger"] {{
    color: {theme.ERROR};
    border-color: rgba(248, 113, 113, 0.35);
}}
QPushButton[class="danger"]:hover {{
    background-color: rgba(248, 113, 113, 0.12);
    border-color: {theme.ERROR};
}}
QMenu {{
    background-color: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    padding: 4px 0;
    font-size: {theme.FONT_SIZE_CAPTION}px;
}}
QMenu::item {{ padding: 6px 24px 6px 16px; }}
QMenu::item:selected {{
    background-color: rgba(92, 196, 212, 0.16);
    color: {theme.ACCENT};
}}
"""


def _danger_btn(label: str) -> QPushButton:
    b = QPushButton(label)
    b.setProperty("class", "danger")
    b.style().unpolish(b)
    b.style().polish(b)
    return b


# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------

def _build_day_header_widget(label: str) -> QWidget:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 8, 12, 4)
    lbl = QLabel(label)
    lbl.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        f"font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;"
    )
    lay.addWidget(lbl)
    lay.addStretch()
    return w


def _build_row_widget(display_text: str, raw_ts: str, entry_type: str, status: str) -> QWidget:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 0, 12, 0)
    lay.setSpacing(10)

    dt = _parse_ts(raw_ts)
    time_lbl = QLabel(dt.strftime("%H:%M") if dt else "")
    time_lbl.setFixedWidth(_TIME_COL_WIDTH)
    time_lbl.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_CAPTION}px;"
    )
    lay.addWidget(time_lbl)

    text_lbl = QLabel(display_text)
    text_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FONT_SIZE_BODY}px;")
    text_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    text_lbl.setToolTip(display_text)
    text_lbl._full_text = display_text          # for re-elision on resize
    lay.addWidget(text_lbl, stretch=1)

    pill = _pill_for_row(entry_type, status)
    if pill is not None:
        pill_label, pill_color = pill
        badge = QLabel(pill_label)
        badge.setStyleSheet(
            f"color: {pill_color}; font-size: {theme.FONT_SIZE_CAPTION}px; font-weight: 600;"
            f"background: rgba(92, 196, 212, 0.12); border-radius: 8px; padding: 2px 8px;"
        )
        lay.addWidget(badge)

    w._text_label = text_lbl
    return w


def _build_load_older_widget(on_click) -> QWidget:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 4, 12, 4)
    lay.addStretch()
    btn = QPushButton("Load older")
    btn.setStyleSheet(
        f"QPushButton {{ background: transparent; color: {theme.ACCENT};"
        f" border: none; font-size: {theme.FONT_SIZE_CAPTION}px; }}"
        f"QPushButton:hover {{ text-decoration: underline; }}"
    )
    btn.clicked.connect(on_click)
    lay.addWidget(btn)
    lay.addStretch()
    return w


def _build_empty_state_widget() -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl = QLabel("Nothing yet — dictate something.")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_HEADING}px;")
    lay.addWidget(lbl)
    return w


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HistoryQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _HistoryWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class _HistoryWindow(QMainWindow):
    # Kept for parity with prior status-update plumbing; all current call
    # sites update the label directly from the Qt thread, but routing
    # through a signal costs nothing and protects against a future
    # off-thread caller.
    _status_signal = Signal(str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._store = getattr(app, 'history_store', None)
        self._status_signal.connect(self._set_status)

        self._rows_by_item_id = {}   # QListWidgetItem id() -> row dict
        self._oldest_loaded_id = None
        self._has_more = False
        self._empty_item = None
        self._load_older_item = None

        self.setWindowTitle("Dictation History")
        self.resize(860, 620)
        self.setMinimumSize(520, 400)
        self.setStyleSheet(_build_stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ---- Search + filter + actions row -------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search history…")
        self._search.textChanged.connect(lambda _: self._reload())
        top_row.addWidget(self._search, stretch=1)

        self._filter = QComboBox()
        self._filter.addItems(["All", "Dictation", "Commands", "Failed"])
        self._filter.currentTextChanged.connect(lambda _: self._reload())
        top_row.addWidget(self._filter)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._reload)
        top_row.addWidget(refresh_btn)

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._copy_selected)
        top_row.addWidget(copy_btn)

        del_btn = _danger_btn("Delete")
        del_btn.clicked.connect(self._delete_selected)
        top_row.addWidget(del_btn)

        clear_btn = _danger_btn("Clear All")
        clear_btn.clicked.connect(self._clear_all)
        top_row.addWidget(clear_btn)

        root.addLayout(top_row)

        # ---- List ---------------------------------------------------------
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self._list, stretch=1)

        # ---- Status bar -----------------------------------------------------
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        root.addWidget(self._status_lbl)

        self._reload()

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._re_elide_visible_rows()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self._list.currentItem()
            if item is not None and id(item) in self._rows_by_item_id:
                self._copy_row_item(item)
                return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _type_filter_value(self):
        f = self._filter.currentText()
        if f == "Commands":
            return None  # client-side (two entry_types); handled in _reload
        if f == "Dictation":
            return "dictation"
        if f == "Failed":
            return "failed"
        return None

    def _reload(self):
        """Fresh windowed load of the most recent page -- replaces the list."""
        query = self._search.text().strip()
        filter_ = self._filter.currentText()

        rows = []
        if self._store is not None:
            type_filter = None if filter_ == "Commands" else self._type_filter_value()
            rows = self._store.query(
                search=query or None, type_filter=type_filter, limit=_PAGE_SIZE,
            )
            if filter_ == "Commands":
                rows = [r for r in rows if r['entry_type'] in ('command', 'wake_command')]
        else:
            # Legacy fallback -- only when history_store itself is
            # unavailable, never merely because a query returned 0 rows.
            q_lower = query.lower()
            rows = [
                {
                    'id': None, 'timestamp': ts, 'entry_type': 'command' if is_cmd else 'dictation',
                    'mode': '', 'display_text': text, 'raw_text': text, 'status': 'success',
                }
                for ts, text, is_cmd in reversed(getattr(self.app, 'history', []))
                if not query or q_lower in text.lower()
            ]
            if filter_ == "Commands":
                rows = [r for r in rows if r['entry_type'] == 'command']
            elif filter_ == "Dictation":
                rows = [r for r in rows if r['entry_type'] == 'dictation']

        self._has_more = self._store is not None and len(rows) == _PAGE_SIZE
        self._render_rows(rows, append=False)

    def _load_older(self):
        if self._store is None or self._oldest_loaded_id is None:
            return
        query = self._search.text().strip()
        filter_ = self._filter.currentText()
        type_filter = None if filter_ == "Commands" else self._type_filter_value()
        rows = self._store.query(
            search=query or None, type_filter=type_filter, limit=_PAGE_SIZE,
            before_id=self._oldest_loaded_id,
        )
        if filter_ == "Commands":
            rows = [r for r in rows if r['entry_type'] in ('command', 'wake_command')]
        self._has_more = len(rows) == _PAGE_SIZE
        self._render_rows(rows, append=True)

    def _render_rows(self, rows, append: bool):
        # Normalize to plain dicts -- sqlite3.Row supports __getitem__ but
        # NOT .get(), and the rest of this method relies on .get() for
        # optional columns.
        rows = [dict(r) for r in rows]

        self._list.setUpdatesEnabled(False)
        try:
            if not append:
                self._list.clear()
                self._rows_by_item_id = {}
                self._last_day = None
                self._empty_item = None
                self._load_older_item = None
            else:
                # Remove the trailing "Load older" row -- it gets re-added
                # (or omitted) at the new end after this batch is appended.
                if self._load_older_item is not None:
                    row_idx = self._list.row(self._load_older_item)
                    if row_idx >= 0:
                        self._list.takeItem(row_idx)
                    self._load_older_item = None

            last_day = getattr(self, '_last_day', None)
            for row in rows:
                raw_ts = str(row['timestamp'])
                dt = _parse_ts(raw_ts)
                if dt is not None:
                    label = day_label(dt)
                    if label != last_day:
                        header_item = QListWidgetItem()
                        header_item.setFlags(Qt.ItemFlag.NoItemFlags)
                        header_item.setSizeHint(_size(_HEADER_HEIGHT))
                        self._list.addItem(header_item)
                        self._list.setItemWidget(header_item, _build_day_header_widget(label))
                        last_day = label

                entry_type = str(row.get('entry_type', 'dictation'))
                status = str(row.get('status', 'success'))
                full_text = str(row.get('display_text', '') or row.get('raw_text', ''))

                item = QListWidgetItem()
                item.setSizeHint(_size(_ROW_HEIGHT))
                item.setData(Qt.ItemDataRole.UserRole, {
                    'id': row['id'], 'text': full_text, 'timestamp': raw_ts,
                    'entry_type': entry_type, 'status': status,
                })
                self._list.addItem(item)
                row_widget = _build_row_widget(full_text, raw_ts, entry_type, status)
                self._list.setItemWidget(item, row_widget)
                self._rows_by_item_id[id(item)] = row['id']

                if row.get('id') is not None:
                    self._oldest_loaded_id = row['id']

            self._last_day = last_day

            if rows and self._has_more:
                self._load_older_item = QListWidgetItem()
                self._load_older_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._load_older_item.setSizeHint(_size(_LOAD_OLDER_HEIGHT))
                self._list.addItem(self._load_older_item)
                self._list.setItemWidget(
                    self._load_older_item, _build_load_older_widget(self._load_older)
                )

            if self._list.count() == 0:
                self._empty_item = QListWidgetItem()
                self._empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._empty_item.setSizeHint(_size(120))
                self._list.addItem(self._empty_item)
                self._list.setItemWidget(self._empty_item, _build_empty_state_widget())
        finally:
            self._list.setUpdatesEnabled(True)

        self._re_elide_visible_rows()
        n = len([k for k in self._rows_by_item_id])
        self._status_lbl.setText(f"{n} entr{'y' if n == 1 else 'ies'} loaded")

    def _re_elide_visible_rows(self):
        available = max(self._list.viewport().width() - _TIME_COL_WIDTH - 70, 40)
        for i in range(self._list.count()):
            item = self._list.item(i)
            widget = self._list.itemWidget(item)
            label = getattr(widget, '_text_label', None)
            if label is None:
                continue
            full = getattr(label, '_full_text', None)
            if full is None:
                continue
            fm = QFontMetrics(label.font())
            elided = fm.elidedText(full, Qt.TextElideMode.ElideRight, available)
            label.setText(elided)

    # ------------------------------------------------------------------
    # Row lookup helpers
    # ------------------------------------------------------------------

    def _row_data_for_item(self, item):
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _current_row_data(self):
        return self._row_data_for_item(self._list.currentItem())

    # ------------------------------------------------------------------
    # Copy / actions
    # ------------------------------------------------------------------

    def _copy_row_item(self, item):
        data = self._row_data_for_item(item)
        if not data or not data.get('text'):
            return
        QApplication.clipboard().setText(data['text'])
        self._show_toast("Copied")

    def _on_item_double_clicked(self, item):
        self._copy_row_item(item)

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if item is None or self._row_data_for_item(item) is None:
            return
        self._list.setCurrentItem(item)
        menu = QMenu(self)
        copy_act = menu.addAction("Copy text")
        menu.addSeparator()
        del_act = menu.addAction("Delete entry")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action == copy_act:
            self._copy_row_item(item)
        elif action == del_act:
            self._delete_item(item)

    def _copy_selected(self):
        data = self._current_row_data()
        if not data or not data.get('text'):
            QMessageBox.information(self, "No Selection", "Select an entry to copy.")
            return
        QApplication.clipboard().setText(data['text'])
        self._show_toast("Copied")

    def _delete_item(self, item):
        data = self._row_data_for_item(item)
        if data is None:
            return
        row_id = data.get('id')
        if row_id is not None and self._store is not None:
            self._store.delete([row_id])
        row_idx = self._list.row(item)
        if row_idx >= 0:
            self._list.takeItem(row_idx)
        self._rows_by_item_id.pop(id(item), None)
        n = len(self._rows_by_item_id)
        self._status_lbl.setText(f"{n} entr{'y' if n == 1 else 'ies'} loaded")

    def _delete_selected(self):
        item = self._list.currentItem()
        data = self._row_data_for_item(item)
        if data is None:
            QMessageBox.information(self, "No Selection", "Select an entry to delete.")
            return
        reply = QMessageBox.question(
            self, "Delete Entry", "Delete this history entry?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._delete_item(item)

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "Clear all dictation history? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self._store is not None:
            self._store.clear()

        legacy = getattr(self.app, 'history', None)
        if legacy is not None:
            legacy.clear()
        if hasattr(self.app, 'save_history'):
            try:
                self.app.save_history()
            except Exception as e:
                logger.debug(f"_clear_all: {e}")

        self._reload()

    # ------------------------------------------------------------------
    def _show_toast(self, msg: str):
        """Transient status confirmation -- no popup. Reverts to the entry
        count after _TOAST_MS."""
        self._status_lbl.setText(msg)
        QTimer.singleShot(_TOAST_MS, self._restore_status)

    def _restore_status(self):
        n = len(self._rows_by_item_id)
        self._status_lbl.setText(f"{n} entr{'y' if n == 1 else 'ies'} loaded")

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)


def _size(height: int) -> QSize:
    return QSize(-1, height)
