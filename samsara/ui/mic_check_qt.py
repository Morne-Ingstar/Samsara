"""Quick microphone check; capture and decoding stay off the Qt thread."""
from dataclasses import dataclass, replace
import threading
import time

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

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
        if time.monotonic() < self.flash_until or self.level_dbfs >= -6:
            description = "Too loud"
        else:
            description = "Too quiet" if self.level_dbfs < check.QUIET_DBFS else "Good"
        self.setAccessibleDescription(description)
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


@dataclass(frozen=True)
class CheckTake:
    measurements: dict
    audio: np.ndarray
    heard: str | None = None
    saved: bool | None = None
    volume: float | None = None
    hands_free: bool = False
    repeat_heard: str | None = None
    repeat_result: dict | None = None


class MicCheckWindow(QDialog):
    progress = Signal(str, object)

    def __init__(self, app, *, source_factory=None, transcriber=None, save=None,
                 inspect=check.inspect_microphone, parent=None):
        super().__init__(parent)
        self.app = app
        self.source_factory, self.transcriber = source_factory, transcriber
        self.save = save or check.save_result
        self.inspect = inspect
        self.cancel = threading.Event()
        self.busy = False
        self.discarding = False
        self.timed_out = False
        self.pending_take = self.take = None
        self.sink = self.play_buffer = None
        self.timeout = QTimer(self, singleShot=True, interval=check.CHECK_TIMEOUT_MS,
                              timerType=Qt.TimerType.PreciseTimer)
        self.timeout.timeout.connect(self.on_timeout)
        self.setWindowTitle("Check my mic")
        self.resize(680, 510)
        self.setStyleSheet(theme.build_stylesheet() + f"QDialog {{background:{theme.BG0}; color:{theme.TEXT_PRIMARY};}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)
        title = QLabel("Check my mic")
        title.setStyleSheet(f"font-size:{theme.TYPE_HUB_TITLE}px; font-weight:600;")
        layout.addWidget(title)
        self.microphone = self.plain_label()
        self.microphone.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        layout.addWidget(self.microphone)

        self.capture_panel = QWidget()
        capture_layout = QVBoxLayout(self.capture_panel)
        capture_layout.setContentsMargins(0, 8, 0, 0)
        capture_layout.setSpacing(16)
        self.bar = LevelBar()
        capture_layout.addWidget(self.bar)
        self.noise = QLabel("Background noise: not measured")
        self.sentence = self.plain_label(f"Say: “{check.SENTENCE}”")
        for label in (self.noise, self.sentence):
            capture_layout.addWidget(label)
        layout.addWidget(self.capture_panel)
        self.instruction = self.plain_label("Stay quiet for five seconds, then read the sentence when asked.")
        layout.addWidget(self.instruction)

        self.results = QWidget()
        results_layout = QVBoxLayout(self.results)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(0)
        grid.setColumnMinimumWidth(0, 92)
        grid.setColumnMinimumWidth(1, 148)
        grid.setColumnStretch(2, 1)
        self.rows = []
        grid.addWidget(self.rule(), 0, 0, 1, 3)
        for i, name in enumerate(check.ROWS, 1):
            label, verdict, sentence = QLabel(name), self.plain_label(), self.plain_label()
            label.setStyleSheet("font-weight:600;")
            for column, widget in enumerate((label, verdict, sentence)):
                widget.setMinimumHeight(41)
                grid.addWidget(widget, 2 * i - 1, column)
            grid.addWidget(self.rule(), 2 * i, 0, 1, 3)
            self.rows.append((label, verdict, sentence))
        results_layout.addLayout(grid)
        self.heard_title = QLabel("Samsara heard")
        self.heard_title.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        self.heard = self.plain_label()
        results_layout.addWidget(self.heard_title)
        results_layout.addWidget(self.heard)
        layout.addWidget(self.results)
        layout.addStretch()

        self.details = self.plain_label()
        self.details.setStyleSheet(f"color:{theme.TEXT_SECONDARY};")
        layout.addWidget(self.details)
        self.details_sound = QPushButton("Open Windows sound settings")
        self.details_sound.setAutoDefault(False)
        theme.make_secondary(self.details_sound)
        layout.addWidget(self.details_sound, alignment=Qt.AlignmentFlag.AlignLeft)
        self.footer_rule = self.rule()
        layout.addWidget(self.footer_rule)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.play_button = QPushButton("Play what Samsara heard")
        self.start_button = QPushButton("Start check")
        self.details_button = QPushButton("Details")
        self.details_button.setCheckable(True)
        self.sound_button = QPushButton("Open Windows sound settings")
        self.cancel_button = QPushButton("Cancel check")
        for button in (self.play_button, self.start_button, self.details_button, self.sound_button, self.cancel_button):
            theme.make_secondary(button)
            button.setAutoDefault(False)
            row.addWidget(button)
        theme.make_primary(self.start_button)
        row.addStretch()
        layout.addLayout(row)
        self.privacy = QLabel("Audio is never saved. Closing this window or checking again clears it.")
        self.privacy.setStyleSheet(f"color:{theme.TEXT_SECONDARY}; font-size:{theme.TYPE_MIN}px;")
        layout.addWidget(self.privacy)
        self.start_button.clicked.connect(self.start_check)
        self.cancel_button.clicked.connect(self.cancel_check)
        self.sound_button.clicked.connect(self.open_sound_settings)
        self.details_sound.clicked.connect(self.open_sound_settings)
        self.play_button.clicked.connect(self.play)
        self.details_button.toggled.connect(self.toggle_details)
        self.progress.connect(self.on_progress)
        self.clear_result()
        self.cancel_button.hide()
        self.refresh_device()
        theme.apply_design_scale(self)

    @staticmethod
    def plain_label(text=""):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def rule():
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.BORDER};")
        return line

    def refresh_device(self):
        previous = getattr(self, "device", None)
        self.device = check.selected_device(self.app)
        self.microphone.setText("Microphone: " + self.device["name"])
        if previous is not None and previous != self.device:
            self.clear_result()
            self.instruction.setText("Microphone changed. Check this microphone to see its results.")

    def clear_result(self):
        self.stop_playback()
        self.pending_take = self.take = None
        self.heard.clear()
        self.results.hide()
        self.details_button.setChecked(False)
        self.details.clear()
        for widget in (self.details, self.details_sound, self.play_button,
                       self.details_button, self.footer_rule, self.privacy):
            widget.hide()
        self.capture_panel.show()
        self.instruction.show()
        self.instruction.setText("Stay quiet for five seconds, then read the sentence when asked.")
        self.sentence.setText(f"Say: “{check.SENTENCE}”")
        self.noise.setText("Background noise: not measured")
        self.bar.flash_until = 0
        self.bar.feed(dict(rms_dbfs=-60, clipped_fraction=0))
        self.sound_button.show()

    def open_sound_settings(self):
        if not QDesktopServices.openUrl(QUrl("ms-settings:sound")):
            self.set_instruction("Open Windows Settings > System > Sound > Input to change the microphone level.")

    def set_instruction(self, text, *, announce=False):
        self.instruction.setText(text)
        self.instruction.show()
        if announce:
            QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self.instruction, text))

    def start_check(self):
        if self.busy:
            return
        if getattr(self.app, "recording", False) or getattr(self.app, "_mic_score_active", False):
            self.set_instruction("Finish the current recording or microphone test, then check again.")
            return
        from samsara.runtime import thread_registry
        from samsara.mic_score import DictationTranscriber, open_source
        self.refresh_device()
        device = dict(self.device)
        self.clear_result()
        self.cancel.clear()
        self.discarding = self.timed_out = False
        self.busy = True
        self.app._mic_score_active = True  # Existing wake/hold admission guard.
        self.start_button.setEnabled(False)
        self.cancel_button.show()
        self.sound_button.setEnabled(False)
        self.set_instruction("Getting the microphone ready…")
        self.noise.setText("Background noise: measuring…")
        deadline = time.monotonic() + check.CHECK_TIMEOUT_MS / 1000
        self.timeout.start()

        def capture(*, repeat=False):
            source = self.source_factory(device) if self.source_factory else open_source(self.app, device)
            try:
                return check.capture(source, self.cancel, self.progress.emit, repeat=repeat)
            finally:
                source.close()

        def ensure_current():
            if self.cancel.is_set():
                raise InterruptedError
            if check.selected_device(self.app) != device:
                raise RuntimeError("The selected microphone changed.")

        def work():
            try:
                audio, quiet = capture()
                ensure_current()
                take = CheckTake(check.evaluate(audio, quiet), audio)
                self.progress.emit("decoding", take)

                transcribe = self.transcriber or DictationTranscriber(self.app)

                def decode(pcm):
                    try:
                        heard, _ = transcribe(pcm)
                        return heard
                    except Exception:
                        return None

                heard = decode(audio)
                ensure_current()
                take = replace(take, heard=heard)
                self.progress.emit("measured", take)

                verify_word = check.first_word_missing(check.SENTENCE, heard)
                verify_gap = take.measurements["possible_dropout"]
                repeat_time = check.REPEAT_PAUSE_S + check.REPEAT_S + 1
                if (verify_word or verify_gap) and time.monotonic() + repeat_time < deadline:
                    try:
                        repeated, pause = capture(repeat=True)
                        repeat_result = check.evaluate(repeated, pause)
                        self.progress.emit("decoding", take)
                        confirmed = decode(repeated) if verify_word else None
                        take = replace(take, repeat_heard=confirmed, repeat_result=repeat_result)
                    except (OSError, RuntimeError, ValueError):
                        pass  # The first take still stands if the follow-up could not run.
                    ensure_current()
                    self.progress.emit("measured", take)

                volume, hands_free = self.inspect(device["name"])
                ensure_current()
                take = replace(take, volume=volume, hands_free=hands_free)
                self.progress.emit("measured", take)
                saved = True
                try:
                    self.save(take.measurements, device["name"])
                except OSError:
                    saved = False
                event = ("result", replace(take, saved=saved))
            except InterruptedError:
                event = ("error", "Check cancelled. Choose Check again when you are ready.")
            except Exception:
                event = ("error", "The microphone check could not finish. Check the connection and try again.")
            finally:
                self.app._mic_score_active = False
            self.progress.emit(*event)

        try:
            thread_registry.spawn("mic-check", work, daemon=True)
        except Exception:
            self.app._mic_score_active = False
            self.on_progress("error", "The microphone check could not start. Check again.")

    def on_progress(self, kind, value):
        if kind in ("result", "error"):
            self.timeout.stop()
            self.busy = False
            self.start_button.setEnabled(True)
            self.sound_button.setEnabled(True)
            self.cancel_button.hide()
            self.pending_take = None
            if self.discarding or self.timed_out:
                return
            self.start_button.setText("Check again")
            if kind == "error":
                self.set_instruction(value, announce=True)
            elif self.cancel.is_set():
                self.set_instruction("Check cancelled. Choose Check again when you are ready.", announce=True)
            elif self.device != check.selected_device(self.app):
                self.refresh_device()
            else:
                self.show_result(value)
            return

        if self.discarding or self.timed_out or self.cancel.is_set():
            return
        if kind == "level":
            self.bar.feed(value)
        elif kind == "quiet":
            self.set_instruction("Step 1 of 2. Stay quiet for five seconds.", announce=True)
        elif kind == "noise":
            self.noise.setText("Background noise: " + check.noise_label(value))
        elif kind == "speak":
            self.set_instruction("Step 2 of 2. Say the sentence now.", announce=True)
        elif kind == "repeat_pause":
            self.sentence.setText(f"Say: “{check.REPEAT_SENTENCE}”")
            self.set_instruction("One more sentence. Wait for the cue.", announce=True)
        elif kind == "repeat_speak":
            self.set_instruction("Say the second sentence now.", announce=True)
        elif kind in ("decoding", "measured"):
            self.pending_take = value
            if kind == "decoding":
                self.set_instruction("Checking what Samsara heard…", announce=True)

    def show_result(self, take):
        self.take = take
        self.capture_panel.hide()
        self.instruction.hide()
        self.sound_button.hide()
        findings, show_heard = check.checklist(
            take.measurements, take.heard, hands_free=take.hands_free,
            repeat_heard=take.repeat_heard, repeat_result=take.repeat_result,
        )
        colors = dict(success=theme.SUCCESS, warning=theme.WARNING, error=theme.ERROR, neutral=theme.TEXT_SECONDARY)
        for (_, verdict, sentence), finding in zip(self.rows, findings):
            verdict.setText(finding.verdict)
            verdict.setStyleSheet(f"color:{colors[finding.tone]}; font-weight:600;")
            sentence.setText(finding.sentence)
        self.heard.setText(f"“{take.heard}”" if show_heard else "")
        self.heard_title.setVisible(show_heard)
        self.heard.setVisible(show_heard)
        self.results.show()
        self.start_button.setText("Check again")
        for widget in (self.play_button, self.details_button, self.footer_rule, self.privacy):
            widget.show()
        if take.saved is False:
            self.set_instruction("The measurements could not be saved.")
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self.results, "Microphone check complete."))

    def toggle_details(self, shown):
        self.details_button.setText("Hide details" if shown else "Details")
        if self.take is None:
            return
        result = self.take.measurements
        background = "Unavailable" if result["muted_quiet"] else f'{result["noise_dbfs"]:.0f} dBFS'
        margin = "Unavailable" if result["muted_quiet"] or result["level"] == "no_speech" else f'{result["snr_db"]:.0f} dB'
        volume = "Unavailable" if self.take.volume is None else f"{self.take.volume:.0%}"
        self.details.setText(
            f'Voice level: {result["speech_dbfs"]:.0f} dBFS    ·    Background: {background}\n'
            f'Voice above background: {margin}    ·    Windows input volume: {volume}'
        )
        self.details.setVisible(shown)
        self.details_sound.setVisible(shown and result["level"] == "too_loud")

    def play(self):
        if self.sink is not None:
            self.stop_playback()
            return
        self.refresh_device()
        if self.take is None:
            return
        from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices, QtAudio

        output = QMediaDevices.defaultAudioOutput()
        audio_format = QAudioFormat()
        audio_format.setSampleRate(check.SAMPLE_RATE)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if output.isNull() or not output.isFormatSupported(audio_format):
            self.set_instruction("Playback is unavailable on the selected speakers or headphones.")
            return
        self.instruction.hide()
        self.play_buffer = QBuffer(self)
        pcm = np.rint(np.clip(self.take.audio, -1, 1) * 32767).astype("<i2")
        self.play_buffer.setData(pcm.tobytes())
        self.play_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.sink = QAudioSink(output, audio_format, self)
        sink, buffer = self.sink, self.play_buffer
        self.play_button.setText("Stop playback")

        def playback_state(state):
            if self.sink is not sink or not isValid(sink):
                return
            if state == QAudio.State.IdleState and buffer.atEnd():
                self.stop_playback()
            elif state == QAudio.State.StoppedState and sink.error() != QtAudio.Error.NoError:
                self.stop_playback()
                self.set_instruction("The recording could not be played.")

        self.sink.stateChanged.connect(playback_state)
        self.sink.start(self.play_buffer)

    def stop_playback(self):
        if self.sink is not None:
            self.sink.stateChanged.disconnect()
            self.sink.stop()
            self.sink.deleteLater()
            self.play_buffer.close()
            self.play_buffer.setData(b"")
            self.play_buffer.deleteLater()
            self.sink = self.play_buffer = None
        self.play_button.setText("Play what Samsara heard")

    def cancel_check(self):
        self.cancel.set()
        self.pending_take = None
        self.set_instruction("Cancelling the check…")

    def on_timeout(self):
        take, self.pending_take = self.pending_take, None
        self.timed_out = True
        self.cancel.set()
        self.cancel_button.hide()
        self.start_button.setText("Check again")
        if take is not None and self.device == check.selected_device(self.app):
            self.show_result(take)
        else:
            self.set_instruction("The microphone check took too long. Check again.", announce=True)

    def done(self, result):
        self.discarding = True
        self.cancel.set()
        self.timeout.stop()
        self.clear_result()
        self.cancel_button.hide()
        super().done(result)


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
