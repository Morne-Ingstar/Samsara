"""The memo list (queue 92) -- the "interactive UI" queue 07 deferred.

Queue 07 shipped a notepad file and a tray item that opened it. That was
what was asked for then, and it is still the raw-file affordance; but a file
in Notepad cannot play a recording, cannot be searched by category, and
cannot delete one entry. This page can.

What it does: reads samsara.quick_memo's store, shows every memo newest
first with its date, its category and its transcript, and plays the audio
where it was retained. Read, play, copy, delete, search, re-categorise.

Operable with no mouse, by design, not by accident:
  * Every control is a real focusable widget in a deliberate tab order, and
    every one of them has a keyboard shortcut spelled out in its own label
    or its accessible name -- never a shortcut a user has to already know.
  * The memo list is a QListWidget, so Up/Down/Home/End work as they do
    everywhere else, and the actions apply to the selected row.
  * Enter plays the selected memo, Delete removes it (with an explicit
    confirm step that is itself keyboard-driven -- never a modal dialog,
    which queue 07's own notes warn blocks the automation path).
  * Ctrl+F reaches the search box from anywhere on the page.

Type sizes come from theme tokens only (queue 83's floor): nothing here is
smaller than the body size the rest of the hub reads at.

Audio playback reuses sounddevice, already a hard dependency of the capture
path, so nothing new is installed for it. Playback is asynchronous and one
memo at a time; starting another stops the first.
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from samsara import quick_memo
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

TITLE = "Memos"
SEARCH_LABEL = "Search memos (Ctrl+F)"
SEARCH_PLACEHOLDER = "Search what you said..."
PLAY = "Play (Enter)"
STOP = "Stop"
COPY = "Copy"
COPIED = "Copied."
DELETE = "Delete (Del)"
CONFIRM_DELETE = "Delete for good?"
CANCEL = "Cancel"
OPEN_FILE = "Open the raw file"
CATEGORY_LABEL = "Category"
UNCATEGORISED = "Uncategorised"
NO_MEMOS = "No memos yet. Hold Ctrl+Alt+M, or say \"take a memo\", and talk."
NO_MATCHES = "No memo matches that search."
NO_AUDIO = "No audio kept for this one."
AUDIO_EXPIRED = "Audio expired; the transcript is kept."
MIN_TARGET = 44
GRID = 12
EM_DASH = chr(0x2014)
MIDDLE_DOT = chr(0xB7)


def summarise(record: dict, width: int = 96) -> str:
    """One list row: when, category, and the start of what was said."""
    stamp = str(record.get("at", "")).replace("T", " ")[:16]
    text = " ".join(str(record.get("text", "")).split())
    if len(text) > width:
        text = text[: width - 1].rstrip() + chr(0x2026)
    category = record.get("category")
    middle = f"  {MIDDLE_DOT} {category}" if category else ""
    return f"{stamp}{middle}  {EM_DASH}  {text}"


def playable(record: dict, home=None) -> Optional[Path]:
    """The WAV to play for this memo, or None."""
    path = quick_memo.audio_path_for(record, home)
    if path is None or not path.exists():
        return None
    return path


def read_wav(path):
    """(samples as floats in -1..1, sample rate). Stdlib only."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"unsupported sample width {width}")
    scale = float(1 << 15)
    values = [int.from_bytes(frames[i:i + 2], "little", signed=True) / scale
              for i in range(0, len(frames) - 1, 2)]
    if channels > 1:
        values = values[::channels]
    return values, rate


class _Player:
    """One memo at a time, through the audio device the app already uses.

    Isolated from the widget so a machine with no working output device
    fails as a message on the page, not as a traceback out of a Qt slot.
    """

    def __init__(self):
        self._active = False

    @property
    def playing(self) -> bool:
        return self._active

    def play(self, path) -> bool:
        self.stop()
        try:
            import sounddevice as sd  # noqa: PLC0415 -- optional at import time
            samples, rate = read_wav(path)
            sd.play(samples, rate)
            self._active = True
            return True
        except Exception as exc:
            logger.warning("[MEMO] Playback failed for %s: %s", path, exc)
            self._active = False
            return False

    def stop(self):
        self._active = False
        try:
            import sounddevice as sd  # noqa: PLC0415
            sd.stop()
        except Exception as exc:
            logger.debug("[MEMO] Could not stop playback: %s", exc)


