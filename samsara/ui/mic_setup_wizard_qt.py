"""Guided microphone setup wizard for Samsara.

A simple 4-step wizard that walks new users through:
  1. Picking the right input device
  2. Checking their speaking level
  3. Testing wake word detection
  4. Confirming everything is set

Designed to be approachable -- no technical parameters, no jargon.
All the numbers happen behind the scenes.

Public API (same wrapper pattern as all Qt windows):
    MicSetupWizardQt(app).show()
"""

import collections
import threading
import time

import numpy as np
import sounddevice as sd

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout,
    QWidget,
)

from samsara.constants import DEFAULT_CAPTURE_RATE
from samsara.ui import qt_runtime

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_WARNING  = "#fbbf24"
_MUTED    = "#4a5568"

# Applied directly to primary buttons so Windows 11's native style engine
# can't override the colours through stylesheet inheritance.
_PRIMARY_SS = (
    f"QPushButton{{background:{_ACCENT};color:{_BG};"
    f"border:none;border-radius:4px;font-weight:bold;padding:6px 18px;}}"
    f"QPushButton:hover{{background:#4ab8c8;color:{_BG};}}"
    f"QPushButton:disabled{{background:{_ELEVATED};color:{_MUTED};"
    f"border:1px solid {_BORDER};}}"
)

_SS = f"""
QDialog, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QPushButton {{
    background: {_ELEVATED};
    color: {_TEXT_PRI};
    border: 1px solid {_BORDER};
    padding: 6px 18px;
    border-radius: 4px;
    min-width: 80px;
}}
QPushButton:hover {{
    background: {_ACCENT};
    color: {_BG};
    border-color: {_ACCENT};
}}
QPushButton#primary {{
    background: {_ACCENT};
    color: {_BG};
    border-color: {_ACCENT};
    font-weight: bold;
}}
QPushButton#primary:hover {{ background: #4ab8c8; }}
QPushButton#ghost {{
    background: transparent;
    color: {_TEXT_SEC};
    border: none;
}}
QPushButton#ghost:hover {{ color: {_TEXT_PRI}; background: transparent; }}
QComboBox {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    padding: 5px 10px;
    border-radius: 4px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {_SURFACE};
    color: {_TEXT_PRI};
    selection-background-color: {_ACCENT};
    selection-color: {_BG};
    border: 1px solid {_BORDER};
}}
QProgressBar {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {_SUCCESS}; border-radius: 5px; }}
"""

# Level zones expressed as a fraction of the normalised bar (0.0-1.0).
# Bar is normalised so that RMS 0.20 = full scale.
# Thresholds are intentionally generous: even a padded interface mic at
# 1 ft will read 0.01-0.03 RMS, which puts it comfortably in the green.
_ZONE_LOW   = 0.025    # below this (RMS < 0.005): near-silence, essentially off
_ZONE_HIGH  = 0.90     # above this (RMS > 0.18):  very loud, risk of clipping
_GREEN_DWELL_S = 1.5   # seconds level must stay green before Next enables

_OWW_PASS_THRESHOLD = 2
_OWW_ATTEMPTS       = 3
_OWW_ATTEMPT_TIMEOUT = 8.0
_OWW_NOISE_FLOOR    = 0.005
_OWW_TARGET_RMS     = 0.10


def _resample(audio, orig_sr, target_sr=16000):
    if orig_sr == target_sr or len(audio) == 0:
        return audio
    new_len = int(len(audio) * target_sr / orig_sr)
    return np.interp(
        np.linspace(0, len(audio) - 1, num=new_len),
        np.arange(len(audio)),
        audio,
    ).astype(np.float32)


def _query_input_devices():
    devices = []
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                devices.append((i, dev["name"]))
    except Exception:
        pass
    return devices


