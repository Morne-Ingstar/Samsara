"""A standalone Qt recorder: only a held key/button, no desktop injection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .audio import Capture, control_phrase, local_model
from .storage import Session


class RecorderWindow(QMainWindow):
    pressed = Signal()
    released = Signal()

    def __init__(self, session, capture, model=None):
        super().__init__()
        self.session, self.capture, self.model = session, capture, model
        self.held = self.busy = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="corpus-save")
        self.future = None
        self.setWindowTitle("Record voice corpus")
        self.resize(820, 620)
        self.setMinimumSize(640, 520)
        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        self.progress = QLabel()
        self.instruction = QLabel()
        self.line = QLabel()
        self.line.setWordWrap(True)
        self.line.setTextFormat(Qt.TextFormat.PlainText)
        self.line.setStyleSheet("font-size: 28px; font-weight: 600;")
        self.instruction.setWordWrap(True)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        self.help = QLabel('Hold Space or Mouse 5 to speak. Release to save.\n'
                           'Hold and say “again”, “skip”, or “pause session” for controls.')
        self.help.setWordWrap(True)
        self.device_label = QLabel("Microphone: " + str(capture.device.get("name", "Test input")) +
                                  ("\nInput snapshot supplied; Daily match unverified; gain unchanged" if
                                   capture.settings.get("microphone_name") or capture.settings.get("microphone") is not None
                                   else "\nDaily match unverified — system default input; gain unchanged"))
        self.device_label.setWordWrap(True)
        for widget in (self.progress, self.instruction, self.line):
            layout.addWidget(widget)
        layout.addStretch()
        for widget in (self.status, self.help, self.device_label):
            layout.addWidget(widget)
        buttons = QHBoxLayout()
        self.buttons = []
        for label, action in (("Again", lambda: self.control("again")),
                              ("Skip", lambda: self.control("skip")),
                              ("Pause session", self.toggle_pause)):
            button = QPushButton(label)
            button.setMinimumHeight(40)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(action)
            self.buttons.append(button)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.pressed.connect(self.begin)
        self.released.connect(self.end)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(50)
        self.refresh()

    def refresh(self):
        item = self.session.current
        self.progress.setText(f"{self.session.completed} / {len(self.session.script)} completed" +
                              (f" · Sitting {item['sitting']} of 4" if item else ""))
        self.instruction.setText(item["instruction"] if item else "All lines are saved. Close whenever you are ready.")
        self.line.setText(item["intended_text"] or "[Say nothing]" if item else "Session complete")
        paused = self.session.state["paused"]
        self.buttons[-1].setText("Resume session" if paused else "Pause session")
        if paused:
            self.status.setText("Paused. Press Space or Mouse 5 once to resume.")
        self.help.setVisible(self.model is not None)

    def begin(self):
        if self.busy or self.held:
            return
        if self.session.state["paused"]:
            self.session.state["paused"] = False
            self.session.persist()
            self.status.setText("Ready — hold again to record")
            self.refresh()
            return
        if self.session.current:
            self.capture.start_take()
            self.held = True
            self.status.setText("Recording — release to save")
            self.buttons_enabled(False)

    def end(self):
        if not self.held:
            return
        self.held = False
        self.busy = True
        self.status.setText("Saving the ending…")
        self.future = self.pool.submit(self.finish)

    def finish(self):
        pcm, released, statuses = self.capture.release()
        try:
            phrase = control_phrase(self.model, pcm) if self.model is not None else None
        except Exception:
            # Voice controls must never make a completed recording disappear.
            phrase = None
            statuses.append("voice controls unavailable for this take")
        if phrase:
            self.session.control(phrase)
            return "Control accepted"
        self.session.save(pcm, released, device_name=self.capture.device["name"], capture_status=statuses)
        return "Saved — hold for the next line" + (" (audio warning; consider Again)" if statuses else "")

    def poll(self):
        if self.future is None or not self.future.done():
            return
        try:
            self.status.setText(self.future.result())
        except Exception:
            self.status.setText("Could not save. Your previous takes are safe. Retry this line.")
        self.future = None
        self.busy = False
        self.buttons_enabled(True)
        self.refresh()

    def buttons_enabled(self, enabled):
        for button in self.buttons:
            button.setEnabled(enabled)

    def control(self, phrase):
        if not self.busy and not self.held:
            self.session.control(phrase)
            self.status.setText("Ready")
            self.refresh()

    def toggle_pause(self):
        if self.session.state["paused"]:
            self.session.state["paused"] = False
            self.session.persist()
            self.status.setText("Ready")
            self.refresh()
        else:
            self.control("pause session")

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.pressed.emit()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.released.emit()
        else:
            super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.ForwardButton:
            self.pressed.emit()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.ForwardButton:
            self.released.emit()
        else:
            super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):
        self.end()
        super().focusOutEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.end()
        super().changeEvent(event)

    def closeEvent(self, event):
        if self.held:
            self.end()
        if self.busy:
            self.status.setText("Finishing this take. Close again after it is saved.")
            event.ignore()
            return
        self.pool.shutdown(wait=False)
        event.accept()


def run(root, settings):
    from .storage import recording_lock, recording_root
    root = recording_root(root)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from samsara.ui import theme
    app.setStyleSheet(theme.build_stylesheet())
    # Load the control model before opening any microphone stream.
    model = local_model(settings.get("model_path"))
    with recording_lock(root), Capture(settings) as capture:
        window = RecorderWindow(Session(root), capture, model)
        window.show()
        return app.exec()
