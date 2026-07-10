"""PySide6 "Correction capture" window for Samsara.

Hotkey-triggered (config `hotkeys.capture_correction`, default ctrl+alt+x):
pre-fills the last dictation, lets the user fix it by hand, and offers each
detected atomic word/phrase substitution (samsara/correction_capture.py) as
a REVIEW-GATED candidate for the corrections dictionary. Nothing is ever
auto-added -- a pair only reaches VoiceTrainingQt.corrections_dict when the
user explicitly clicks [Always fix] on that specific row.

Close-and-destroy lifecycle (unlike the hide-don't-destroy windows such as
diagnostics_qt.py/benchmark_review_qt.py): each hotkey press is a fresh
"fix what I just said" moment tied to whatever the most recent dictation
was at trigger time, so there is no persistent window to reuse.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QScrollArea, QFrame,
)

from samsara.ui import qt_runtime, theme
from samsara import clipboard
from samsara.correction_capture import extract_corrections

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Colour palette -- sourced from samsara.ui.theme, same convention as
# diagnostics_qt.py/benchmark_review_qt.py (local aliases so the stylesheet
# below stays readable).
# ---------------------------------------------------------------------------

_BG         = theme.BG0
_SURFACE    = theme.BG1
_ELEVATED   = theme.BG2
_BORDER     = theme.BORDER
_ACCENT     = theme.ACCENT
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI   = theme.TEXT_PRIMARY
_TEXT_SEC   = theme.TEXT_SECONDARY
_TEXT_DIS   = theme.TEXT_DISABLED

# >= 40px accessibility minimum for interactive controls (history_view.py /
# stress_wizard_qt.py convention).
_MIN_TARGET = 44

_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QTextEdit {{
    background-color: {_ELEVATED};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY + 2}px;
    padding: 8px 10px;
}}
QTextEdit:focus {{ border: 1px solid {_ACCENT}; }}
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
    color: {_TEXT_DIS};
}}
QScrollArea {{ border: none; background: transparent; }}
"""