class MemosPage(QWidget):
    """The hub's Memos page."""

    def __init__(self, app, parent=None, home=None, player=None):
        super().__init__(parent)
        self._app = app
        self._home = home
        self._player = player if player is not None else _Player()
        self._records = []
        self._filtered = []
        self._pending_delete = None
        self._build()
        self.reload()

    # ---- Build ----------------------------------------------------------

    def _build(self):
        self.setStyleSheet(
            f"QWidget {{ background: {theme.BG1}; }}"
            f"QLineEdit, QComboBox {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 10px;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QLineEdit:focus, QComboBox:focus {{ border: 2px solid {theme.ACCENT}; }}"
            f"QListWidget {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QListWidget::item {{ padding: 10px 8px; }}"
            f"QListWidget::item:selected {{ background: {theme.BG2};"
            f" color: {theme.TEXT_PRIMARY}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(GRID, GRID, GRID, GRID)
        lay.setSpacing(GRID)

        # Search row. The label carries the shortcut, so the shortcut is
        # never something the user had to be told somewhere else.
        search_row = QHBoxLayout()
        search_row.setSpacing(GRID)
        label = QLabel(SEARCH_LABEL)
        label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        search_row.addWidget(label)
        self._search = QLineEdit()
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setAccessibleName(SEARCH_LABEL)
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(MIN_TARGET)
        self._search.textChanged.connect(self._apply_filter)
        label.setBuddy(self._search)
        search_row.addWidget(self._search, stretch=1)
        lay.addLayout(search_row)

        self._list = QListWidget()
        self._list.setAccessibleName("Memos")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setAlternatingRowColors(False)
        self._list.setWordWrap(True)
        # Rows wrap; they never scroll sideways. A horizontal scrollbar in a
        # list of sentences is a way to hide half of one.
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentRowChanged.connect(self._on_selection)
        self._list.itemActivated.connect(lambda _item: self.play_selected())
        lay.addWidget(self._list, stretch=1)

        # The full transcript of the selected memo, at reading size.
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setAccessibleName("Selected memo")
        self._detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self._detail.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;"
            f" background: {theme.BG0}; border: 1px solid {theme.BORDER};"
            " border-radius: 8px; padding: 10px;")
        self._detail.setMinimumHeight(MIN_TARGET + GRID)
        lay.addWidget(self._detail)

        # Actions. Every one is a button with its key in the visible label.
        actions = QHBoxLayout()
        actions.setSpacing(GRID)
        self._play_btn = self._button(PLAY, self.play_selected)
        self._copy_btn = self._button(COPY, self.copy_selected)
        self._delete_btn = self._button(DELETE, self.request_delete)
        self._confirm_btn = self._button(CONFIRM_DELETE, self.confirm_delete, danger=True)
        self._cancel_btn = self._button(CANCEL, self.cancel_delete)
        self._confirm_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        for button in (self._play_btn, self._copy_btn, self._delete_btn,
                       self._confirm_btn, self._cancel_btn):
            actions.addWidget(button)

        actions.addStretch(1)
        category_label = QLabel(CATEGORY_LABEL)
        category_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        actions.addWidget(category_label)
        self._category = QComboBox()
        self._category.setAccessibleName(CATEGORY_LABEL)
        self._category.setEditable(True)
        self._category.setMinimumHeight(MIN_TARGET)
        self._category.setMinimumWidth(160)
        category_label.setBuddy(self._category)
        self._category.activated.connect(lambda _i: self.apply_category())
        self._category.lineEdit().returnPressed.connect(self.apply_category)
        actions.addWidget(self._category)
        lay.addLayout(actions)

        footer = QHBoxLayout()
        footer.setSpacing(GRID)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Memo status")
        self._status.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        footer.addWidget(self._status, stretch=1)
        # Queue 07's affordance, kept: anyone who wants the plain file gets it.
        self._raw_btn = self._button(OPEN_FILE, self.open_raw_file)
        footer.addWidget(self._raw_btn)
        lay.addLayout(footer)

        chain = [self._search, self._list, self._play_btn, self._copy_btn,
                 self._delete_btn, self._confirm_btn, self._cancel_btn,
                 self._category, self._raw_btn]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)
        self._tab_chain = chain

    def _button(self, text, slot, danger=False) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.setMinimumHeight(MIN_TARGET)
        button.setMinimumWidth(MIN_TARGET)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        colour = theme.ERROR if danger else theme.TEXT_PRIMARY
        button.setStyleSheet(
            f"QPushButton {{ background: {theme.BG2}; color: {colour};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px;"
            f" padding: 8px 14px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED};"
            f" border-color: {theme.BORDER_FAINT}; }}"
        )
        button.clicked.connect(slot)
        return button

    # ---- Data -----------------------------------------------------------

    def reload(self):
        """Re-read the store. Cheap enough to call whenever the page opens."""
        try:
            self._records = list(reversed(quick_memo.load_memos(self._home)))
        except Exception as exc:
            logger.warning("[MEMO] Could not load memos: %s", exc)
            self._records = []
        self._reload_categories()
        self._apply_filter()

    def _reload_categories(self):
        try:
            names = quick_memo.categories(self._home)
        except Exception:
            names = []
        current = self._category.currentText()
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem("")
        for name in names:
            self._category.addItem(name)
        self._category.setCurrentText(current)
        self._category.blockSignals(False)

    def _apply_filter(self):
        needle = self._search.text()
        self._filtered = quick_memo.search_memos(self._records, needle)
        self._list.blockSignals(True)
        self._list.clear()
        for record in self._filtered:
            item = QListWidgetItem(summarise(record))
            item.setData(Qt.ItemDataRole.UserRole, record.get("id"))
            # The row's accessible name is the whole memo, not the elided
            # summary: a screen reader must not read a truncated thought.
            item.setData(Qt.ItemDataRole.AccessibleTextRole, self._spoken(record))
            self._list.addItem(item)
        self._list.blockSignals(False)
        if self._filtered:
            self._list.setCurrentRow(0)
        self.cancel_delete()
        self._on_selection(self._list.currentRow())

    def _spoken(self, record) -> str:
        stamp = str(record.get("at", "")).replace("T", " ")[:16]
        category = record.get("category") or UNCATEGORISED
        return f"{stamp}, {category}. {record.get('text', '')}"

    # ---- Selection ------------------------------------------------------

    @property
    def selected(self) -> Optional[dict]:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._filtered):
            return None
        return self._filtered[row]

    def _on_selection(self, _row=None):
        record = self.selected
        has = record is not None
        self._play_btn.setEnabled(has and playable(record, self._home) is not None)
        self._copy_btn.setEnabled(has)
        self._delete_btn.setEnabled(has)
        self._category.setEnabled(has)
        if not has:
            self._detail.setText("")
            self._status.setText(NO_MATCHES if self._records else NO_MEMOS)
            return
        self._detail.setText(record.get("text", ""))
        self._category.blockSignals(True)
        self._category.setCurrentText(record.get("category") or "")
        self._category.blockSignals(False)
        if playable(record, self._home) is not None:
            self._status.setText("")
        elif record.get("audio_pruned"):
            self._status.setText(AUDIO_EXPIRED)
        else:
            self._status.setText(NO_AUDIO)

    # ---- Actions --------------------------------------------------------

    def play_selected(self):
        record = self.selected
        if record is None:
            return False
        path = playable(record, self._home)
        if path is None:
            self._status.setText(AUDIO_EXPIRED if record.get("audio_pruned") else NO_AUDIO)
            return False
        if self._player.play(path):
            self._play_btn.setText(STOP)
            self._play_btn.setAccessibleName(STOP)
            self._status.setText("")
            # The button goes back to Play when the clip is over; the
            # duration is known from the file, so no polling is needed.
            QTimer.singleShot(self._duration_ms(path), self._reset_play)
            return True
        self._status.setText("That memo's audio could not be played.")
        return False

    def _duration_ms(self, path) -> int:
        try:
            with wave.open(str(path), "rb") as wav:
                return int(wav.getnframes() / float(wav.getframerate()) * 1000) + 100
        except Exception:
            return 1000

    def _reset_play(self):
        self._player.stop()
        self._play_btn.setText(PLAY)
        self._play_btn.setAccessibleName(PLAY)

    def stop_playback(self):
        self._reset_play()

    def copy_selected(self) -> bool:
        record = self.selected
        if record is None:
            return False
        try:
            from PySide6.QtWidgets import QApplication  # noqa: PLC0415
            QApplication.clipboard().setText(record.get("text", ""))
        except Exception as exc:
            logger.warning("[MEMO] Copy failed: %s", exc)
            return False
        self._status.setText(COPIED)
        return True

    def request_delete(self):
        """Ask, in the page itself. Never a modal dialog: a modal blocks
        every other path into the app, including the voice one."""
        record = self.selected
        if record is None:
            return
        self._pending_delete = record.get("id")
        self._delete_btn.setVisible(False)
        self._confirm_btn.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._confirm_btn.setFocus()
        self._status.setText(f"{CONFIRM_DELETE} This also deletes its audio.")

    def cancel_delete(self):
        self._pending_delete = None
        self._delete_btn.setVisible(True)
        self._confirm_btn.setVisible(False)
        self._cancel_btn.setVisible(False)

    def confirm_delete(self) -> bool:
        memo_id = self._pending_delete
        self.cancel_delete()
        if not memo_id:
            return False
        try:
            gone = quick_memo.delete_memo(memo_id, self._home)
        except Exception as exc:
            logger.warning("[MEMO] Delete failed: %s", exc)
            return False
        self.reload()
        self._status.setText("Memo deleted." if gone else "That memo was already gone.")
        return gone

    def apply_category(self) -> bool:
        record = self.selected
        if record is None:
            return False
        name = self._category.currentText().strip()
        try:
            if name and name.lower() not in [c.lower() for c in
                                             quick_memo.categories(self._home)]:
                quick_memo.add_category(name, home=self._home)
            changed = quick_memo.set_category(record.get("id"), name or None,
                                              home=self._home)
        except Exception as exc:
            logger.warning("[MEMO] Category change failed: %s", exc)
            return False
        if changed:
            row = self._list.currentRow()
            self.reload()
            if 0 <= row < self._list.count():
                self._list.setCurrentRow(row)
            self._status.setText(f"Filed under {name}." if name
                                 else f"Set to {UNCATEGORISED}.")
        return changed

    def open_raw_file(self) -> bool:
        path = quick_memo.memo_file(self._home)
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Memos\n\n", encoding="utf-8")
            import os  # noqa: PLC0415
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                self._status.setText(f"The memo file is at {path}")
                return False
            startfile(str(path))
            return True
        except Exception as exc:
            logger.warning("[MEMO] Could not open %s: %s", path, exc)
            self._status.setText(f"Could not open {path}")
            return False

    # ---- Keyboard -------------------------------------------------------

    def keyPressEvent(self, event):
        """The shortcuts the labels promise. Handled here rather than with
        QShortcut so they work whatever has focus inside the page, and so a
        test can exercise them without a window manager."""
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_F and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._search.setFocus()
            self._search.selectAll()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._pending_delete:
                self.cancel_delete()
                self._status.setText("")
            elif self._player.playing:
                self.stop_playback()
            elif self._search.text():
                self._search.clear()
            event.accept()
            return
        # Delete and Enter act on the list, but not while the user is typing
        # a search term or a category name.
        typing = self._search.hasFocus() or self._category.hasFocus()
        if key == Qt.Key.Key_Delete and not typing:
            self.request_delete()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not typing:
            self.play_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- Introspection (tests) ------------------------------------------

    @property
    def tab_chain(self) -> list:
        return list(self._tab_chain)

    @property
    def rows(self) -> list:
        return [self._list.item(i).text() for i in range(self._list.count())]

    @property
    def status_text(self) -> str:
        return self._status.text()

    @property
    def detail_text(self) -> str:
        return self._detail.text()
