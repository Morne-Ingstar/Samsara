"""Loss-safe, themed decision for the in-memory draft before quitting."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from samsara.ui import theme


class QuitDraftDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.choice = "keep"
        self.setWindowTitle("Pending draft")
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setStyleSheet(f"""
            QDialog {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}
            QLabel {{ color: {theme.TEXT_PRIMARY}; background: transparent; }}
            QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 8px 16px; }}
            QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        label = QLabel("A draft is waiting. Send it before quitting, discard it, or cancel quitting?", self)
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        self.action_buttons = []
        # Stand-in copy, pending the owner's three write asks in 501/ASK.json.
        for text, choice in (("Send it", "send"), ("Discard it", "discard"),
                             ("Keep Samsara open", "keep")):
            button = QPushButton(text, self)
            button.setAccessibleName(text)
            button.setMinimumHeight(44)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _=False, c=choice: self._choose(c))
            row.addWidget(button)
            self.action_buttons.append(button)
        layout.addLayout(row)
        self.action_buttons[-1].setDefault(True)
        self.action_buttons[-1].setFocus()
        self.setMinimumWidth(520)

    def _choose(self, choice):
        self.choice = choice
        self.accept()

    def reject(self):
        self.choice = "keep"
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()


def ask_quit_draft():
    """Called from the existing quit decision on the shared Qt thread."""
    dialog = QuitDraftDialog()
    dialog.exec()
    return dialog.choice
