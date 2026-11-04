"""The memo list (queue 92) -- the "interactive UI" queue 07 deferred.

Queue 07 shipped a notepad file and a tray item that opened it. That was
what was asked for then, and it is still the raw-file affordance; but a file
in Notepad cannot play a recording, cannot be searched by category, and
cannot delete one entry. This page can.

What it does: reads samsara.quick_memo's store, shows every memo newest
first with its date, its category and its transcript, and plays retained audio
or opens a saved video. Read, play, copy, delete, search, re-categorise.

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
path. Video opens in the user's default media player.
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, QSize, QEvent, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
    QPlainTextEdit, QMenu, QToolButton, QWidgetAction, QFrame, QStyledItemDelegate, QCheckBox, QScrollArea, QDialog,
)
from PySide6.QtGui import QDesktopServices

from samsara import quick_memo
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

TITLE = "Memos"
SEARCH_LABEL = "Search memos (Ctrl+F)"
SEARCH_PLACEHOLDER = "Search what you said..."
PLAY = "Play (Enter)"
OPEN_VIDEO = "Open video (Enter)"
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
EMPTY_MEMOS = "Record a voice or video memo with the buttons above."
NO_MATCHES = "No memo matches that search."
NO_AUDIO = "No recording for this one."  # PLACEHOLDER COPY (1125)
NO_VIDEO = "This video's file is missing."
AUDIO_EXPIRED = "Audio expired; the transcript is kept."
MIN_TARGET = 34
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
    media = "Video  " if record.get("video") else ""
    if record.get("partial"):
        media = "Partial video  "
    return f"{stamp}{middle}  {EM_DASH}  {media}{text}"


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


class _MemoCardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # The card widget paints the row; keep its summary for accessibility
        # and introspection without painting it again in the card's margins.
        pass


class MemosPage(QWidget):
    """The hub's Memos page."""

    _batch_ready = Signal(object, str)

    def __init__(self, app, parent=None, home=None, player=None):
        super().__init__(parent)
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self._app = app
        self._home = home
        self._player = player if player is not None else _Player()
        self._records = []
        self._filtered = []
        self._pending_delete = None
        self._checked_ids = set()
        self._drafts = {}
        self._editing_id = None
        self._batch_runner = None
        self._batch_ready.connect(self._show_batch_preview)
        self._build()
        self.reload()

    # ---- Build ----------------------------------------------------------

    def _label(self, text, size=theme.TYPE_BODY, muted=False):
        label = QLabel(text)
        label.setFont(theme.qfont(size))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet(
            f"background: transparent; border: none; color: {theme.TEXT_SECONDARY if muted else theme.TEXT_PRIMARY};"
            + {theme.TYPE_HUB_TITLE: f" font-size: {theme.TYPE_HUB_TITLE}px;",
               theme.TYPE_BODY: f" font-size: {theme.TYPE_BODY}px;",
               theme.TYPE_MIN: f" font-size: {theme.TYPE_MIN}px;"}[size])
        return label

    def _build(self):
        self.setStyleSheet(
            f"QWidget {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}"
            f"QLineEdit, QComboBox, QPlainTextEdit {{ background: {theme.BG2};"
            f" color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; padding: 0 10px; font-size: {theme.TYPE_BODY}px; }}"
            f"QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {theme.ACCENT}; }}"
            "QComboBox::drop-down { background: transparent; border: none;"
            " subcontrol-origin: padding; subcontrol-position: center right; width: 24px; }"
            "QComboBox::down-arrow { background: transparent; border: none;"
            f" image: url({theme.ARROW_PATH}); width: 10px; height: 6px; margin-right: 8px; }}"
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { background: transparent; border: none; padding: 0; }"
            + theme.SCROLLBAR_QSS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        self._column = QWidget()
        self._column.setMaximumWidth(1180)
        outer.addWidget(self._column, 1)
        lay = QVBoxLayout(self._column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        title = self._label(TITLE, theme.TYPE_HUB_TITLE)
        title.setStyleSheet(title.styleSheet() + "font-weight: 600;")
        lay.addWidget(title)
        # PLACEHOLDER COPY (854): purpose line from the approved 841 study.
        lay.addWidget(self._label("Keep a thought, then find it when you need it.", muted=True))
        lay.addSpacing(8)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._search = QLineEdit()
        self._search.setAccessibleName(SEARCH_LABEL)
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setFixedSize(280, 34)
        self._search.textChanged.connect(self._apply_filter)
        controls.addWidget(self._search)
        self._filter_category = QComboBox()
        self._filter_category.setAccessibleName("Filter memos by category")
        self._filter_category.setFixedSize(140, 34)
        self._filter_category.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(self._filter_category)
        self._record_voice_btn = self._button("Record voice memo (Alt+R)", self.record_voice_memo)
        self._record_voice_btn.setAccessibleName("Record voice memo (Alt+R)")
        self._record_voice_btn.setStyleSheet(self._record_voice_btn.styleSheet() +
            f"QPushButton {{ background: {theme.ACCENT}; color: {theme.TEXT_ON_ACCENT}; }}")
        controls.addWidget(self._record_voice_btn)
        self._record_video_btn = self._button("Record video memo (Alt+V)", self.record_video_memo)
        self._settings_link = self._button("Settings", self.open_memo_settings)
        self._settings_link.setAccessibleName("Settings (Alt+S)")
        menu_button = QToolButton()
        menu_button.setArrowType(Qt.ArrowType.DownArrow)
        menu_button.setAccessibleName("More recording options")
        menu_button.setFixedSize(34, 34)
        menu_button.setStyleSheet(
            f"QToolButton {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            "QToolButton::menu-indicator { image: none; }")
        menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(menu_button)
        for button in (self._record_video_btn, self._settings_link):
            action = QWidgetAction(menu)
            action.setDefaultWidget(button)
            menu.addAction(action)
        menu_button.setMenu(menu)
        controls.addWidget(menu_button)
        controls.addStretch()
        lay.addLayout(controls)
        self._memo_keys = self._label("", theme.TYPE_MIN, True)
        self._memo_keys.setAccessibleName("Memo recording keys")
        lay.addWidget(self._memo_keys)

        # PLACEHOLDER COPY (975c): checked batch is independent of editor row.
        batch = QHBoxLayout()
        self._batch_mode = QCheckBox("Select a batch")
        self._batch_mode.setAccessibleName(self._batch_mode.text())
        self._batch_mode.setMinimumHeight(40)
        self._batch_mode.toggled.connect(lambda _checked: self._render_cards())
        batch.addWidget(self._batch_mode)
        self._today_btn = self._button("Select today's memos", self.select_today_batch)
        self._clear_batch_btn = self._button("Clear selection", self.clear_batch)
        self._preview_batch_btn = self._button("Preview Cockpit changes (0)", self.preview_batch)
        for button in (self._today_btn, self._clear_batch_btn, self._preview_batch_btn):
            batch.addWidget(button)
        batch.addStretch()
        lay.addLayout(batch)
        self._batch_total = self._label("0 checked; 0 hidden", theme.TYPE_MIN, True)
        lay.addWidget(self._batch_total)

        # A modeless review keeps the memo list/editor usable and avoids
        # forcing the hub taller than a normal desktop display.
        self._batch_review = QDialog(self)
        self._batch_review.setFont(theme.qfont(theme.TYPE_BODY))
        self._batch_review.setWindowTitle("Review Cockpit changes")
        self._batch_review.setModal(False)
        self._batch_review.resize(1050, 800)
        self._batch_review.finished.connect(lambda _result: self.cancel_batch())
        review_layout = QVBoxLayout(self._batch_review)
        review_layout.setContentsMargins(20, 20, 20, 20)
        review_layout.setSpacing(12)
        self._batch_preview = QPlainTextEdit()
        self._batch_preview.setFont(theme.qfont(theme.TYPE_BODY))
        self._batch_preview.setReadOnly(True)
        self._batch_preview.setAccessibleName("Cockpit proposed changes and exclusions")
        self._batch_preview.setMinimumHeight(180)
        self._batch_preview.setMaximumWidth(1180)
        self._batch_preview.hide()
        review_layout.addWidget(self._batch_preview, 1)
        self._batch_choices_area = QScrollArea()
        self._batch_choices_area.setWidgetResizable(True)
        self._batch_choices_area.setMaximumHeight(180)
        self._batch_choices_widget = QWidget()
        self._batch_choices_layout = QVBoxLayout(self._batch_choices_widget)
        self._batch_choices_area.setWidget(self._batch_choices_widget)
        self._batch_choices_area.hide()
        review_layout.addWidget(self._batch_choices_area)
        self._revise_batch_btn = self._button("Update preview with these choices", self.revise_batch)
        self._revise_batch_btn.hide()
        review_layout.addWidget(self._revise_batch_btn)
        review = QHBoxLayout()
        self._approve_batch_btn = self._button("Review and approve batch", self.approve_batch)
        self._cancel_batch_btn = self._button("Cancel batch", self.cancel_batch)
        self._approve_batch_btn.hide()
        self._cancel_batch_btn.hide()
        review.addWidget(self._approve_batch_btn)
        review.addWidget(self._cancel_batch_btn)
        review.addStretch()
        review_layout.addLayout(review)
        source_row = QHBoxLayout()
        self._batch_source_choice = QComboBox()
        self._batch_source_choice.setFont(theme.qfont(theme.TYPE_BODY))
        self._batch_source_choice.setMaximumWidth(520)
        self._batch_source_choice.setMinimumHeight(40)
        self._batch_source_choice.setAccessibleName("Review a selected memo source")
        self._batch_source_choice.hide()
        self._batch_source_btn = self._button("Show selected source", self.show_batch_source)
        self._batch_source_btn.hide()
        source_row.addWidget(self._batch_source_choice)
        source_row.addWidget(self._batch_source_btn)
        source_row.addStretch()
        review_layout.addLayout(source_row)
        self._batch_review_status = self._label("", theme.TYPE_MIN, True)
        self._batch_review_status.setWordWrap(True)
        review_layout.addWidget(self._batch_review_status)

        panes = QHBoxLayout()
        panes.setSpacing(32)
        self._list = QListWidget()
        self._list.setAccessibleName("Memos")
        self._list.setItemDelegate(_MemoCardDelegate(self._list))
        self._list.setFixedWidth(700)
        self._list.setSpacing(0)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentRowChanged.connect(self._on_selection)
        self._list.itemSelectionChanged.connect(self._selection_changed)
        self._list.itemActivated.connect(lambda _item: self.play_selected())
        self._list.viewport().installEventFilter(self)
        left = QVBoxLayout()
        self._empty = self._label(EMPTY_MEMOS, muted=True)
        self._empty.setWordWrap(True)
        self._empty.setMaximumWidth(680)
        self._empty.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        left.addWidget(self._empty, alignment=Qt.AlignmentFlag.AlignTop)
        left.addWidget(self._list, 1)
        left.addStretch()
        panes.addLayout(left)
        self._editor_pane = QWidget()
        self._editor_pane.setMaximumWidth(718)
        detail = QVBoxLayout(self._editor_pane)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(16)
        self._selected_meta = self._label("", theme.TYPE_MIN, True)
        detail.addWidget(self._selected_meta)
        self._detail = QPlainTextEdit()
        self._detail.setAccessibleName("Selected memo transcript")
        self._detail.setMaximumWidth(718)
        self._detail.setFixedHeight(220)
        self._detail.setStyleSheet("QPlainTextEdit { padding: 10px; }")
        detail.addWidget(self._detail)
        # PLACEHOLDER COPY (854): explicit Save keeps selection changes non-destructive.
        self._save_btn = self._button("Save", self.save_selected)
        detail.addWidget(self._save_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._play_btn = self._button(PLAY, self.play_selected)
        self._copy_btn = self._button(COPY, self.copy_selected)
        self._delete_btn = self._button(DELETE, self.request_delete)
        self._confirm_btn = self._button(CONFIRM_DELETE, self.confirm_delete, danger=True)
        self._cancel_btn = self._button(CANCEL, self.cancel_delete)
        for button in (self._play_btn, self._copy_btn, self._delete_btn,
                       self._confirm_btn, self._cancel_btn):
            actions.addWidget(button)
        actions.addStretch()
        detail.addLayout(actions)
        category_row = QHBoxLayout()
        category_label = self._label(CATEGORY_LABEL, theme.TYPE_MIN, True)
        category_row.addWidget(category_label)
        self._category = QComboBox()
        self._category.setAccessibleName(CATEGORY_LABEL)
        self._category.setEditable(True)
        self._category.setFixedSize(160, 34)
        self._category.lineEdit().setMaximumWidth(160)
        category_label.setBuddy(self._category)
        self._category.activated.connect(lambda _i: self.apply_category())
        self._category.lineEdit().returnPressed.connect(self.apply_category)
        category_row.addWidget(self._category)
        category_row.addStretch()
        detail.addLayout(category_row)
        detail.addStretch()
        panes.addWidget(self._editor_pane, 1)
        panes.addStretch()
        lay.addLayout(panes, 1)
        self._count = self._label("", theme.TYPE_MIN, True)
        lay.addWidget(self._count)
        footer = QHBoxLayout()
        self._status = self._label("", theme.TYPE_MIN, True)
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Memo status")
        self._status.setMinimumWidth(320)
        footer.addWidget(self._status)
        self._raw_btn = self._button(OPEN_FILE, self.open_raw_file)
        footer.addWidget(self._raw_btn)
        footer.addStretch()
        lay.addLayout(footer)
        chain = [self._record_voice_btn, self._record_video_btn, self._settings_link,
                 self._search, self._list,
                 self._batch_mode, self._today_btn, self._clear_batch_btn, self._preview_batch_btn,
                 self._detail, self._save_btn,
                 self._play_btn, self._copy_btn, self._delete_btn, self._confirm_btn,
                 self._cancel_btn, self._category, self._raw_btn, self._filter_category, menu_button]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)
        self._tab_chain = chain

    def _button(self, text, slot, danger=False):
        button = QPushButton(text)
        button.setFont(theme.qfont(theme.TYPE_BODY))
        button.setAccessibleName(text)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        colour = theme.ERROR if danger else theme.TEXT_PRIMARY
        button.setStyleSheet(
            f"QPushButton {{ background: {theme.BG2}; color: {colour};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px;"
            f" min-height: 32px; max-height: 32px; padding: 0 12px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover, QPushButton:focus {{ border: 1px solid {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED}; }}")
        button.setFixedWidth(button.sizeHint().width())
        button.clicked.connect(slot)
        return button

    def eventFilter(self, watched, event):
        if (watched is self._list.viewport() and event.type() == QEvent.Type.MouseButtonPress
                and self._list.itemAt(event.position().toPoint()) is None):
            self._list.setCurrentRow(-1)
        return super().eventFilter(watched, event)

    def _selection_changed(self):
        if not self._list.selectedItems():
            self._list.setCurrentRow(-1)
        self._on_selection()

    def _render_cards(self):
        width = 680 if self._list.width() == 700 else 408
        for i, record in enumerate(self._filtered):
            item = self._list.item(i)
            card = QFrame()
            selected = i == self._list.currentRow()
            card.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not self._batch_mode.isChecked())
            if self._batch_mode.isChecked():
                card.mousePressEvent = lambda _event, row=i: self._list.setCurrentRow(row)
            card.setFixedWidth(width)
            card.setStyleSheet(
                f"QFrame {{ background: {theme.BG0}; border: 1px solid {theme.ACCENT if selected else theme.BORDER_FAINT}; border-radius: 8px; }}")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)
            meta = QHBoxLayout()
            meta.setSpacing(8)
            if self._batch_mode.isChecked():
                check = QCheckBox()
                check.setFont(theme.qfont(theme.TYPE_BODY))
                memo_id = record["id"]
                check.setAccessibleName("Include memo " + str(record.get("at", "")))
                check.setMinimumSize(40, 40)
                check.setStyleSheet(self._batch_checkbox_style())
                check.setChecked(memo_id in self._checked_ids)
                check.toggled.connect(lambda value, key=memo_id: self._check_memo(key, value))
                meta.addWidget(check)
            meta.addWidget(self._label(str(record.get("at", "")).replace("T", " ")[:16], theme.TYPE_MIN, True))
            category = self._label(record.get("category") or UNCATEGORISED, theme.TYPE_MIN, True)
            category.setStyleSheet(category.styleSheet() + f"background: {theme.BG2}; border-radius: 4px; padding: 3px 7px;")
            meta.addWidget(category)
            if record.get("video"):
                meta.addWidget(self._label("Partial video" if record.get("partial") else "Video", theme.TYPE_MIN, True))
            meta.addStretch()
            layout.addLayout(meta)
            text = self._label(record.get("text", ""))
            text.setWordWrap(True)
            text.setFixedWidth(width - 26)
            layout.addWidget(text)
            height = layout.totalHeightForWidth(width)
            card.setFixedHeight(max(80, height))
            item.setSizeHint(QSize(width, card.height() + 8))
            old = self._list.itemWidget(item)
            if old is not None:
                old.hide()
                self._list.removeItemWidget(item)
                old.deleteLater()
            self._list.setItemWidget(item, card)

    def save_selected(self):
        record = self.selected
        text = self._detail.toPlainText().strip()
        if record is None or not text:
            return False
        try:
            records = quick_memo.load_memos(self._home)
            target = next((r for r in records if r.get("id") == record.get("id")), None)
            if target is None:
                return False
            target["text"] = text
            quick_memo._write_index(records, self._home)
            quick_memo._rewrite_markdown(records, self._home)
        except Exception as exc:
            logger.warning("[MEMO] Edit failed: %s", exc)
            self._status.setText("Memo changes could not be saved.")
            return False
        memo_id = record.get("id")
        self._drafts.pop(memo_id, None)
        self._editing_id = None
        self.reload()
        for i, entry in enumerate(self._filtered):
            if entry.get("id") == memo_id:
                self._list.setCurrentRow(i)
                break
        self._status.setText("Memo saved.")
        return True

    # ---- Data -----------------------------------------------------------

    def reload(self):
        """Re-read the store. Cheap enough to call whenever the page opens."""
        self._remember_draft()
        self._refresh_memo_keys()
        try:
            self._records = list(reversed(quick_memo.load_memos(self._home)))
        except Exception as exc:
            logger.warning("[MEMO] Could not load memos: %s", exc)
            self._records = []
        self._reload_categories()
        self._checked_ids.intersection_update(r["id"] for r in self._records)
        self._apply_filter()

    def _config(self):
        config = getattr(self._app, "config", {})
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _pretty_hotkey(raw):
        if not raw:
            return "(not set)"
        from samsara.ui.quick_reference_qt import _pretty_key_combo
        return _pretty_key_combo(str(raw))

    def _refresh_memo_keys(self):
        config = self._config()
        self._memo_hotkey = str(config.get("memo_hotkey", "ctrl+alt+m"))
        self._cancel_hotkey = str(config.get("cancel_hotkey", "escape"))
        self._memo_keys.setText(
            f'Voice memo: {self._pretty_hotkey(self._memo_hotkey)}  {MIDDLE_DOT}  '
            f'Video memo: say "record video memo"  {MIDDLE_DOT}  ')

    def record_voice_memo(self):
        self._refresh_memo_keys()
        start = getattr(self._app, "start_memo_capture", None)
        if not callable(start):
            self._status.setText("Voice memo recording is unavailable.")
            return False
        try:
            started = start()
        except Exception as exc:
            logger.warning("[MEMO] Capture start failed: %s", exc)
            self._status.setText("Voice memo recording could not start.")
            return False
        if started is False:
            self._status.setText("Voice memo recording could not start.")
            return False
        self._status.setText(
            f"Say 'finish' or press {self._pretty_hotkey(self._memo_hotkey)} again to stop.")
        return True

    def record_video_memo(self):
        self._refresh_memo_keys()
        executor = getattr(self._app, "command_executor", None)
        dispatch = getattr(executor, "process_text", None)
        if not callable(dispatch):
            self._status.setText("Video memo recording is unavailable.")
            return False
        try:
            result = dispatch("record video memo", self._app, force_commands=True)
        except Exception as exc:
            logger.warning("[MEMO] Video capture start failed: %s", exc)
            self._status.setText("Video memo recording could not start.")
            return False
        if result is False or getattr(result, "succeeded", True) is False:
            self._status.setText("Video memo recording could not start.")
            return False
        self._status.setText(
            f"Press {self._pretty_hotkey(self._cancel_hotkey)} to finish the video.")
        return True

    def open_memo_settings(self):
        from samsara.ui.home_qt import open_settings_tab
        return open_settings_tab(self._app, "Modes")

    def _reload_categories(self):
        try:
            names = quick_memo.categories(self._home)
        except Exception:
            names = []
        names = sorted(set(names) | {r["category"] for r in self._records if r.get("category")})
        selected_filter = self._filter_category.currentText()
        self._filter_category.blockSignals(True)
        self._filter_category.clear()
        self._filter_category.addItem("All categories")
        self._filter_category.addItems(names)
        self._filter_category.setCurrentIndex(max(0, self._filter_category.findText(selected_filter)))
        self._filter_category.blockSignals(False)
        current = self._category.currentText()
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem("")
        for name in names:
            self._category.addItem(name)
        self._category.setCurrentText(current)
        self._category.blockSignals(False)

    def _apply_filter(self):
        self._remember_draft()
        self._editing_id = None
        needle = self._search.text()
        self._filtered = quick_memo.search_memos(self._records, needle)
        category = self._filter_category.currentText()
        if self._filter_category.currentIndex() > 0:
            self._filtered = [r for r in self._filtered if r.get("category") == category]
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
        is_empty = not self._records and not self._search.text().strip()
        self._empty.setVisible(is_empty)
        self._list.setVisible(not is_empty)
        if self._filtered:
            self._list.setCurrentRow(0)
        self._count.setText(f"{len(self._filtered)} memos")
        self._update_batch_total()
        self.cancel_delete()
        self._on_selection(self._list.currentRow())

    def _spoken(self, record) -> str:
        stamp = str(record.get("at", "")).replace("T", " ")[:16]
        category = record.get("category") or UNCATEGORISED
        media = "Video memo. " if record.get("video") else ""
        if record.get("partial"):
            media = "Partial video memo. "
        return f"{stamp}, {category}. {media}{record.get('text', '')}"

    # ---- Selection ------------------------------------------------------

    @property
    def selected(self) -> Optional[dict]:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._filtered):
            return None
        return self._filtered[row]

    def _on_selection(self, _row=None):
        self._remember_draft()
        record = self.selected
        has = record is not None
        self.cancel_delete()
        self._editor_pane.setVisible(has)
        self._list.setFixedWidth(430 if has else 700)
        self._render_cards()
        self._save_btn.setEnabled(has)
        video_path = quick_memo.video_path_for(record, self._home) if has else None
        has_video = bool(video_path and video_path.exists())
        has_audio = has and playable(record, self._home) is not None
        self._play_btn.setText(OPEN_VIDEO if has_video else PLAY)
        self._play_btn.setAccessibleName(OPEN_VIDEO if has_video else PLAY)
        self._play_btn.setFixedWidth(self._play_btn.sizeHint().width())
        self._play_btn.setEnabled(bool(has_video or has_audio))
        self._copy_btn.setEnabled(has)
        self._delete_btn.setEnabled(has)
        self._category.setEnabled(has)
        if not has:
            self._editing_id = None
            self._detail.setPlainText("")
            self._status.setText(NO_MATCHES if self._records and not self._filtered else "")
            return
        self._selected_meta.setText(self._spoken(record).split(". ", 1)[0])
        self._editing_id = record["id"]
        self._detail.setPlainText(self._drafts.get(record["id"], record.get("text", "")))
        self._category.blockSignals(True)
        self._category.setCurrentText(record.get("category") or "")
        self._category.blockSignals(False)
        if has_video:
            self._status.setText("Partial recording kept." if record.get("partial")
                                 else "Open video to play this memo.")
        elif playable(record, self._home) is not None:
            self._status.setText("")
        elif record.get("audio_pruned"):
            self._status.setText(AUDIO_EXPIRED)
        elif record.get("video"):
            self._status.setText(NO_VIDEO)
        else:
            self._status.setText(NO_AUDIO)

    # ---- Cockpit batch (PLACEHOLDER COPY for Morne) -----------------------

    @staticmethod
    def _batch_checkbox_style():
        return (f"QCheckBox::indicator {{ width: 30px; height: 30px; border: 2px solid {theme.TEXT_SECONDARY};"
                f" border-radius: 4px; background: {theme.BG2}; }}"
                f"QCheckBox::indicator:checked {{ background: {theme.ACCENT}; border-color: {theme.ACCENT}; }}")

    def _remember_draft(self):
        if self._editing_id is None or not hasattr(self, "_detail"):
            return
        record = next((r for r in self._records if r["id"] == self._editing_id), None)
        if record and self._detail.toPlainText() != record.get("text", ""):
            self._drafts[self._editing_id] = self._detail.toPlainText()
        else:
            self._drafts.pop(self._editing_id, None)

    def _check_memo(self, memo_id, checked):
        if checked:
            self._checked_ids.add(memo_id)
        else:
            self._checked_ids.discard(memo_id)
        self._update_batch_total()

    def _update_batch_total(self):
        visible = {r["id"] for r in self._filtered}
        self._batch_total.setText(f"{len(self._checked_ids)} checked; {len(self._checked_ids - visible)} hidden")
        self._preview_batch_btn.setText(f"Preview Cockpit changes ({len(self._checked_ids)})")
        self._preview_batch_btn.setAccessibleName(self._preview_batch_btn.text())
        self._preview_batch_btn.setFixedWidth(self._preview_batch_btn.sizeHint().width())

    def clear_batch(self):
        self._checked_ids.clear()
        self._update_batch_total()
        self._render_cards()

    def select_today_batch(self):
        from datetime import datetime
        from samsara.cockpit_memos import quick_source, select_memos, MemoDisposition
        try:
            selection = select_memos([quick_source(r) for r in self._records], mode="today",
                                     now=datetime.now().astimezone(), allow_partial=True)
        except MemoDisposition as exc:
            self._status.setText(str(exc))
            return False
        self._checked_ids = {m["memo_id"] for m in selection.memos}
        self._batch_mode.setChecked(True)
        self._render_cards()
        self._update_batch_total()
        self._status.setText("Today's capture date in " + selection.timezone + ". Partial memos remain marked.")
        return True

    def preview_batch(self):
        from plugins.commands.cockpit import start_preview
        self._remember_draft()
        if self._checked_ids & self._drafts.keys():
            self._status.setText("Save checked memo edits explicitly before previewing the batch.")
            return False
        if not self._checked_ids:
            self._status.setText("Check at least one saved memo.")
            return False
        try:
            result = start_preview(self._app, dict(mode="checked", checked_ids=tuple(self._checked_ids)),
                                   home=self._home, completed=self._batch_ready.emit)
            self._batch_runner = getattr(self._app, "_cockpit_memo_runner", None)
            self._approve_batch_btn.hide()
            self._cancel_batch_btn.show()
            self._batch_preview.setPlainText("Preparing a preview from the saved selection.")
            self._batch_preview.show()
            self._set_batch_status("Preparing a preview from the saved selection.")
            self._batch_review.show()
            return result
        except ValueError as exc:
            self._status.setText(str(exc))
            return False

    def _show_batch_preview(self, runner, error):
        if error:
            self._set_batch_status(error)
            self._approve_batch_btn.setEnabled(False)
            return
        if getattr(self._app, "_cockpit_memo_runner", runner) is not runner or runner._cancelled:
            return
        self._batch_runner = runner
        pages = [runner.read_page(i, page_size=3) for i in range(max(1, (len(runner.proposal["items"]) + 2) // 3))]
        # Questions are locally rendered reason/candidates, never model wording.
        remainder = [str(q["reason"]) + ": " + ", ".join(q["candidate_ids"])
                     for q in runner.proposal["unresolved"]]
        self._batch_preview_text = "\n".join(list(runner.selection.warnings) + pages + remainder)
        self._batch_preview.setPlainText(self._batch_preview_text)
        self._batch_source_choice.clear()
        for number, memo in enumerate(runner.selection.memos, 1):
            self._batch_source_choice.addItem(f"Source {number}: {memo['source_kind']} — {memo['captured_at']}", memo['memo_id'])
        self._batch_source_choice.show()
        self._batch_source_btn.show()
        self._batch_preview.show()
        self._approve_batch_btn.setEnabled(True)
        while self._batch_choices_layout.count():
            child = self._batch_choices_layout.takeAt(0)
            if child.widget():
                child.widget().hide()
                child.widget().deleteLater()
        self._batch_choice_controls = []
        titles = {r["id"]: r["title"] for r in runner.context["records"]}
        for number, row in enumerate(runner.proposal["items"], 1):
            widget = QWidget()
            widget.setMinimumHeight(48)
            line = QHBoxLayout(widget)
            line.setContentsMargins(4, 4, 4, 4)
            check = QCheckBox(f"Include item {number}")
            check.setAccessibleName(check.text())
            check.setFont(theme.qfont(theme.TYPE_BODY))
            check.setMinimumHeight(40)
            check.setStyleSheet(self._batch_checkbox_style())
            check.setChecked(row["included"])
            check.setEnabled(row["disposition"] in {"included", "excluded", "needs_target_choice"})
            line.addWidget(check)
            choice = QComboBox()
            choice.setFont(theme.qfont(theme.TYPE_BODY))
            choice.setMinimumHeight(40)
            choice.setAccessibleName(f"Choose existing target for item {number}")
            choice.addItem("Choose a target", None)
            for candidate in row["candidates"]:
                choice.addItem(titles.get(candidate["id"], candidate["id"]), candidate["id"])
            selected = runner.proposal["target_choices"].get(row["item_key"])
            if selected:
                choice.setCurrentIndex(choice.findData(selected))
            choice.setVisible(row["disposition"] == "needs_target_choice" or row["item_key"] in runner.proposal["target_choices"])
            choice.currentIndexChanged.connect(lambda index, c=check: c.setChecked(index > 0))
            check.toggled.connect(lambda _value: self._approve_batch_btn.setEnabled(False))
            choice.currentIndexChanged.connect(lambda _index: self._approve_batch_btn.setEnabled(False))
            line.addWidget(choice)
            self._batch_choice_controls.append((row["item_key"], check, choice))
            self._batch_choices_layout.addWidget(widget)
        self._batch_choices_area.setFixedHeight(min(230, max(80, len(self._batch_choice_controls) * 56 + 16)))
        self._batch_choices_area.setVisible(bool(self._batch_choice_controls))
        self._revise_batch_btn.setVisible(bool(self._batch_choice_controls))
        self._approve_batch_btn.setVisible(True)
        self._cancel_batch_btn.show()
        self._set_batch_status("Review every proposed change. Unresolved items remain excluded.")
        self._batch_review.show()

    def _set_batch_status(self, message):
        self._status.setText(message)
        self._batch_review_status.setText(message)

    def show_batch_source(self):
        if self._batch_runner is None:
            return False
        memo_id = self._batch_source_choice.currentData()
        memo = next((m for m in self._batch_runner.selection.memos if m['memo_id'] == memo_id), None)
        if memo is None:
            return False
        self._batch_preview.setPlainText(self._batch_preview_text + "\n\nSelected source:\n" + memo['text'])
        return True

    def revise_batch(self):
        runner = self._batch_runner
        if runner is None:
            return False
        keys, choices = [], {}
        for key, check, choice in self._batch_choice_controls:
            if check.isChecked():
                keys.append(key)
            if choice.currentData() is not None:
                choices[key] = choice.currentData()
        from samsara.runtime import thread_registry
        def work():
            try:
                runner.repreview(selected_keys=keys, target_choices=choices)
                self._batch_ready.emit(runner, "")
            except Exception:
                self._batch_ready.emit(None, "Choices could not be previewed; refresh the batch before approval.")
        # Updating choices always invalidates authority; approval requires the
        # newly displayed, server-validated immutable preview.
        self._approve_batch_btn.setEnabled(False)
        thread_registry.spawn("cockpit.memo.repreview", work, daemon=True)
        return True

    def approve_batch(self):
        if self._batch_runner is None:
            return False
        self._remember_draft()
        if {m["memo_id"] for m in self._batch_runner.selection.memos} & self._drafts.keys():
            self._set_batch_status("Save checked memo edits explicitly and refresh the preview before approval.")
            return False
        try:
            from samsara import execution_policy as policy
            self._batch_runner.stage_approval(listen=False)
            outcome = policy.answer_pending(self._app, "yes", source=policy.ANSWER_DIALOG)
            if outcome != "approved":
                self._set_batch_status("Batch approval refused; refresh the preview.")
                return False
            result = self._batch_runner.last_result or {}
            result = result.get("receipt") or result
            self._set_batch_status("Batch " + str(result.get("status", "status unknown; recover the request"))
                                   + "; unresolved items remain excluded.")
            return True
        except ValueError as exc:
            self._set_batch_status(str(exc))
            return False

    def cancel_batch(self):
        if self._batch_runner is not None:
            self._batch_runner.cancel()
            if (getattr(self._app, "_cockpit_memo_runner", None) is self._batch_runner
                    and not self._batch_runner.application_uncertain):
                self._app._cockpit_memo_runner = None
        self._batch_runner = None
        self._batch_preview.hide()
        self._batch_choices_area.hide()
        self._revise_batch_btn.hide()
        self._batch_source_choice.hide()
        self._batch_source_btn.hide()
        self._batch_review.hide()
        self._approve_batch_btn.hide()
        self._cancel_batch_btn.hide()
        self._set_batch_status("Batch cancelled.")

    # ---- Actions --------------------------------------------------------

    def play_selected(self):
        record = self.selected
        if record is None:
            return False
        video_path = quick_memo.video_path_for(record, self._home)
        if video_path is not None and video_path.exists():
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(video_path)))
            self._status.setText("" if opened else "That video could not be opened.")
            return bool(opened)
        if record.get("video"):
            self._status.setText(NO_VIDEO)
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
        self._status.setText(f"{CONFIRM_DELETE} This also deletes its audio or video.")

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
        if modifiers & Qt.KeyboardModifier.AltModifier and key == Qt.Key.Key_S:
            self.open_memo_settings()
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.AltModifier and key == Qt.Key.Key_R:
            self.record_voice_memo()
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.AltModifier and key == Qt.Key.Key_V:
            self.record_video_memo()
            event.accept()
            return
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
            elif self.selected is not None:
                self._list.setCurrentRow(-1)
            event.accept()
            return
        # Delete and Enter act on the list, but not while the user is typing
        # a search term or a category name.
        typing = (self._search.hasFocus() or self._category.hasFocus()
                  or self._detail.hasFocus())
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
        return self._detail.toPlainText()