def _detect_capture_rate(device_index):
    try:
        info = sd.query_devices(device_index)
        rate = int(info.get("default_samplerate", DEFAULT_CAPTURE_RATE))
        return rate if rate > 0 else DEFAULT_CAPTURE_RATE
    except Exception:
        return DEFAULT_CAPTURE_RATE


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class MicSetupWizardQt:
    """Drop-in Qt wizard -- same open/close pattern as other Qt windows."""

    def __init__(self, app):
        self.app = app
        self._window: "_WizardWindow | None" = None
        self._init_posted = False

    @property
    def window(self):
        return self._window

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def close(self):
        if self._window is not None:
            qt_runtime.post(self._window.close)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _WizardWindow(self._app if hasattr(self, '_app') else self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
        self._init_posted = False

    # Keep attribute access consistent whether callers use app or _app
    @property
    def _app(self):
        return self.app


# ---------------------------------------------------------------------------
# Wizard window
# ---------------------------------------------------------------------------

class _WizardWindow(QDialog):

    _level_sig   = Signal(float)   # raw RMS from audio thread
    _oww_hit_sig = Signal()        # OWW detection from audio thread

    _STEP_DEVICE = 0
    _STEP_LEVEL  = 1
    _STEP_WAKE   = 2
    _STEP_DONE   = 3

    _STEP_TITLES = [
        "Choose your microphone",
        "Check your speaking level",
        "Test your wake word",
        "You're all set",
    ]

    def __init__(self, app):
        super().__init__()
        self._app = app

        # Single persistent audio stream — opened on first step,
        # closed only in closeEvent. Never reopened mid-navigation.
        self._stream          = None
        self._stream_lock     = threading.Lock()
        self._wizard_active   = False   # master flag for the audio worker
        self._selected_device = None    # sounddevice index (None = default)
        self._capture_rate    = DEFAULT_CAPTURE_RATE
        self._current_step    = -1      # set by _go_to()

        # Level state
        self._level_history   = collections.deque(maxlen=20)
        self._green_since     = None
        self._cal_threshold   = None

        # OWW state
        self._oww_detector    = None
        self._oww_running     = False   # True only during wake word step
        self._oww_hits        = 0
        self._oww_attempt_idx = 0
        self._attempt_started = None
        self._attempt_labels  = []
        self._oww_poll_timer  = None

        self.setWindowTitle("Microphone Setup")
        self.setFixedSize(560, 480)
        self.setStyleSheet(_SS)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        self._build_ui()
        self._level_sig.connect(self._on_level)
        self._oww_hit_sig.connect(self._on_oww_hit)
        self._go_to(self._STEP_DEVICE)

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header ----
        hdr = QWidget()
        hdr.setFixedHeight(60)
        hdr.setStyleSheet(
            f"background:{_SURFACE};border-bottom:1px solid {_BORDER};"
        )
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(24, 0, 24, 0)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            f"color:{_TEXT_PRI};font-size:15px;font-weight:bold;"
        )
        hdr_lay.addWidget(self._title_lbl, stretch=1)
        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        hdr_lay.addWidget(self._step_lbl)
        root.addWidget(hdr)

        # ---- Progress dots bar ----
        # Height must accommodate two lines: dot (10px) + label (14px) + spacing
        dots_bar = QWidget()
        dots_bar.setFixedHeight(52)
        dots_bar.setStyleSheet(
            f"background:{_SURFACE};border-bottom:1px solid {_BORDER};"
        )
        dots_lay = QHBoxLayout(dots_bar)
        dots_lay.setContentsMargins(24, 6, 24, 6)
        dots_lay.setSpacing(0)
        self._dots: list = []
        step_names = ["Device", "Level", "Wake word", "Done"]
        for i, name in enumerate(step_names):
            if i > 0:
                line = QFrame()
                line.setFixedHeight(2)
                line.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
                )
                line.setStyleSheet(f"background:{_BORDER};margin-bottom:14px;")
                dots_lay.addWidget(line)
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setContentsMargins(0, 0, 0, 0)
            dot = QLabel("*")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setStyleSheet(f"color:{_MUTED};font-size:14px;font-weight:bold;")
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{_MUTED};font-size:10px;")
            col.addWidget(dot)
            col.addWidget(lbl)
            container = QWidget()
            container.setFixedWidth(72)
            container.setLayout(col)
            dots_lay.addWidget(container)
            self._dots.append((dot, lbl))
        root.addWidget(dots_bar)

        # ---- Page stack ----
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_device_page())
        self._stack.addWidget(self._build_level_page())
        self._stack.addWidget(self._build_wake_page())
        self._stack.addWidget(self._build_done_page())
        root.addWidget(self._stack, stretch=1)

        # ---- Nav bar ----
        nav = QWidget()
        nav.setFixedHeight(64)
        nav.setStyleSheet(
            f"background:{_SURFACE};border-top:1px solid {_BORDER};"
        )
        nav_lay = QHBoxLayout(nav)
        nav_lay.setContentsMargins(24, 0, 24, 0)
        nav_lay.setSpacing(10)

        self._back_btn = QPushButton("Back")
        self._back_btn.setFixedWidth(88)
        self._back_btn.clicked.connect(self._go_back)
        nav_lay.addWidget(self._back_btn)
        nav_lay.addStretch()

        self._skip_btn = QPushButton("Skip")
        self._skip_btn.setObjectName("ghost")
        self._skip_btn.setFixedWidth(72)
        self._skip_btn.clicked.connect(self._skip_step)
        nav_lay.addWidget(self._skip_btn)

        self._next_btn = QPushButton("Next")
        self._next_btn.setStyleSheet(_PRIMARY_SS)
        self._next_btn.setFixedWidth(110)
        self._next_btn.clicked.connect(self._go_next)
        nav_lay.addWidget(self._next_btn)

        root.addWidget(nav)

    # ---- Page builders ----

    def _build_device_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(16)
        lay.addWidget(_body(
            "Select the microphone you'll be speaking into, then say a "
            "few words to confirm it's picking up your voice."
        ))
        row = QHBoxLayout()
        row.addWidget(_label("Microphone:"))
        self._device_combo = QComboBox()
        self._device_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._populate_devices()
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        row.addWidget(self._device_combo, stretch=1)
        lay.addLayout(row)
        lay.addWidget(_label("Signal:"))
        self._device_level_bar = _LevelBar()
        lay.addWidget(self._device_level_bar)
        self._device_status = QLabel("Say something to test the mic...")
        self._device_status.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        lay.addWidget(self._device_status)
        lay.addStretch()
        return page

    def _build_level_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(16)
        lay.addWidget(_body(
            "Speak at the distance and volume you'll normally use. "
            "Aim for the green zone -- it means Samsara will hear you "
            "clearly without picking up too much background noise."
        ))
        self._level_bar = _LevelBar()
        lay.addWidget(self._level_bar)
        self._level_hint = QLabel("")
        self._level_hint.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:12px;font-style:italic;"
        )
        self._level_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._level_hint)
        legend = QHBoxLayout()
        legend.addStretch()
        for color, text in [(_ERROR, "Too quiet"), (_SUCCESS, "Good"), (_WARNING, "Too loud")]:
            dot = QLabel("*")
            dot.setStyleSheet(f"color:{color};font-size:12px;font-weight:bold;")
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
            legend.addWidget(dot)
            legend.addWidget(lbl)
            legend.addSpacing(16)
        legend.addStretch()
        lay.addLayout(legend)
        lay.addStretch()
        self._cal_status = QLabel("")
        self._cal_status.setStyleSheet(
            f"color:{_SUCCESS};font-size:12px;font-weight:bold;"
        )
        self._cal_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._cal_status)
        return page

    def _build_wake_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(14)
        wake_phrase = self._app.config.get('wake_word_config', {}).get('phrase', 'Jarvis')
        self._wake_intro = _body(
            f'Say <b>"{wake_phrase.title()}"</b> three times at your normal '
            f'speaking volume. Each circle lights up when Samsara hears it.'
        )
        lay.addWidget(self._wake_intro)
        slots_row = QHBoxLayout()
        slots_row.addStretch()
        self._attempt_labels = []
        for i in range(_OWW_ATTEMPTS):
            slot = _AttemptSlot(i + 1)
            slots_row.addWidget(slot)
            if i < _OWW_ATTEMPTS - 1:
                slots_row.addSpacing(20)
            self._attempt_labels.append(slot)
        slots_row.addStretch()
        lay.addLayout(slots_row)
        self._oww_result_lbl = QLabel("")
        self._oww_result_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._oww_result_lbl.setWordWrap(True)
        self._oww_result_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        lay.addWidget(self._oww_result_lbl)
        lay.addStretch()
        self._oww_tip = QLabel("")
        self._oww_tip.setWordWrap(True)
        self._oww_tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._oww_tip.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:11px;font-style:italic;"
        )
        lay.addWidget(self._oww_tip)
        return page

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 36, 32, 20)
        lay.setSpacing(12)
        title = QLabel("Microphone is ready.")
        title.setStyleSheet(
            f"color:{_SUCCESS};font-size:18px;font-weight:bold;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        self._done_summary = QLabel("")
        self._done_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_summary.setWordWrap(True)
        self._done_summary.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        lay.addWidget(self._done_summary)
        lay.addStretch()
        adv_btn = QPushButton("Open Wake Word Debug (advanced)")
        adv_btn.setObjectName("ghost")
        adv_btn.clicked.connect(self._open_debug)
        adv_btn.setFixedWidth(280)
        adv_row = QHBoxLayout()
        adv_row.addStretch()
        adv_row.addWidget(adv_btn)
        adv_row.addStretch()
        lay.addLayout(adv_row)
        return page

    # ----------------------------------------------------------------
    # Step navigation
    # ----------------------------------------------------------------

    def _go_to(self, step: int):
        prev_step = self._current_step
        self._current_step = step

        # Stop OWW poll timer whenever we leave the wake word step
        if prev_step == self._STEP_WAKE:
            self._oww_running = False
            if self._oww_poll_timer is not None:
                self._oww_poll_timer.stop()
                self._oww_poll_timer = None

        self._stack.setCurrentIndex(step)

        # Header
        self._title_lbl.setText(self._STEP_TITLES[step])
        self._step_lbl.setText(f"Step {step + 1} of 4")

        # Dots
        for i, (dot, lbl) in enumerate(self._dots):
            if i < step:
                dot.setStyleSheet(f"color:{_SUCCESS};font-size:14px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{_SUCCESS};font-size:10px;")
            elif i == step:
                dot.setStyleSheet(f"color:{_ACCENT};font-size:14px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{_ACCENT};font-size:10px;font-weight:bold;")
            else:
                dot.setStyleSheet(f"color:{_MUTED};font-size:14px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{_MUTED};font-size:10px;")

        # Nav bar
        self._back_btn.setVisible(step > 0)
        if step == self._STEP_DONE:
            self._skip_btn.hide()
            self._next_btn.setText("Finish")
        else:
            self._skip_btn.show()
            self._next_btn.setText("Next  ->")

        # Step-specific setup
        if step == self._STEP_DEVICE:
            self._next_btn.setEnabled(True)
            self._ensure_audio_running()

        elif step == self._STEP_LEVEL:
            self._next_btn.setEnabled(False)
            self._next_btn.setText("Calibrate  ->")
            self._level_hint.setText("")
            self._cal_status.setText("")
            self._green_since = None
            self._level_history.clear()
            self._ensure_audio_running()

        elif step == self._STEP_WAKE:
            self._oww_hits = 0
            self._oww_attempt_idx = 0
            for slot in self._attempt_labels:
                slot.reset()
            self._oww_result_lbl.setText("")
            self._oww_result_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
            self._oww_tip.setText("")
            self._next_btn.setEnabled(False)
            self._next_btn.setText("Continue  ->")
            self._setup_oww_test()

        elif step == self._STEP_DONE:
            self._build_done_summary()

    def _go_next(self):
        step = self._current_step
        if step == self._STEP_LEVEL:
            self._do_calibrate()
        elif step == self._STEP_DONE:
            self._finish()
            return
        if step < self._STEP_DONE:
            self._go_to(step + 1)

    def _go_back(self):
        if self._current_step > 0:
            self._go_to(self._current_step - 1)

    def _skip_step(self):
        if self._current_step < self._STEP_DONE:
            self._go_to(self._current_step + 1)

    # ----------------------------------------------------------------
    # Single persistent audio worker
    #
    # One thread runs for the lifetime of the wizard.  It reads chunks
    # from the selected device and emits _level_sig on every chunk.
    # When _current_step == _STEP_WAKE and _oww_running is True it also
    # runs OWW detection.  No stream is ever opened or closed mid-
    # navigation -- this eliminates the WASAPI device-conflict crash.
    # ----------------------------------------------------------------

    def _ensure_audio_running(self):
        """Start the audio worker if it isn't already running."""
        if not self._wizard_active:
            self._wizard_active = True
            self._selected_device = self._device_combo.currentData()
            self._capture_rate = _detect_capture_rate(self._selected_device)
            threading.Thread(
                target=self._audio_worker,
                daemon=True,
                name="wizard-audio",
            ).start()

    def _audio_worker(self):
        """Persistent audio loop. Runs until _wizard_active is False.

        When device changes, the inner read-loop exits (device mismatch),
        the stream is closed, and the outer loop immediately reopens a
        stream for the new device -- seamless from the UI's perspective.
        """
        while self._wizard_active:
            device = self._selected_device
            capture_rate = _detect_capture_rate(device)
            blocksize = int(capture_rate * 0.1)
            stream = None
            try:
                stream = sd.InputStream(
                    samplerate=capture_rate,
                    channels=1,
                    dtype=np.float32,
                    device=device,
                    blocksize=blocksize,
                )
                stream.start()
                with self._stream_lock:
                    self._stream = stream

                while self._wizard_active and self._selected_device == device:
                    try:
                        data, _ = stream.read(blocksize)
                        chunk = data.flatten()
                        rms = float(np.sqrt(np.mean(chunk ** 2)))
                        self._level_sig.emit(rms)

                        # OWW detection only when wake word step is active
                        if (self._current_step == self._STEP_WAKE
                                and self._oww_running
                                and self._oww_detector is not None):
                            oww_chunk = _resample(chunk, capture_rate, 16000)
                            if rms > _OWW_NOISE_FLOOR:
                                gain = min(_OWW_TARGET_RMS / rms, 20.0)
                                oww_chunk = np.clip(oww_chunk * gain, -1.0, 1.0)
                            if self._oww_detector.detected(oww_chunk):
                                self._oww_hit_sig.emit()

                        time.sleep(0.08)
                    except Exception:
                        break

            except Exception as exc:
                print(f"[WIZARD] Audio stream error: {exc}")
                time.sleep(0.3)
            finally:
                with self._stream_lock:
                    if self._stream is stream:
                        self._stream = None
                if stream:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass

    def _stop_audio(self):
        """Signal the worker to exit and close the current stream."""
        self._wizard_active = False
        self._oww_running = False
        with self._stream_lock:
            stream = self._stream
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Level monitoring
    # ----------------------------------------------------------------

    def _populate_devices(self):
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._input_devices = _query_input_devices()
        current_cfg = self._app.config.get('microphone')
        default_idx = 0
        self._device_combo.addItem("System default", userData=None)
        for i, (dev_idx, name) in enumerate(self._input_devices):
            self._device_combo.addItem(name, userData=dev_idx)
            if dev_idx == current_cfg:
                default_idx = i + 1
        if current_cfg is None:
            default_idx = 0
        self._device_combo.setCurrentIndex(default_idx)
        self._device_combo.blockSignals(False)
        self._selected_device = self._device_combo.currentData()

    def _on_device_changed(self, _index: int):
        """When the user picks a new device, update the flag.

        The audio worker's inner loop detects the device mismatch and
        exits; the outer loop reopens a stream for the new device.
        No explicit restart is needed -- the worker handles it.
        """
        self._selected_device = self._device_combo.currentData()
        self._capture_rate = _detect_capture_rate(self._selected_device)

    def _on_level(self, rms: float):
        level = min(rms / 0.20, 1.0)
        step = self._current_step

        if step == self._STEP_DEVICE:
            self._device_level_bar.set_level(level)
            if rms > 0.008:
                self._device_status.setText("Signal detected -- mic is working.")
                self._device_status.setStyleSheet(f"color:{_SUCCESS};font-size:12px;")
            else:
                self._device_status.setText("Say something to test the mic...")
                self._device_status.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")

        elif step == self._STEP_LEVEL:
            self._level_bar.set_level(level)
            self._level_history.append(rms)
            now = time.monotonic()

            if level < _ZONE_LOW:
                self._level_hint.setText(
                    "Essentially silent -- check the mic is connected and selected above."
                )
                self._level_hint.setStyleSheet(f"color:{_ERROR};font-size:12px;")
                self._green_since = None
            elif level > _ZONE_HIGH:
                self._level_hint.setText(
                    "Very loud -- you may get clipping. Back off slightly or reduce gain."
                )
                self._level_hint.setStyleSheet(f"color:{_WARNING};font-size:12px;")
                self._green_since = None
            else:
                self._level_hint.setText("Level looks good -- keep talking naturally.")
                self._level_hint.setStyleSheet(f"color:{_SUCCESS};font-size:12px;")
                if self._green_since is None:
                    self._green_since = now

            if (level >= _ZONE_LOW and level <= _ZONE_HIGH
                    and self._green_since is not None
                    and (now - self._green_since) >= _GREEN_DWELL_S
                    and not self._next_btn.isEnabled()):
                self._next_btn.setEnabled(True)
                self._cal_status.setText(
                    "Calibrate & Continue when you're happy with the level."
                )

    def _do_calibrate(self):
        if not self._level_history:
            return
        avg_rms = float(np.mean(list(self._level_history)))
        threshold = max(avg_rms * 0.25, 0.003)
        self._cal_threshold = threshold
        cfg = self._app.config.get('wake_word_config', {})
        if 'audio' not in cfg:
            cfg['audio'] = {}
        cfg['audio']['speech_threshold'] = round(threshold, 5)
        self._app.update_config({'wake_word_config': cfg}, save=True)
        self._cal_status.setText(
            f"Calibrated -- threshold set to {threshold:.4f}"
        )

    # ----------------------------------------------------------------
    # Wake word test
    # ----------------------------------------------------------------

    def _setup_oww_test(self):
        """Initialise OWW detector and start the attempt timer."""
        wake_phrase = self._app.config.get('wake_word_config', {}).get('phrase', 'jarvis')
        oww_threshold = float(
            self._app.config.get('wake_word_config', {}).get('oww_threshold', 0.2)
        )
        try:
            from samsara.wake_detector import WakeWordDetector
            self._oww_detector = WakeWordDetector(wake_phrase, threshold=oww_threshold)
        except Exception as exc:
            print(f"[WIZARD] WakeWordDetector init failed: {exc}")
            self._oww_detector = None

        if self._oww_detector is None or not self._oww_detector.is_available:
            self._oww_result_lbl.setText(
                f'No built-in model for "{wake_phrase}" -- '
                f"Whisper handles detection instead (no live preview here)."
            )
            self._oww_tip.setText(
                'Use "Test Wake Word..." in Settings -> Advanced to run a live test.'
            )
            self._next_btn.setEnabled(True)
            return

        self._oww_running = True
        self._attempt_started = time.monotonic()
        self._ensure_audio_running()

        self._oww_poll_timer = QTimer(self)
        self._oww_poll_timer.setInterval(400)
        self._oww_poll_timer.timeout.connect(self._oww_poll)
        self._oww_poll_timer.start()

    def _oww_poll(self):
        """Qt-thread timer: advance to the next slot when one times out."""
        if not self._oww_running:
            self._oww_poll_timer.stop()
            self._oww_poll_timer = None
            return
        if (self._oww_attempt_idx < _OWW_ATTEMPTS
                and self._attempt_started is not None
                and time.monotonic() - self._attempt_started > _OWW_ATTEMPT_TIMEOUT):
            self._advance_attempt(hit=False)

    def _on_oww_hit(self):
        if self._oww_attempt_idx < _OWW_ATTEMPTS:
            self._advance_attempt(hit=True)

    def _advance_attempt(self, hit: bool):
        idx = self._oww_attempt_idx
        if idx >= _OWW_ATTEMPTS:
            return
        self._attempt_labels[idx].set_result(hit)
        if hit:
            self._oww_hits += 1
        self._oww_attempt_idx += 1
        self._attempt_started = time.monotonic()

        if self._oww_attempt_idx >= _OWW_ATTEMPTS:
            self._finish_oww_test()
        else:
            wake_phrase = self._app.config.get(
                'wake_word_config', {}
            ).get('phrase', 'Jarvis')
            self._oww_result_lbl.setText(
                f'{self._oww_hits}/{self._oww_attempt_idx} heard -- '
                f'say "{wake_phrase.title()}" again.'
            )

    def _finish_oww_test(self):
        self._oww_running = False
        if self._oww_poll_timer is not None:
            self._oww_poll_timer.stop()
            self._oww_poll_timer = None

        passed = self._oww_hits >= _OWW_PASS_THRESHOLD
        if passed:
            self._oww_result_lbl.setText(
                f"Detected {self._oww_hits}/{_OWW_ATTEMPTS} -- wake word is working."
            )
            self._oww_result_lbl.setStyleSheet(
                f"color:{_SUCCESS};font-size:12px;font-weight:bold;"
            )
        else:
            self._oww_result_lbl.setText(
                f"Only detected {self._oww_hits}/{_OWW_ATTEMPTS} times."
            )
            self._oww_result_lbl.setStyleSheet(
                f"color:{_WARNING};font-size:12px;font-weight:bold;"
            )
            tips = (
                "Try speaking more directly toward the mic and a little slower."
                if self._oww_hits == 0 else
                "Speak at a steady, natural pace -- don't rush the word."
            )
            if self._oww_hits < _OWW_PASS_THRESHOLD:
                tips += (
                    "  If it keeps missing, lower 'Wake word sensitivity' "
                    "in Settings -> Advanced (try 0.10)."
                )
            self._oww_tip.setText(tips)

        self._next_btn.setEnabled(True)

    # ----------------------------------------------------------------
    # Done page
    # ----------------------------------------------------------------

    def _build_done_summary(self):
        parts = []
        parts.append(f"Microphone:  {self._device_combo.currentText()}")
        if self._cal_threshold is not None:
            parts.append(
                f"Speech threshold:  calibrated ({self._cal_threshold:.4f})"
            )
        else:
            parts.append("Speech threshold:  using existing setting")
        if self._oww_hits > 0:
            parts.append(
                f"Wake word:  detected {self._oww_hits}/{_OWW_ATTEMPTS} during test"
            )
        else:
            parts.append("Wake word:  test skipped or not applicable")
        self._done_summary.setText("\n".join(parts))

    def _finish(self):
        dev_idx = self._device_combo.currentData()
        if dev_idx is not None:
            try:
                self._app.update_config_and_save({'microphone': dev_idx})
            except Exception:
                pass
        self.close()

    def _open_debug(self):
        try:
            self._app.open_wake_word_debug()
        except Exception:
            pass

    # ----------------------------------------------------------------
    # Cleanup
    # ----------------------------------------------------------------

    def closeEvent(self, e):
        self._stop_audio()
        if self._oww_poll_timer is not None:
            self._oww_poll_timer.stop()
            self._oww_poll_timer = None
        e.accept()


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------

class _LevelBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(24)
        self._bar.setStyleSheet(
            f"QProgressBar{{background:{_SURFACE};border:1px solid {_BORDER};"
            f"border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{_SUCCESS};border-radius:4px;}}"
        )
        lay.addWidget(self._bar)

    def set_level(self, level: float):
        pct = int(min(max(level, 0.0), 1.0) * 100)
        self._bar.setValue(pct)
        if level < _ZONE_LOW:
            chunk_color = _ERROR
        elif level > _ZONE_HIGH:
            chunk_color = _WARNING
        else:
            chunk_color = _SUCCESS
        self._bar.setStyleSheet(
            f"QProgressBar{{background:{_SURFACE};border:1px solid {_BORDER};"
            f"border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{chunk_color};border-radius:4px;}}"
        )


class _AttemptSlot(QWidget):
    _SIZE = 64

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self._number = number
        self.setFixedSize(self._SIZE, self._SIZE + 22)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._circle = QLabel(str(number))
        self._circle.setFixedSize(self._SIZE, self._SIZE)
        self._circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_style(str(number), _ELEVATED, _TEXT_SEC, _BORDER)
        lay.addWidget(self._circle)
        self._lbl = QLabel("waiting")
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setStyleSheet(f"color:{_MUTED};font-size:10px;")
        lay.addWidget(self._lbl)

    def _apply_style(self, text, bg, fg, border):
        self._circle.setText(text)
        self._circle.setStyleSheet(
            f"border-radius:{self._SIZE // 2}px;"
            f"background:{bg};"
            f"color:{fg};"
            f"font-size:18px;font-weight:bold;"
            f"border:2px solid {border};"
        )

    def reset(self):
        self._apply_style(str(self._number), _ELEVATED, _TEXT_SEC, _BORDER)
        self._lbl.setText("waiting")
        self._lbl.setStyleSheet(f"color:{_MUTED};font-size:10px;")

    def set_result(self, heard: bool):
        if heard:
            self._apply_style("OK", _SUCCESS, _BG, _SUCCESS)
            self._lbl.setText("heard")
            self._lbl.setStyleSheet(
                f"color:{_SUCCESS};font-size:10px;font-weight:bold;"
            )
        else:
            self._apply_style("--", _ELEVATED, _ERROR, _ERROR)
            self._lbl.setText("missed")
            self._lbl.setStyleSheet(f"color:{_ERROR};font-size:10px;")


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{_TEXT_PRI};font-size:13px;")
    return lbl
