"""Quick microphone check; capture and decoding stay off the Qt thread."""
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from samsara import mic_check as check
from samsara.ui import theme


class LevelBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.level_dbfs = -60
        self.flash_until = 0
        self.flash_timer = QTimer(self)
        self.flash_timer.setSingleShot(True)
        self.flash_timer.timeout.connect(self.update)
        self.setMinimumHeight(54)
        self.setAccessibleName("Live microphone level: too quiet, good, too loud")

    def feed(self, frame):
        self.level_dbfs = frame["rms_dbfs"]
        if frame["clipped_fraction"] > 0:
            self.flash_until = time.monotonic() + .4
            self.flash_timer.start(420)
        self.setAccessibleDescription(f"{self.level_dbfs:.0f} dBFS" + (", clipping" if time.monotonic() < self.flash_until else ""))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        width = self.width()
        quiet = (check.QUIET_DBFS + 60) / 60
        loud = .9
        for start, end, color in ((0, quiet, theme.TEXT_SECONDARY), (quiet, loud, theme.ACCENT), (loud, 1, theme.ERROR)):
            painter.fillRect(round(start * width), 4, round((end - start) * width), 14, theme.qcolor(color))
        position = round(min(1, max(0, (self.level_dbfs + 60) / 60)) * (width - 4))
        painter.fillRect(position, 0, 4, 23, QColor(theme.TEXT_PRIMARY))
        if time.monotonic() < self.flash_until:
            painter.fillRect(width - 10, 0, 10, 23, theme.qcolor(theme.ERROR))
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        for x, span, alignment, label in ((0, 150, Qt.AlignmentFlag.AlignLeft, "Too quiet"),
                                         (150, width - 300, Qt.AlignmentFlag.AlignCenter, "Good"),
                                         (width - 150, 150, Qt.AlignmentFlag.AlignRight, "Too loud")):
            painter.drawText(x, 28, span, 24, alignment, label)


