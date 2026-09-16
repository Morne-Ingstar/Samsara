"""The "Transcribe a file" dialog (queue 123).

A file picker, a destination choice, a progress bar and a working Cancel.
The decoding lives in samsara/transcribe_file.py; this is only the surface.

Operable with no mouse, in the shape queue 92 and 121 established: every
control focusable and at least 44 px, a deliberate tab order, shortcuts
written on the controls themselves, no modal message boxes, theme type
tokens only.

The end of the job is announced on the app's existing OUTCOME CHIP as well
as on the dialog's own status line -- a user who cannot see a progress bar
still learns that a two-hour file has finished, on the surface the rest of
the app already uses for that. No new announcement surface was invented.

The work runs on the thread registry, never on the UI thread: a long decode
must not freeze the hub, and Cancel must be able to reach it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

from samsara import transcribe_file as tf
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

TITLE = "Transcribe a file"
CHOOSE = "Choose a file... (Ctrl+O)"
START = "Transcribe (Enter)"
CANCEL = "Cancel (Esc)"
CLOSE = "Close"
DEST_LABEL = "When it is done"
DEST_SAVE_TEXT = "Save it beside the audio file"
DEST_TYPE_TEXT = "Type it into the window in front"
DEST_BOTH_TEXT = "Save it and type it"
NO_FILE = "No file chosen yet."
CANCELLED = "Cancelled. Nothing was written."
MIN_TARGET = 44
GRID = 12

#: The chip the rest of the app already uses for "this finished".
CHIP_DONE = "transcribed"
CHIP_CANCELLED = "transcribe cancelled"
CHIP_FAILED = "transcribe failed"


def _human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class TranscribeFilePage(QWidget):
    """The dialog. `app` is the DictationApp; `spawn` and `runner` are seams
    so the tests drive the real flow without a thread or a model."""

    #: Emitted on the worker thread, delivered on the UI thread by Qt.
    _progressed = Signal(float, float)
    _finished = Signal(object, str)

    def __init__(self, app, parent=None, spawn=None, runner=None):
        super().__init__(parent, Qt.WindowType.Window)
        self._app = app
        self._runner = runner or tf.transcribe_path
        self._spawn = spawn
        self._path: Optional[Path] = None
        self._cancelled = False
        self._busy = False
        self.setWindowTitle(TITLE)
        self._build()
        self._progressed.connect(self._on_progress)
        self._finished.connect(self._on_finished)

    # ---- Construction ----------------------------------------------------

    def _build(self):
        self.setStyleSheet(
            f"QWidget {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}"
            f"QRadioButton {{ font-size: {theme.TYPE_BODY}px; padding: 6px 0;"
            f" color: {theme.TEXT_PRIMARY}; spacing: 10px; }}"
            # The indicator is styled explicitly: the window-wide QWidget
            # background rule paints over Qt's default one, and the CHECKED
            # state was rendering as nothing at all -- so the page could not
            # show which destination was selected, which is the one thing
            # about this dialog that has to be unambiguous.
            f"QRadioButton::indicator {{ width: 16px; height: 16px;"
            f" border: 2px solid {theme.BORDER}; border-radius: 10px;"
            f" background: {theme.BG2}; }}"
            f"QRadioButton::indicator:checked {{ border: 5px solid {theme.ACCENT};"
            f" background: {theme.BG0}; }}"
            f"QRadioButton:focus {{ color: {theme.ACCENT}; }}"
            f"QProgressBar {{ background: {theme.BG2}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; height: 18px; text-align: center;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 5px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(GRID * 2, GRID * 2, GRID * 2, GRID * 2)
        lay.setSpacing(GRID)

        intro = self._label(
            "Pick a recording and Samsara will write out what is said in it. "
            "Your microphone is not used and nothing is listening."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        row.setSpacing(GRID)
        self._choose_btn = self._button(CHOOSE, self.choose_file)
        row.addWidget(self._choose_btn)
        self._file_label = self._label(NO_FILE)
        self._file_label.setWordWrap(True)
        self._file_label.setAccessibleName("Chosen file")
        row.addWidget(self._file_label, stretch=1)
        lay.addLayout(row)

        lay.addWidget(self._label(DEST_LABEL))
        # Save is checked FIRST and by default: a long transcript typed into
        # whatever has focus cannot be undone by "scratch that".
        self._dest_save = self._radio(DEST_SAVE_TEXT, checked=True)
        self._dest_type = self._radio(DEST_TYPE_TEXT)
        self._dest_both = self._radio(DEST_BOTH_TEXT)
        for radio in (self._dest_save, self._dest_type, self._dest_both):
            lay.addWidget(radio)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setAccessibleName("Transcription progress")
        self._progress.setVisible(False)
        lay.addWidget(self._progress)

        self._status = self._label("")
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Transcription status")
        lay.addWidget(self._status)

        actions = QHBoxLayout()
        actions.setSpacing(GRID)
        self._start_btn = self._button(START, self.start)
        self._start_btn.setEnabled(False)
        self._cancel_btn = self._button(CANCEL, self.cancel, danger=True)
        self._cancel_btn.setVisible(False)
        self._close_btn = self._button(CLOSE, self.close)
        actions.addWidget(self._start_btn)
        actions.addWidget(self._cancel_btn)
        actions.addStretch(1)
        actions.addWidget(self._close_btn)
        lay.addLayout(actions)

        chain = [self._choose_btn, self._dest_save, self._dest_type, self._dest_both,
                 self._start_btn, self._cancel_btn, self._close_btn]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)
        self._tab_chain = chain

    def _label(self, text) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        return label

    def _radio(self, text, checked=False) -> QRadioButton:
        radio = QRadioButton(text)
        radio.setAccessibleName(text)
        radio.setChecked(checked)
        radio.setMinimumHeight(MIN_TARGET)
        radio.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return radio

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

    # ---- Choosing --------------------------------------------------------

    def destination(self) -> str:
        if self._dest_both.isChecked():
            return tf.DEST_BOTH
        if self._dest_type.isChecked():
            return tf.DEST_TYPE
        return tf.DEST_SAVE

    def choose_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, TITLE, "", tf.file_filter())
        if path:
            self.set_file(path)

    def set_file(self, path) -> bool:
        """Accept a chosen file, or refuse it in words. Separate from the
        picker so a test (and a voice entry point) can drive it."""
        try:
            tf.check_supported(path)
        except tf.TranscribeError as exc:
            self._path = None
            self._file_label.setText(NO_FILE)
            self._status.setText(str(exc))
            self._start_btn.setEnabled(False)
            return False
        self._path = Path(path)
        self._file_label.setText(self._path.name)
        self._status.setText(f"Ready. Saves to {tf.transcript_path(self._path).name}.")
        self._start_btn.setEnabled(True)
        self._start_btn.setFocus()
        return True

    # ---- Running ---------------------------------------------------------

    def start(self) -> bool:
        if self._path is None or self._busy:
            return False
        self._busy = True
        self._cancelled = False
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._cancel_btn.setVisible(True)
        self._start_btn.setEnabled(False)
        self._choose_btn.setEnabled(False)
        self._status.setText("Working...")
        self._spawn_worker()
        return True

    def _spawn_worker(self):
        spawn = self._spawn
        if spawn is None:
            from samsara.runtime import thread_registry  # noqa: PLC0415
            spawn = thread_registry.spawn
        spawn("transcribe-file", self._work)

    def _work(self):
        """The worker. Never touches a widget directly -- results come back
        through the two signals, which Qt delivers on the UI thread."""
        try:
            result = self._runner(
                self._app, self._path,
                progress=lambda done, total: self._progressed.emit(done, total),
                cancel=lambda: self._cancelled,
            )
        except tf.Cancelled:
            self._finished.emit(None, "")
            return
        except Exception as exc:
            logger.warning("[FILE-TX] %s failed: %s", self._path, exc)
            self._finished.emit(None, str(exc))
            return
        self._finished.emit(result, "")

    def cancel(self):
        """Ask the worker to stop. It checks before every chunk, and nothing
        is written until it returns, so a cancel leaves no partial output."""
        self._cancelled = True
        self._status.setText("Stopping...")

    # ---- Results ---------------------------------------------------------

    def _on_progress(self, done: float, total: float):
        if total > 0:
            self._progress.setValue(int(100 * done / total))
        self._status.setText(f"Working... {_human(done)} of {_human(total)}")

    def _on_finished(self, result, error: str):
        self._busy = False
        self._cancel_btn.setVisible(False)
        self._choose_btn.setEnabled(True)
        self._start_btn.setEnabled(self._path is not None)
        self._progress.setVisible(False)

        if result is None and self._cancelled:
            self._status.setText(CANCELLED)
            self._chip(CHIP_CANCELLED, "warning")
            return
        if result is None:
            self._status.setText(error or "That file could not be transcribed.")
            self._chip(CHIP_FAILED, "error")
            return
        if not result.text:
            self._status.setText("No speech was found in that file. Nothing was written.")
            self._chip("no speech found", "warning")
            return

        try:
            outcome = tf.deliver(self._app, self._path, result.text, self.destination())
        except tf.TranscribeError as exc:
            self._status.setText(str(exc))
            self._chip(CHIP_FAILED, "error")
            return

        words = len(result.text.split())
        where = []
        if outcome.get("saved"):
            where.append(Path(outcome["saved"]).name)
        if outcome.get("typed"):
            where.append("typed into the window in front")
        self._status.setText(
            f"Done. {words} words from {_human(result.duration_s)} of audio"
            + (f" -> {', '.join(where)}." if where else "."))
        # The end-of-job announcement, on the surface the app already uses --
        # a user who cannot watch a progress bar still learns it finished.
        self._chip(f"{CHIP_DONE}: {words} words", "success")
        self._progress.setValue(100)

    def _chip(self, label, kind):
        show = getattr(self._app, "_show_outcome_chip", None)
        if not callable(show):
            return
        try:
            show(label, kind)
        except Exception as exc:
            logger.debug("[FILE-TX] chip failed: %s", exc)

    # ---- Qt --------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.cancel() if self._busy else self.close()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._start_btn.isEnabled():
            self.start()
            return
        if key == Qt.Key.Key_O and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.choose_file()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._cancelled = True          # a closed dialog must not leave work running
        super().closeEvent(event)


def open_transcribe_file(app, parent=None) -> TranscribeFilePage:
    """Build and show the dialog. The app-side entry point calls this."""
    page = TranscribeFilePage(app, parent)
    page.resize(560, 460)
    page.show()
    page.raise_()
    page.activateWindow()
    return page
