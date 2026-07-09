"""PySide6 "Benchmark Review" window for Samsara.

Gold-standard review UI for the personal WER benchmark harness
(samsara/benchmark_store.py). Lists locally collected dictation samples;
lets the user play back the audio, correct the transcript, and confirm it
as gold (or discard the sample entirely). tools/benchmark_eval.py reads
only gold-confirmed rows for offline accuracy evaluation.

Persistent, hide-don't-destroy window (same family as diagnostics_qt.py):
built once on the Qt thread via qt_runtime.post(), closeEvent ignores the
close and hides instead of destroying, no wrapper close() method.
"""

import wave

import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QPlainTextEdit,
)

from samsara.ui import qt_runtime, theme
from samsara import benchmark_store
from samsara.runtime import thread_registry

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Colour palette -- sourced from samsara.ui.theme, same convention as
# diagnostics_qt.py/history_qt.py (local aliases so the stylesheet below
# stays readable).
# ---------------------------------------------------------------------------

_BG         = theme.BG0
_SURFACE    = theme.BG1
_ELEVATED   = theme.BG2
_BORDER     = theme.BORDER
_ACCENT     = theme.ACCENT
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI   = theme.TEXT_PRIMARY
_TEXT_SEC   = theme.TEXT_SECONDARY

# >= 40px accessibility minimum for interactive controls, matching
# history_view.py's _ROW_HEIGHT / stress_wizard_qt.py's button heights.
_MIN_TARGET = 44

