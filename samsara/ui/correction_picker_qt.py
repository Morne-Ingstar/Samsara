"""Explicit draft teaching and a scoped, letter-only speech editor.

Widget construction stays on the Qt runtime. No audio, decoder, or disk scan
is needed to offer teaching; only a deliberate button invokes the store.
"""
from __future__ import annotations

import re
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from samsara.dictionary_store import get_dictionary_store
from samsara.letter_spelling import parse_letters
from samsara.ui import qt_runtime, theme


NATO = dict(zip(
    "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu".split(), "abcdefghijklmnopqrstuvwxyz"))
NATO.update(alfa="a", juliet="j", **{"x-ray": "x"})


def spelling_chunk(text: str) -> str | None:
    """Reuse the existing letter parser, adding the closed NATO alphabet."""
    words = re.sub(r"[,.;:!?]", " ", text.lower()).split()
    letters = parse_letters(" ".join(NATO.get(word, word) for word in words))
    # The existing parser title-cases names; an editor must preserve literal
    # incremental letter case instead (capital markers still work).
    return letters if any(w in {"capital", "uppercase", "cap", "upper"} for w in words) else (
        letters.lower() if letters is not None else None)


class SpellingInput:
    """Claim speech only while the owner has deliberately opened Spell.

    A lock protects the input mailbox; all widget access is posted onto Qt.
    Closing/replacing the session invalidates queued input before it paints.
    """
    def __init__(self, widget, app):
        self.widget, self.app = widget, app
        self.active = True
        self._lock = threading.Lock()
        previous = getattr(app, "_draft_spelling_input", None)
        if previous is not None:
            previous.close()
        app._draft_spelling_input = self

    def close(self):
        with self._lock:
            self.active = False
        if getattr(self.app, "_draft_spelling_input", None) is self:
            self.app._draft_spelling_input = None

    def consume(self, text):
        with self._lock:
            if not self.active:
                return False
        def update():
            if not self.active:
                return
            command = text.strip().lower().rstrip(".!?,;")
            if command in {"cancel", "keep original"}:
                self.widget._cancel_editor()
            elif command in {"apply", "finish"}:
                self.widget._apply_editor()
            elif command == "backspace":
                self.widget._editor.setText(self.widget._editor.text()[:-1])
            else:
                chunk = spelling_chunk(text)
                if chunk is None:
                    self.widget._announce("spelling-invalid", "Use letters or NATO words")
                else:
                    self.widget._editor.setText(self.widget._editor.text() + chunk)
        qt_runtime.post(update)
        return True  # Invalid spelling is consumed too, never a global command.


class CorrectionLearningOffer(QWidget):
    """One correction, two independent opt-in saves, and a safe dismissal."""
    def __init__(self, app, wrong: str, right: str):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.app, self.wrong, self.right = app, wrong, right
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Correction applied")  # Stand-in copy (621).
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_3, theme.SPACE_3, theme.SPACE_3, theme.SPACE_3)
        self.status = QLabel(f'Changed “{wrong}” to “{right}”. Save for next time?')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.always = QPushButton("Always fix this")
        self.vocabulary = QPushButton(f'Add “{right}” to my words')
        self.dismiss = QPushButton("Done")
        for button in (self.always, self.vocabulary, self.dismiss):
            button.setMinimumHeight(44)
            button.setAccessibleName(button.text())
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            layout.addWidget(button)
        self.always.clicked.connect(lambda: self._save(False))
        self.vocabulary.clicked.connect(lambda: self._save(True))
        self.dismiss.clicked.connect(self.hide)
        store = get_dictionary_store(app)
        self.always.setEnabled(store is not None and wrong.casefold() != right.casefold())
        self.vocabulary.setEnabled(store is not None and not any(
            word.casefold() == right.casefold() for word in store.custom_vocab))
        self.setStyleSheet(
            f"QWidget {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; "
            f"font-family: {theme.FONT_FAMILY}; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; "
            f"border-radius: 8px; padding: 0 {theme.SPACE_3}px; }}")
        self.setFixedWidth(460)

    def _save(self, vocabulary):
        store = get_dictionary_store(self.app)
        if store is None:
            return
        ok = store.learn_draft_correction(self.wrong, self.right, vocabulary=vocabulary)
        if not ok:
            self.status.setText("Could not save. Try again.")
            return
        button = self.vocabulary if vocabulary else self.always
        button.setEnabled(False)
        self.status.setText("Added to my words" if vocabulary else "Saved. This will be fixed next time.")

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def offer_correction_learning(app, wrong: str, right: str):
    """Called only after an accepted edit, from Full or Compact."""
    wrong, right = str(wrong).strip(), str(right).strip()
    if not wrong or not right or wrong == right:
        return
    def show():
        old = getattr(app, "_correction_learning_offer", None)
        if old is not None:
            old.hide()
            old.deleteLater()
        offer = CorrectionLearningOffer(app, wrong, right)
        app._correction_learning_offer = offer
        offer.show()
    qt_runtime.post(show)
