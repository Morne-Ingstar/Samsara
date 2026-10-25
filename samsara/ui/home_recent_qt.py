"""826b: bounded recent History preview, using Home's unused stretch only."""
from __future__ import annotations

from datetime import datetime
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QSizePolicy, QWidget

from samsara import recent_privacy
from samsara.ui import theme
from samsara.ui.history_view import is_no_speech_attempt


# PLACEHOLDER COPY (826b): exact choices and privacy copy in REPORT.md / ASK.json.
HEADING = "WHAT SAMSARA HEARD"
HIDE = "Hide recent words"
HISTORY = "Open in History"
FIX = "Fix word"


def row_budget(free, scale=1.0):
    """826 measured 44 header + 56/row + 5 gap + 16 reserved stretch."""
    return max(0, min(3, int((free / scale - 65) // 56)))


def relative_time(value, now=None):
    try:
        stamp = datetime.fromisoformat(str(value))
        seconds = max(0, int(((now or datetime.now(stamp.tzinfo)) - stamp).total_seconds()))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def presentation(row, reveal):
    """Plain strings only. app_context contains private titles: never use it."""
    reason = row.get("privacy_reason")
    status = row.get("status", "success")
    command = row.get("entry_type") in ("command", "wake_command")
    raw = " ".join(str(row.get("raw_text") or "").split())
    saved = " ".join(str(row.get("display_text") or "").split())
    words = len(saved.split())
    if reason:
        return "hidden", ("Text hidden — password field" if reason == "password_field"
                          else "Text hidden — protected or unverified field"), False
    if is_no_speech_attempt(row) or status == "empty":
        return "—", "No speech", False
    if status == "transcribing":
        return "—", "Transcribing", False
    if status in ("failed", "refused", "miss", "cancelled", "error"):
        outcome = "Command did not run" if command else "Not typed"
    elif status == "buffered":
        outcome = "Saved to clipboard"
    elif status == "awaiting_confirmation":
        outcome = "Waiting for confirmation"
    elif status in ("success", "completed", "matched", "queued"):
        if command:
            label = row.get("matched_command")
            outcome = f'Ran "{label}"' if reveal and label else "Ran a command"
        else:
            # Existing success rows precede delivery; no verified app receipt.
            outcome = "Typed text" if reveal else f"Typed {words} words"
    else:
        outcome = "Outcome unavailable"
    heard = f'“{raw}”' if raw else (f'Saved text: “{saved}”' if saved else "—")
    return heard if reveal else "hidden", outcome, bool(
        reveal and not command and status == "success" and saved)


class RecentActivity(QWidget):
    _loaded = Signal(object, int)

    def __init__(self, app, open_page, instruction, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.app, self.open_page, self.instruction = app, open_page, instruction
        self.rows, self.error = [], False
        self.capacity = 0
        self._busy, self._generation = False, 0
        self._sharing = True
        self._loaded.connect(self._accept)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.heading = self._label(HEADING, "section label")
        self.empty = self._label("", "outcome text")
        self._slots = []
        for index in range(3):
            age = self._label("", "section label")
            heard = self._label("", "outcome kind")
            outcome = self._label("", "outcome text")
            button = QPushButton(HISTORY, self)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda checked=False, i=index: self._activate(i))
            self._slots.append((age, heard, outcome, button))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.hide()

    def _label(self, text, role):
        label = QLabel(text, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setProperty("recentRole", role)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return label

    def refresh(self):
        # Conceal synchronously before any asynchronous store read completes.
        self._sharing = recent_privacy.sharing_detected()
        self._paint_rows()
        if self._busy:
            return
        store = getattr(self.app, "history_store", None)
        if store is None:
            self.rows, self.error = [], True
            self._paint_rows()
            return
        self._busy = True
        self._generation += 1
        generation = self._generation

        def read():
            try:
                rows = [dict(row) for row in store.query(limit=3)]
            except Exception:
                rows = None
            try:
                self._loaded.emit(rows, generation)
            except RuntimeError:
                pass  # Qt owner was destroyed; never log a row or exception.

        threading.Thread(target=read, name="home-recent-history", daemon=True).start()

    def _accept(self, rows, generation):
        if generation != self._generation:
            return
        self._busy = False
        self.error = rows is None
        self.rows = rows or []
        self._paint_rows()

    def _row(self, index):
        row = self.rows[index]
        reason = getattr(self.app, "_recent_sensitive_ids", {}).get(row.get("id"))
        if reason:
            # A query may have completed just before the injection guard erased
            # its DB row. IDs invalidate that stale snapshot, without text keys.
            row = {**row, "raw_text": "", "display_text": "", "privacy_reason": reason}
            self.rows[index] = row
        return row

    def fit(self, free):
        from samsara.ui.home_qt import _px
        # 16 vertical padding + 28 heading region + actual row target/text.
        row_height = max(_px(56), *(
            max(button.sizeHint().height(), _px(44), _px(52))
            for _, _, _, button in self._slots))
        header = _px(44)
        empty_height = max(_px(80), header + self.empty.heightForWidth(max(1, self.width() - 24)) + _px(8))
        capacity = min(3, max(0, int((free - header) // row_height))) if self.rows else int(free >= empty_height)
        self.capacity = capacity
        height = header + row_height * min(capacity, len(self.rows)) if self.rows else empty_height
        self._row_height = row_height
        if capacity:
            if self.height() != height:
                self.setFixedHeight(height)
            self.show()
        else:
            self.hide()
        self._paint_rows()

    def _paint_rows(self):
        from samsara.ui.home_qt import _px, _label_css
        reveal = recent_privacy.show_recent_words(getattr(self.app, "config", {})) and not self._sharing
        width, top = self.width(), _px(44)
        inset = _px(12)
        width = max(0, width - 2 * inset)
        self.setStyleSheet(f"RecentActivity {{ background: {theme.DATA_SURFACE}; border: none; border-radius: 10px; }}")
        self.heading.setGeometry(inset, _px(12), width, _px(24))
        self.heading.setStyleSheet(_label_css("section label", theme.ACCENT, 700))
        self.heading.setVisible(bool(self.capacity))
        empty = not self.rows and bool(self.capacity)
        self.empty.setVisible(empty)
        self.empty.setText(("Recent history unavailable" if self.error else
                            f"Nothing heard yet. {self.instruction()}.") if empty else "")
        self.empty.setStyleSheet(_label_css("outcome text", theme.DATA_TEXT_SECONDARY, 400))
        self.empty.setGeometry(inset, top, width, max(_px(28), self.height() - top - _px(8)))
        self.empty.setWordWrap(True)
        for i, (age, heard, outcome, button) in enumerate(self._slots):
            visible = i < min(self.capacity, len(self.rows))
            for child in (age, heard, outcome, button):
                child.setVisible(visible)
            if not visible:
                for label in (age, heard, outcome):
                    label.clear()
                continue
            row = self._row(i)
            speech, result, correctable = presentation(row, reveal)
            button.setText(FIX if correctable else HISTORY)
            button.setAccessibleName(button.text())
            button.setStyleSheet(_label_css("section label", theme.ACCENT, 400) +
                                " border: none; padding: 0 6px; background: transparent;")
            button_width = max(_px(112), button.sizeHint().width())
            button.setGeometry(inset + max(0, width - button_width), top, button_width, _px(44))
            age_width = _px(68)
            text_width = max(0, width - age_width - button_width - _px(12))
            for label, text, role, color, y in (
                (age, relative_time(row.get("timestamp")), "section label", theme.DATA_TEXT_SECONDARY, top),
                (heard, speech, "outcome kind", theme.TEXT_PRIMARY, top),
                (outcome, result, "outcome text", theme.DATA_TEXT_SECONDARY, top + _px(26)),
            ):
                label.setStyleSheet(_label_css(role, color, 400))
                label.setGeometry(inset if label is age else inset + age_width, y,
                                  age_width if label is age else text_width, _px(25))
                # Only the actually visible elided string enters accessibility.
                label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight,
                                                            label.width()))
                label.setToolTip("")
                label.setAccessibleDescription("")
            top += getattr(self, "_row_height", _px(56))

    def _activate(self, index):
        # Recheck the guard at the moment an explicit action might reveal text.
        self._sharing = recent_privacy.sharing_detected()
        self._paint_rows()
        row = self._row(index)
        reveal = recent_privacy.show_recent_words(self.app.config) and not self._sharing
        if presentation(row, reveal)[2]:
            from samsara.ui.correction_capture_qt import CorrectionCaptureQt
            CorrectionCaptureQt(self.app).open(str(row.get("display_text") or ""))
        else:
            self.open_page("History")

    def hide_words(self):
        home = self.app.config.get("home")
        self.app.config["home"] = {**(home if isinstance(home, dict) else {}),
                                   "show_recent_words": False}
        self._paint_rows()  # Clear visible words even if saving fails.
        save = getattr(self.app, "save_config", None)
        if callable(save):
            save()

    def _menu(self, pos):
        menu = QMenu(self)
        action = menu.addAction(HIDE)
        action.setEnabled(recent_privacy.show_recent_words(self.app.config))
        if menu.exec(self.mapToGlobal(pos)) is action:
            self.hide_words()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._paint_rows()
