"""Reusable network-voice acknowledgement; never starts a speech engine."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from samsara.ui import theme

ACCEPTANCE_KEY = 'tts.network_voice_accepted'


def notice_facts():
    # PLACEHOLDER COPY (373): wording can change; these three facts cannot.
    return (
        'The text of everything Samsara says aloud is sent to Microsoft to make speech, including replies, read-backs of your dictation and remembered facts it speaks.',
        'The network voice needs an internet connection.',
        'Nothing is sent to the voice service while the local voice is selected.',
    )


class NetworkVoiceNotice(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        # PLACEHOLDER COPY (373)
        self.setWindowTitle('Using the network voice')
        self.setAccessibleName(self.windowTitle())
        self.setAccessibleDescription('\n\n'.join(notice_facts()))
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.build_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        text_layout = QVBoxLayout(body)
        text_layout.setContentsMargins(0, 0, 0, 0)
        for fact in notice_facts():
            label = QLabel(fact)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet(f'font-size:{theme.TYPE_BODY}px;color:{theme.TEXT_PRIMARY};')
            text_layout.addWidget(label)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        row = QHBoxLayout()
        row.addStretch()
        # PLACEHOLDER COPY (373)
        for title, callback, primary in (
            ('Not now', self.reject, False),
            ('I understand, use the network voice', self.accept, True),
        ):
            button = QPushButton(title)
            button.setAccessibleName(title)
            (theme.make_primary if primary else theme.make_secondary)(button)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.resize(760, 430)
        theme.apply_design_scale(self)


def request_acceptance(parent):
    dialog = NetworkVoiceNotice(parent)
    return dialog.exec() == QDialog.DialogCode.Accepted
