"""Explicit draft teaching and a scoped, letter-only speech editor.

Widget construction stays on the Qt runtime. No audio, decoder, or disk scan
is needed to offer teaching; only a deliberate button invokes the store.
"""
from __future__ import annotations

import re
import threading

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtWidgets import (QLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

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
    """Claim letter speech only while the replacement field is active.

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


class _WordRows(QLayout):
    """Keep controls at their natural width, wrapping only when necessary."""
    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def sizeHint(self):
        return QSize(0, self.heightForWidth(self.parentWidget().width()))

    def minimumSize(self):
        return QSize(0, 44)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self.items:
            if item.widget().isHidden():
                continue
            size = item.sizeHint()
            width = min(size.width(), max(1, rect.width()))
            if x > rect.x() and x + width > rect.x() + rect.width():
                x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(x, y, width, max(44, size.height())))
            x += width + self.spacing()
            row_height = max(row_height, 44, size.height())
        return y + row_height - rect.y()


class WordCorrectionMenu(QWidget):
    """Shared embedded picker; an accepted edit reveals explicit teaching."""
    def __init__(self, parent, app, on_apply, on_done, on_changed):
        super().__init__(parent)
        self.app, self.on_apply, self.on_done, self.on_changed = app, on_apply, on_done, on_changed
        self.learning = self.awaiting = False
        self.wrong = self.right = ""
        self._spelling_input = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.choices = QScrollArea()
        self.choices.setWidgetResizable(True)
        self.choices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.choices.setFrameShape(QScrollArea.Shape.NoFrame)
        self.choice_body = QWidget()
        self.choice_body.setObjectName("wordAlternates")
        self.choice_layout = _WordRows(self.choice_body)
        self.choices.setWidget(self.choice_body)
        layout.addWidget(self.choices)
        self._editor = QLineEdit()
        self._editor.setPlaceholderText("Type it")
        self._editor.setAccessibleName("Type a replacement or say letters")
        self._editor.setToolTip("Type or paste a replacement, or use the Command-only key to say letters.")
        self._editor.setMaxLength(120)
        self._editor.setMinimumHeight(44)
        self._editor.setFixedWidth(240)
        self._editor.installEventFilter(self)
        self._editor.returnPressed.connect(self._apply_editor)
        layout.addWidget(self._editor, alignment=Qt.AlignmentFlag.AlignLeft)
        self.actions = QWidget()
        row = _WordRows(self.actions)
        self.apply = self._button("Apply", self._apply_editor)
        self.dismiss = self._button("Keep original", self._cancel_editor)
        self.always = self._button("Always fix this", lambda: self._save(False))
        self.vocabulary = self._button("", lambda: self._save(True))
        for button in (self.always, self.vocabulary, self.apply, self.dismiss):
            row.addWidget(button)
        layout.addWidget(self.actions)
        self._editor.textChanged.connect(lambda text: self.apply.setEnabled(bool(text.strip())))
        self.buttons = []
        self.hide()

    def _button(self, text, callback):
        button = QPushButton(text)
        button.setMinimumHeight(44)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def open(self, wrong, candidates):
        self.close_input()
        self.learning = self.awaiting = False
        self.wrong, self.right = wrong, ""
        if self.app is not None:
            previous = active_menu(self.app)
            if previous is not None and previous is not self:
                previous.finish()
            self.app._draft_correction_menu = self
        while self.buttons:
            button = self.buttons.pop()
            self.choice_layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        for candidate in dict.fromkeys(str(getattr(c, "replacement", c)) for c in candidates):
            if candidate and candidate != wrong:
                button = self._button(candidate, lambda _=False, text=candidate: self.choose(text))
                self.choice_layout.addWidget(button)
                button.show()
                self.buttons.append(button)
        self.status.setText(f'Correct “{wrong}”' if self.buttons else f'No suggestions for “{wrong}”. Type a replacement or say letters.')
        self.choices.setVisible(bool(self.buttons))
        self._editor.clear(); self._editor.show(); self.apply.show()
        self.apply.setEnabled(False)
        self.dismiss.setText("Keep original")
        self.always.hide(); self.vocabulary.hide()
        self.restyle()
        self._fit_rows()
        self.show()

    def _fit_rows(self):
        width = max(1, self.width())
        # Reserve the vertical scrollbar gutter even before Qt lays it out.
        height = self.choice_layout.heightForWidth(max(1, width - 12))
        self.choice_body.setMinimumHeight(height)
        self.choices.setFixedHeight(min(94, height))
        self._editor.setFixedWidth(min(240, width))
        self.actions.setFixedHeight(self.actions.layout().heightForWidth(width))
        self.layout().invalidate()
        self.setFixedHeight(self.layout().sizeHint().height())
        self.layout().activate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._fit_rows()
            self.on_changed()

    def restyle(self):
        self.setStyleSheet(
            f"QWidget {{ background: transparent; color: {theme.TEXT_PRIMARY}; "
            f"font-family: {theme.FONT_FAMILY}; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton,QLineEdit {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; "
            "border-radius: 8px; padding: 0 12px; } "
            f"QPushButton:disabled {{ color: {theme.TEXT_SECONDARY}; }} "
            f"QScrollBar:vertical {{ background: transparent; width: 10px; }} "
            f"QScrollBar::handle:vertical {{ background: {theme.BORDER}; min-height: 24px; }} "
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height: 0; }")

    def choose(self, text):
        if text == self.wrong:
            self._cancel_editor()
            return
        self.close_input()
        self.awaiting = True
        self.on_apply(text)

    def show_learning(self, wrong, right):
        self.close_input()
        self.awaiting, self.learning = False, True
        self.wrong, self.right = wrong, right
        self.status.setText(f'Changed “{wrong}” to “{right}”.')
        self.choices.hide(); self._editor.hide(); self.apply.hide()
        self.dismiss.setText("Done")
        self.vocabulary.setText(f'Add “{right}” to my words')
        store = get_dictionary_store(self.app)
        self.always.setEnabled(store is not None and wrong.casefold() != right.casefold())
        self.vocabulary.setEnabled(store is not None and not any(
            word.casefold() == right.casefold() for word in store.custom_vocab))
        self.always.show(); self.vocabulary.show()
        self._fit_rows()
        self.show()
        self.restore_passive()
        self.on_changed()

    def _save(self, vocabulary):
        CorrectionLearningOffer._save(self, vocabulary)
        self._fit_rows()
        self.on_changed()

    def _apply_editor(self):
        text = self._editor.text().strip()
        if text:
            self.choose(text)

    def _cancel_editor(self):
        self.finish()
        self.on_done()

    def finish(self):
        self.close_input()
        self.learning = self.awaiting = False
        if self.app is not None and getattr(self.app, "_draft_correction_menu", None) is self:
            self.app._draft_correction_menu = None
        self.hide()
        self.restore_passive()

    def restore_passive(self):
        window = self.window()
        if not window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus:
            visible = window.isVisible()
            window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
            if visible:
                window.show()

    def begin_input(self):
        self.close_input()
        if self.app is not None:
            self._spelling_input = SpellingInput(self, self.app)

    def close_input(self):
        if self._spelling_input is not None:
            self._spelling_input.close()
            self._spelling_input = None

    def hideEvent(self, event):
        self.close_input()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._editor:
            if event.type() == QEvent.Type.MouseButtonPress:
                window = self.window()
                if window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus:
                    window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
                    window.show()
                self._editor.setFocus(Qt.FocusReason.MouseFocusReason)
                self.begin_input()
            if event.type() == QEvent.Type.FocusIn:
                self.begin_input()
            elif event.type() == QEvent.Type.FocusOut:
                self.close_input()
            elif event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self._cancel_editor()
                return True
        return super().eventFilter(watched, event)

    def _announce(self, key, message):
        self.status.setText(message)


def offer_correction_learning(app, wrong: str, right: str):
    """Called only after an accepted edit, from Full or Compact."""
    wrong, right = str(wrong).strip(), str(right).strip()
    if not wrong or not right or wrong == right:
        return
    def show():
        menu = active_menu(app)
        if menu is not None and not menu.learning and menu.wrong == wrong:
            menu.show_learning(wrong, right)
            app._correction_learning_offer = menu
            return
        from shiboken6 import isValid
        old = getattr(app, "_correction_learning_offer", None)
        if old is not None and isValid(old):
            old.hide()
            if not isinstance(old, WordCorrectionMenu):
                old.deleteLater()
        offer = CorrectionLearningOffer(app, wrong, right)
        app._correction_learning_offer = offer
        offer.show()
    qt_runtime.post(show)


def cancel_pending_menu(app):
    menu = active_menu(app)
    if menu is not None and not menu.learning:
        menu.finish()


def active_menu(app):
    from shiboken6 import isValid
    menu = getattr(app, "_draft_correction_menu", None)
    return menu if menu is not None and isValid(menu) else None
