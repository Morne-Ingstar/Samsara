"""Explicit optional Hermes tool acknowledgement; no provider calls."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from samsara.ui import theme

# PLACEHOLDER COPY (539).
NOTICE = ("Hermes can use its own tools on this computer, under your own Hermes approval settings. "
          "Samsara sends Hermes what it heard you say.")


class HermesNotice(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Using Hermes")
        self.setAccessibleName(self.windowTitle())
        self.setAccessibleDescription(NOTICE)
        self.setModal(True)
        self.setFixedWidth(760)
        self.setStyleSheet(theme.build_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(theme.SPACE_2)
        heading = QLabel("Hermes (experimental)")
        heading.setProperty("role", "title")
        layout.addWidget(heading)
        label = QLabel(NOTICE)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch()
        for title, callback, primary in (("Not now", self.reject, False),
                                          ("Accept and enable Hermes", self.accept, True)):
            button = QPushButton(title)
            button.setAccessibleName(title)
            (theme.make_primary if primary else theme.make_secondary)(button)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        theme.apply_design_scale(self)
        self.ensurePolished()
        label.setFixedHeight(label.heightForWidth(self.width() - 56))
        self.adjustSize()


def request_acceptance(parent):
    return HermesNotice(parent).exec() == QDialog.DialogCode.Accepted
