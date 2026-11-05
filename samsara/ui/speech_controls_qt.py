"""Small keyboard-accessible speech controls shared by the reading windows."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from samsara.tts.speech_control import HINT_TEXT, repeat_speech, stop_speaking
from samsara.ui import theme


class SpeechControls(QWidget):
    def __init__(self, app=None, parent=None):
        super().__init__(parent)
        self.app = app
        self._hint_serial = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        row = QHBoxLayout()
        self.stop = QPushButton("Stop")  # PLACEHOLDER COPY (1149)
        self.stop.setAccessibleName("Stop speaking")
        self.stop.setToolTip("Stop this reading; keep your draft and pending questions.")
        self.repeat = QPushButton("Repeat")  # PLACEHOLDER COPY (1149)
        self.repeat.setAccessibleName("Repeat the last reading")
        self.repeat.setToolTip("Read the last spoken text again, from the beginning.")
        for button in (self.stop, self.repeat):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            theme.make_secondary(button)
            row.addWidget(button)
        row.addStretch()
        column.addLayout(row)
        self.hint = QLabel(HINT_TEXT)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent; border: none;")
        self.hint.hide()
        column.addWidget(self.hint)
        self.stop.clicked.connect(self._stop)
        self.repeat.clicked.connect(self._repeat)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        coordinator = getattr(self.app, "audio_coordinator", None)
        speaking = coordinator is not None and coordinator.is_speaking is True
        self.stop.setEnabled(speaking)
        self.repeat.setEnabled(bool(getattr(coordinator, "_speech_replay", None)))
        if not speaking or self._hint_serial != getattr(coordinator, "_speech_item_serial", None):
            self.hint.hide()

    def show_hint(self):
        self.refresh()
        if not self.isVisible() or not self.stop.isEnabled():
            return False
        self._hint_serial = self.app.audio_coordinator._speech_item_serial
        self.hint.show()
        self.window().adjustSize()
        return True

    def _stop(self):
        stop_speaking(self.app)
        self.refresh()

    def _repeat(self):
        repeat_speech(self.app)
        self.refresh()


def add_tray_controls(menu, app):
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None or coordinator.is_speaking is not True:
        return
    action = menu.addAction("Stop speaking")  # PLACEHOLDER COPY (1149)
    action.triggered.connect(lambda: stop_speaking(app))
    focus = menu.addAction("Show speech controls")  # PLACEHOLDER COPY (1149)
    from samsara.ui.ava_captions_qt import focus_controls
    focus.triggered.connect(lambda: focus_controls(app))