class _PairRow(QFrame):
    """One learnable-pair row: "wrong" -> "right" plus [Always fix] /
    [Just this once]. Emits nothing -- callbacks are passed in directly."""

    def __init__(self, wrong: str, right: str, on_always_fix, on_just_once, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        label = QLabel(f'"{wrong}"  →  "{right}"')
        label.setWordWrap(True)
        lay.addWidget(label, stretch=1)

        always_btn = QPushButton("Always fix")
        always_btn.setMinimumHeight(_MIN_TARGET)
        just_once_btn = QPushButton("Just this once")
        just_once_btn.setMinimumHeight(_MIN_TARGET)

        def _handle_always():
            on_always_fix(wrong, right)
            always_btn.setEnabled(False)
            just_once_btn.setEnabled(False)
            label.setText(f'"{wrong}"  →  "{right}"   (added to dictionary)')

        def _handle_just_once():
            on_just_once(wrong, right)
            always_btn.setEnabled(False)
            just_once_btn.setEnabled(False)
            label.setText(f'"{wrong}"  →  "{right}"   (not saved)')

        always_btn.clicked.connect(_handle_always)
        just_once_btn.clicked.connect(_handle_just_once)
        lay.addWidget(always_btn)
        lay.addWidget(just_once_btn)


class _RejectedRow(QFrame):
    """Greyed, button-less row explaining why a candidate wasn't offered."""

    def __init__(self, wrong: str, right: str, reason: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        label = QLabel(f'"{wrong}"  →  "{right}"   — {reason}')
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {_TEXT_DIS};")
        lay.addWidget(label, stretch=1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CorrectionCaptureQt:
    """Close-and-destroy window opener -- one fresh window per hotkey press."""

    def __init__(self, app):
        self.app = app

    def open(self, last_text: str):
        """Safe to call from any thread. `last_text` is prefetched by the
        caller (dictation.py's history lookup) so window construction on
        the Qt thread never blocks on DB I/O."""
        qt_runtime.post(lambda: self._open_window(last_text))

    def _open_window(self, last_text: str):
        window = CorrectionCaptureWindow(self.app, last_text)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.show()
        window.raise_()
        window.activateWindow()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class CorrectionCaptureWindow(QMainWindow):

    def __init__(self, app, last_text: str):
        super().__init__()
        self.app = app
        self._original_text = last_text or ""

        self.setWindowTitle("Correct Last Dictation")
        self.resize(720, 520)
        self.setMinimumSize(520, 400)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel("Correct Last Dictation")
        title.setStyleSheet(
            f"font-size: {theme.FONT_SIZE_HEADING}px; font-weight: 600;"
        )
        root.addWidget(title)

        self._text_edit = QTextEdit()
        self._text_edit.setPlainText(self._original_text)
        if not self._original_text:
            self._text_edit.setPlaceholderText("No recent dictation found.")
        root.addWidget(self._text_edit, stretch=1)

        apply_row = QHBoxLayout()
        apply_row.addStretch()
        apply_btn = QPushButton("Apply  (Ctrl+Enter)")
        apply_btn.setMinimumHeight(_MIN_TARGET)
        apply_btn.clicked.connect(self._on_apply)
        apply_row.addWidget(apply_btn)
        root.addLayout(apply_row)

        # ---- Review queue (populated after Apply) ------------------------
        self._review_scroll = QScrollArea()
        self._review_scroll.setWidgetResizable(True)
        self._review_container = QWidget()
        self._review_layout = QVBoxLayout(self._review_container)
        self._review_layout.setContentsMargins(0, 0, 0, 0)
        self._review_layout.setSpacing(6)
        self._review_layout.addStretch()
        self._review_scroll.setWidget(self._review_container)
        self._review_scroll.setVisible(False)
        root.addWidget(self._review_scroll, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        root.addWidget(self._status_lbl)

        # ---- Keyboard shortcuts ------------------------------------------
        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_apply)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self._on_apply)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    # ------------------------------------------------------------------
    # Apply: copy corrected text to clipboard, run extraction, populate
    # the review queue. Nothing here writes to the corrections dictionary.
    # ------------------------------------------------------------------

    def _on_apply(self):
        corrected = self._text_edit.toPlainText()

        if clipboard.copy_text(corrected):
            self._set_status("Corrected text copied.")
        else:
            self._set_status("Corrected text ready (clipboard copy failed).")

        max_edit_ratio = (
            self.app.config.get('correction_capture', {}).get('max_edit_ratio', 0.5)
        )
        result = extract_corrections(self._original_text, corrected, max_edit_ratio)
        self._populate_review_queue(result)

    def _clear_review_queue(self):
        while self._review_layout.count() > 1:  # keep the trailing stretch
            item = self._review_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _populate_review_queue(self, result):
        self._clear_review_queue()
        self._review_scroll.setVisible(True)

        if not result.learnable and not result.rejected:
            no_change = QLabel("No word-level correction detected.")
            no_change.setStyleSheet(f"color: {_TEXT_SEC};")
            self._review_layout.insertWidget(0, no_change)
            return

        insert_at = 0
        for wrong, right in result.learnable:
            row = _PairRow(wrong, right, self._on_always_fix, self._on_just_once)
            self._review_layout.insertWidget(insert_at, row)
            insert_at += 1

        for wrong, right, reason in result.rejected:
            row = _RejectedRow(wrong, right, reason)
            self._review_layout.insertWidget(insert_at, row)
            insert_at += 1

        if not result.learnable:
            no_change = QLabel("No word-level correction detected.")
            no_change.setStyleSheet(f"color: {_TEXT_SEC};")
            self._review_layout.insertWidget(0, no_change)

    # ------------------------------------------------------------------
    # Per-pair actions -- the ONLY code paths that ever write to the
    # corrections dictionary, and only on an explicit [Always fix] click.
    # ------------------------------------------------------------------

    def _on_always_fix(self, wrong: str, right: str):
        """Append via the exact same mutation path
        _TrainingWindow._add_correction() uses (samsara/ui/voice_training_qt.py):
        corrections_dict[wrong] = right, then _rebuild_corrections_pattern()
        (recompiles the single-pass lookup regex), then save_training_data()
        (persists to training_data.json). Our review-queue row isn't the
        Vocabulary tab's QTableWidget, so we drive VoiceTrainingQt directly
        rather than calling the window method itself."""
        vt = getattr(self.app, 'voice_training_window', None)
        if vt is None:
            self._set_status("Could not save -- voice training is unavailable.")
            return
        try:
            vt.corrections_dict[wrong] = right
            vt._rebuild_corrections_pattern()
            vt.save_training_data()
            logger.info(f'[LEARN] "{wrong}" -> "{right}" (correction capture)')
            self._set_status(f'Added "{wrong}" -> "{right}" to your dictionary.')
        except Exception as exc:
            logger.debug(f"[CORRECT-CAP] Always-fix save failed: {exc}")
            self._set_status("Could not save that correction.")

    def _on_just_once(self, wrong: str, right: str):
        # Explicitly a no-op on the dictionary -- the row is just
        # acknowledged so the user can see it was reviewed.
        self._set_status(f'"{wrong}" -> "{right}" applied this time only.')

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