class MicCheckWindow(QDialog):
    progress = Signal(str, object)

    def __init__(self, app, *, source_factory=None, transcriber=None, save=None, parent=None):
        super().__init__(parent)
        self.app = app
        self.source_factory, self.transcriber = source_factory, transcriber
        self.save = save or check.save_result
        self.cancel = threading.Event()
        self.busy = False
        self.setWindowTitle("Check my mic")
        self.resize(680, 510)
        self.setStyleSheet(theme.build_stylesheet() + f"QDialog {{background:{theme.BG0}; color:{theme.TEXT_PRIMARY};}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(theme.hub_header("Check my mic", "A quick check of your voice level and background noise."))
        self.microphone = QLabel()
        self.microphone.setTextFormat(Qt.TextFormat.PlainText)
        self.microphone.setWordWrap(True)
        layout.addWidget(self.microphone)
        self.bar = LevelBar()
        layout.addWidget(self.bar)
        self.noise = QLabel("Background noise: not measured")
        layout.addWidget(self.noise)
        # PLACEHOLDER COPY (1120)
        self.instruction = QLabel("Stay quiet for a moment, then read the sentence when asked.")
        self.sentence = QLabel(f"Say: “{check.SENTENCE}”")
        self.heard = QLabel("Samsara heard: not checked yet")
        self.verdict = QLabel("Choose Start check when you are ready.")
        for label in (self.instruction, self.sentence, self.heard, self.verdict):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        self.verdict.setStyleSheet(f"font-size:{theme.TYPE_HEADING}px; font-weight:600;")
        layout.addStretch()
        row = QHBoxLayout()
        self.start_button = QPushButton("Start check")
        self.sound_button = QPushButton("Open Windows sound settings")
        self.cancel_button = QPushButton("Cancel check")
        for button in (self.start_button, self.sound_button, self.cancel_button):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            theme.make_secondary(button)
            row.addWidget(button)
        theme.make_primary(self.start_button)
        row.addStretch()
        layout.addLayout(row)
        self.cancel_button.hide()
        self.start_button.clicked.connect(self.start_check)
        self.cancel_button.clicked.connect(self.cancel.set)
        self.sound_button.clicked.connect(self.open_sound_settings)
        self.progress.connect(self.on_progress)
        self.refresh_device()
        theme.apply_design_scale(self)

    def refresh_device(self):
        previous = getattr(self, "device", None)
        self.device = check.selected_device(self.app)
        self.microphone.setText("Microphone: " + self.device["name"])
        if previous is not None and previous != self.device:
            self.verdict.setText("Microphone changed. Check this microphone before using the previous result.")
            self.heard.setText("Samsara heard: not checked yet")
            self.noise.setText("Background noise: not measured")
            self.bar.flash_until = 0
            self.bar.feed(dict(rms_dbfs=-60, clipped_fraction=0))

    def open_sound_settings(self):
        if not QDesktopServices.openUrl(QUrl("ms-settings:sound")):
            self.instruction.setText("Open Windows Settings > System > Sound > Input to change the microphone level.")

    def start_check(self):
        if self.busy:
            return
        if getattr(self.app, "recording", False) or getattr(self.app, "_mic_score_active", False):
            self.instruction.setText("Finish the current recording or microphone test, then check again.")
            return
        from samsara.runtime import thread_registry
        from samsara.mic_score import DictationTranscriber, open_source
        self.refresh_device()
        device = dict(self.device)
        self.cancel.clear()
        self.busy = True
        self.app._mic_score_active = True  # Existing wake/hold admission guard.
        self.start_button.setEnabled(False)
        self.cancel_button.show()
        self.sound_button.setEnabled(False)
        self.heard.setText("Samsara heard: waiting for the sentence")
        self.verdict.setText("Checking…")
        self.noise.setText("Background noise: measuring…")
        self.bar.feed(dict(rms_dbfs=-60, clipped_fraction=0))
        self.bar.flash_until = 0

        def work():
            source = None
            try:
                source = self.source_factory(device) if self.source_factory else open_source(self.app, device)
                audio, quiet = check.capture(source, self.cancel, self.progress.emit)
                captured_source, source = source, None
                captured_source.close()
                if check.selected_device(self.app) != device:
                    raise RuntimeError("The selected microphone changed. Check again.")
                result = check.evaluate(audio, quiet)
                self.progress.emit("decoding", None)
                try:
                    heard, _ = (self.transcriber or DictationTranscriber(self.app))(audio)
                except Exception:
                    heard = None
                if self.cancel.is_set():
                    raise InterruptedError
                if check.selected_device(self.app) != device:
                    raise RuntimeError("The selected microphone changed. Check again.")
                saved = True
                try:
                    self.save(result, device["name"])
                except OSError:
                    saved = False
                event = ("result", (result, heard, saved))
            except InterruptedError:
                event = ("error", "Check cancelled. Choose Check again when you are ready.")
            except Exception:
                event = ("error", "The microphone check could not finish. Check the connection and try again.")
            finally:
                try:
                    if source is not None:
                        source.close()
                except Exception:
                    event = ("error", "The microphone connection could not close cleanly. Check again.")
                finally:
                    self.app._mic_score_active = False
            self.progress.emit(*event)

        try:
            thread_registry.spawn("mic-check", work, daemon=True)
        except Exception:
            self.app._mic_score_active = False
            self.on_progress("error", "The microphone check could not start. Check again.")

    def on_progress(self, kind, value):
        if kind == "level":
            self.bar.feed(value)
        elif kind == "quiet":
            self.instruction.setText("Step 1 of 2: stay quiet while we measure background noise…")
        elif kind == "noise":
            self.noise.setText("Background noise: " + check.noise_label(value))
        elif kind == "speak":
            self.instruction.setText("Step 2 of 2: say the sentence now. Listening for eight seconds…")
        elif kind == "decoding":
            self.instruction.setText("Checking what Samsara heard…")
        elif kind in ("result", "error"):
            self.busy = False
            self.start_button.setEnabled(True)
            self.start_button.setText("Check again")
            self.sound_button.setEnabled(True)
            self.cancel_button.hide()
            if kind == "error":
                self.instruction.setText(value)
                self.verdict.setText("No result. Check again.")
                self.heard.setText("Samsara heard: not checked")
            else:
                result, heard, saved = value
                self.verdict.setText(result["message"])
                self.heard.setText("Samsara heard: " + (heard if heard else "nothing recognised" if heard == "" else "transcription unavailable"))
                self.noise.setText("Background noise: " + result["noise_level"])
                self.instruction.setText("Check complete. Levels saved; no audio saved." if saved else "Check complete. The diagnostics result could not be saved.")

    def reject(self):
        self.cancel.set()
        super().reject()

    def closeEvent(self, event):
        self.cancel.set()
        super().closeEvent(event)


def open_mic_check(app):
    from samsara.ui import qt_runtime

    def show():
        window = getattr(app, "_mic_check_window", None)
        if window is None:
            window = MicCheckWindow(app)
            app._mic_check_window = window
        if not window.busy:
            window.refresh_device()
        window.show()
        window.raise_()
        window.activateWindow()
    qt_runtime.post(show)
