"""Reusable network-voice acknowledgement; never starts a speech engine."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
)
from samsara.ui import theme

ACCEPTANCE_KEY = 'tts.network_voice_accepted'
NOTICE_HEADING = 'Network voice'  # PLACEHOLDER COPY (377)


def notice_facts():
    return (
        'The text of everything Samsara says aloud is sent to Microsoft to make speech, including replies, read-backs of your dictation and remembered facts it speaks.',
        'The network voice needs an internet connection.',
        'Nothing is sent to the voice service while the local voice is selected.',
    )


class NetworkVoiceNotice(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Using the network voice')
        self.setAccessibleName(self.windowTitle())
        self.setAccessibleDescription('\n\n'.join(notice_facts()))
        self.setModal(True)
        self.setFixedWidth(760)
        self.setStyleSheet(theme.build_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(theme.SPACE_2)
        heading = QLabel(NOTICE_HEADING)
        heading.setStyleSheet(f'font-size:{theme.TYPE_HEADING}px;font-weight:600;')
        layout.addWidget(heading)
        for fact in notice_facts():
            label = QLabel(fact)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            label.setStyleSheet(f'font-size:{theme.TYPE_BODY}px;color:{theme.TEXT_PRIMARY};')
            layout.addWidget(label)
        layout.addSpacing(theme.SPACE_2)
        row = QHBoxLayout()
        row.addStretch()
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
        theme.apply_design_scale(self)
        self.ensurePolished()
        content_width = self.width() - layout.contentsMargins().left() - layout.contentsMargins().right()
        for label in self.findChildren(QLabel):
            label.setFixedHeight(label.heightForWidth(content_width) if label.wordWrap() else label.sizeHint().height())
        self.adjustSize()


def request_acceptance(parent):
    dialog = NetworkVoiceNotice(parent)
    return dialog.exec() == QDialog.DialogCode.Accepted