_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QListWidget {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-bottom: 1px solid {_BORDER};
}}
QListWidget::item:selected {{
    background-color: {_ACCENT_DIM};
    color: {_ACCENT};
}}
QPushButton {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 8px 14px;
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QPushButton:hover {{
    background-color: {_ELEVATED};
    border-color: {_ACCENT};
}}
QPushButton:pressed {{
    background-color: {_ACCENT_DIM};
}}
QPushButton:disabled {{
    color: {_TEXT_SEC};
}}
QPlainTextEdit {{
    background-color: {_ELEVATED};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
    padding: 8px 10px;
}}
QPlainTextEdit:focus {{ border: 1px solid {_ACCENT}; }}
"""


def _fmt_sample_label(row: dict) -> str:
    status = "[gold]" if row.get('gold') else "[pending]"
    preview = (row.get('final_text') or '').strip() or "(empty)"
    if len(preview) > 64:
        preview = preview[:61] + "..."
    dur = row.get('duration_s') or 0.0
    return f"{status}  {dur:.1f}s  {preview}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class BenchmarkReviewQt:
    """Persistent, hide-don't-destroy window wrapper (diagnostics_qt.py pattern)."""

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
        self._window = BenchmarkReviewWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class BenchmarkReviewWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.setWindowTitle("Benchmark Review")
        self.resize(900, 580)
        self.setMinimumSize(640, 440)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        # ---- Left: sample list ---------------------------------------
        left = QVBoxLayout()
        left.setSpacing(6)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        left.addWidget(self._progress_lbl)

        self._list = QListWidget()
        self._list.setMinimumWidth(320)
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, stretch=1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setMinimumHeight(_MIN_TARGET)
        refresh_btn.clicked.connect(self._reload)
        left.addWidget(refresh_btn)

        root.addLayout(left, stretch=1)

        # ---- Right: detail pane -----------------------------------------
        right = QVBoxLayout()
        right.setSpacing(8)

        self._meta_lbl = QLabel("Select a sample to review.")
        self._meta_lbl.setWordWrap(True)
        self._meta_lbl.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        right.addWidget(self._meta_lbl)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText(
            "Transcript (edit to correct, then Confirm as gold)"
        )
        right.addWidget(self._text_edit, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._play_btn = QPushButton("Play  (Space)")
        self._play_btn.setMinimumHeight(_MIN_TARGET)
        self._play_btn.clicked.connect(self._on_play)
        btn_row.addWidget(self._play_btn)

        self._confirm_btn = QPushButton("Confirm as gold  (Ctrl+Enter)")
        self._confirm_btn.setMinimumHeight(_MIN_TARGET)
        self._confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self._confirm_btn)

        self._discard_btn = QPushButton("Discard sample  (Del)")
        self._discard_btn.setMinimumHeight(_MIN_TARGET)
        self._discard_btn.clicked.connect(self._on_discard)
        btn_row.addWidget(self._discard_btn)

        right.addLayout(btn_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        right.addWidget(self._status_lbl)

        root.addLayout(right, stretch=2)

        # ---- Keyboard shortcuts ------------------------------------------
        # Space/Del are guarded against the transcript editor having focus
        # (see _on_play/_on_discard) so normal typing still works while
        # correcting a transcript. Ctrl+Enter is not a normal typing key in
        # a plain-text editor, so it's safe to fire unconditionally.
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._on_play)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_confirm)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self._on_confirm)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self._on_discard)

        self._samples = []  # parallel to list rows
        self._reload()

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self._stop_playback()
        self.hide()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _reload(self):
        selected_id = self._current_sample_id()
        rows = benchmark_store.list_samples()
        self._samples = rows

        st = benchmark_store.stats()
        self._progress_lbl.setText(f"{st['gold_confirmed']} of {st['total']} reviewed")

        self._list.blockSignals(True)
        self._list.clear()
        restore_row = -1
        for i, row in enumerate(rows):
            item = QListWidgetItem(_fmt_sample_label(row))
            self._list.addItem(item)
            if row.get('id') == selected_id:
                restore_row = i
        self._list.blockSignals(False)

        if restore_row >= 0:
            self._list.setCurrentRow(restore_row)
        elif self._list.count() > 0:
            self._list.setCurrentRow(0)
        else:
            self._on_row_changed(-1)

    def _current_sample_id(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._samples):
            return self._samples[row].get('id')
        return None

    def _current_sample(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._samples):
            return self._samples[row]
        return None

    def _on_row_changed(self, _row):
        sample = self._current_sample()
        if sample is None:
            self._meta_lbl.setText("Select a sample to review.")
            self._text_edit.setPlainText("")
            return
        gold = sample.get('gold')
        self._text_edit.setPlainText(gold if gold else sample.get('final_text', ''))
        self._meta_lbl.setText(
            f"model: {sample.get('model', '?')}    "
            f"duration: {sample.get('duration_s', 0):.1f}s    "
            f"raw transcript: \"{sample.get('raw_transcript', '')}\""
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _stop_playback(self):
        thread_registry.spawn("benchmark-review-stop", self._stop_playback_worker)

    def _stop_playback_worker(self):
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as exc:
            logger.debug(f"[BENCH-UI] sd.stop() failed: {exc}")

    def _on_play(self):
        if self._text_edit.hasFocus():
            return
        sample = self._current_sample()
        if sample is None:
            return
        wav_path = benchmark_store.audio_path(sample)
        thread_registry.spawn("benchmark-review-play", self._play_worker, args=(wav_path,))

    def _play_worker(self, wav_path):
        """Runs on a background thread -- wav file read + sd.play() (which
        itself hands off to a PortAudio callback thread) never touch the
        Qt thread. Only the status-label update is marshalled back via
        qt_runtime.post()."""
        try:
            import sounddevice as sd
            with wave.open(str(wav_path), 'rb') as wf:
                n_frames = wf.getnframes()
                samplerate = wf.getframerate()
                raw = wf.readframes(n_frames)
            audio = np.frombuffer(raw, dtype=np.int16)
            sd.play(audio, samplerate)
            qt_runtime.post(lambda: self._set_status("Playing..."))
        except Exception as exc:
            logger.debug(f"[BENCH-UI] playback failed: {exc}")
            qt_runtime.post(lambda: self._set_status("Playback failed."))

    def _on_confirm(self):
        sample = self._current_sample()
        if sample is None:
            return
        text = self._text_edit.toPlainText().strip()
        if not text:
            self._set_status("Cannot confirm an empty transcript.")
            return
        if benchmark_store.set_gold(sample['id'], text):
            self._set_status("Confirmed as gold.")
            self._reload()
        else:
            self._set_status("Confirm failed.")

    def _on_discard(self):
        if self._text_edit.hasFocus():
            return
        sample = self._current_sample()
        if sample is None:
            return
        self._stop_playback()
        if benchmark_store.discard_sample(sample['id']):
            self._set_status("Sample discarded.")
            self._reload()
        else:
            self._set_status("Discard failed.")

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
