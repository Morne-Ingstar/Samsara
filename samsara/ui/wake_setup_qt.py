"""Small wake choice and first-run capture adapters for the reused guide (919)."""
from copy import deepcopy
import threading
import time

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from samsara.wake_setup import (calibration_needed, modelled_phrases,
                                MORE_WAKE_WORDS, FALLBACK_NOTE)
from samsara.ui import theme

# PLACEHOLDER COPY (919)
CALIBRATION_NOTE = 'Your wake word needs a quick calibration'


def configure_phrase_choice(combo, phrase, layout, *, fallback_pending=False):
    """Offer the proven phrase; keep future choices visibly non-selectable."""
    phrases = modelled_phrases()
    combo.clear()
    for value in phrases:
        combo.addItem(value.title(), value)
    combo.setEditable(False)
    combo.addItem(MORE_WAKE_WORDS)
    coming_soon = combo.model().item(combo.count() - 1)
    coming_soon.setEnabled(False)
    coming_soon.setSelectable(False)
    coming_soon.setForeground(theme.qcolor(theme.TEXT_DISABLED))
    combo.setCurrentIndex(0)
    note = QLabel(FALLBACK_NOTE)
    note.setWordWrap(True)
    note.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;')
    layout.addWidget(note)
    note.setVisible(fallback_pending or (bool(phrase) and phrase not in phrases))
    combo.setMinimumWidth(200)
    return note


class SetupWakeApp:
    """Unsaved setup configuration; never touches a half-built DictationApp.

    The normal guide uses production ACE. Before startup there is no ACE, so
    a short native-rate stream supplies the same 16 kHz frames to the quiet
    measurement. Save happens on Qt completion, never from a cancelled worker.
    """
    def __init__(self, config, microphones):
        self.config = config
        self.microphones = microphones
        self.last_wake_calibration = None

    def get_available_microphones(self):
        return self.microphones

    def update_config(self, updates, save=True):
        self.config.update(deepcopy(updates))

    def calibrate_wake_mic(self, seconds=3.0, cancel_event=None):
        import sounddevice as sd
        from samsara.audio_devices import detect_capture_rate
        from samsara.audio_engine.wake_prefilter import NativeRateFrames, chunk_rms
        from samsara.audio_engine.frame import FRAME_MS
        from samsara.ui.mic_setup_wizard_qt import _capture_channels

        cancel = cancel_event if cancel_event is not None else threading.Event()
        device = self.config.get('microphone')
        frames = NativeRateFrames(detect_capture_rate(device))
        rms = []
        deadline = time.monotonic() + seconds + 2.0
        with sd.InputStream(device=device, samplerate=frames.native_rate,
                            channels=_capture_channels(device), dtype='float32',
                            blocksize=frames.blocksize) as stream:
            while len(rms) < round(seconds * 1000 / FRAME_MS) and time.monotonic() < deadline:
                if cancel.is_set():
                    return None
                block, overflowed = stream.read(frames.blocksize)
                if overflowed:
                    continue
                rms.append(chunk_rms(frames.convert(block[:, 0])))
        if cancel.is_set() or len(rms) < round(seconds * 1000 / FRAME_MS * 0.8):
            self.last_wake_calibration = {'status': 'insufficient', 'message':
                'Calibration did not get enough audio. Nothing was saved; try again.'}  # PLACEHOLDER COPY (919)
            return None
        floor = max(float(np.median(rms)), 0.0005)
        if not np.isfinite(floor):
            return None
        self.last_wake_calibration = {'status': 'ok', 'frames': len(rms),
                                      'seconds': len(rms) * FRAME_MS / 1000, 'floor': floor}
        return floor


def refresh_tutorial_wake(window):
    """Recheck before teaching the wake phrase, and after returning from calibration."""
    from samsara.ui.mic_setup_wizard_qt import open_wake_calibration
    from PySide6.QtWidgets import QWidget
    frame = window.findChild(QWidget, 'tutorialWay_wake')
    if frame is None:
        return
    button = frame.findChild(QPushButton, 'tutorialWakeCalibrate')
    if button is None:
        button = QPushButton('Calibrate wake word')  # PLACEHOLDER COPY (919)
        button.setObjectName('tutorialWakeCalibrate')
        theme.make_secondary(button)
        frame.layout().addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)

        def run():
            guide = open_wake_calibration(window._app, parent=window)
            guide.finished.connect(lambda _: refresh_tutorial_wake(window))
        button.clicked.connect(run)
    needed = calibration_needed(window._app.config)
    button.setVisible(needed)
    button.setToolTip(CALIBRATION_NOTE)
    # The instruction is replaced until calibration, so learning never claims it is ready.
    detail = frame.findChild(QLabel, 'tutorialWakeInstruction')
    if detail is not None:
        detail.setText(CALIBRATION_NOTE if needed else detail.property('wakeInstruction'))
