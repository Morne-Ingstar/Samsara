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
import math
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

from samsara.constants import ADAPTIVE_SPEECH_FLOOR_RATIO, DEFAULT_CAPTURE_RATE, DEFAULT_WAKE_PHRASE
from samsara.audio_devices import detect_capture_rate as _detect_capture_rate
from samsara.audio_engine import guide_capture
from samsara.audio_engine.ring import EMPTY
from samsara.audio_engine.wake_prefilter import NativeRateFrames, chunk_rms, oww_prefilter, pcm_to_float
from samsara.runtime import thread_registry
from samsara.speech_pace import measured_interword_pauses, recommend_profile
from samsara.ui import qt_runtime, theme
from samsara.audio_devices import pick_index_by_name

from samsara.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Colours -- sourced from samsara.ui.theme (the shared design system this
# wizard is the accent reference for). Local aliases kept so the rest of
# this file's ~90 usage sites don't need touching; only the source of truth
# moved, not the call sites or the resulting look.
# ---------------------------------------------------------------------------


# Level zones expressed as a fraction of the normalised bar (0.0-1.0).
# Bar is normalised so that RMS 0.20 = full scale.
# Thresholds are intentionally generous: even a padded interface mic at
# 1 ft will read 0.01-0.03 RMS, which puts it comfortably in the green.
_ZONE_LOW   = 0.025    # below this (RMS < 0.005): near-silence, essentially off
_ZONE_HIGH  = 0.90     # above this (RMS > 0.18):  very loud, risk of clipping
_GREEN_DWELL_S = 1.5   # seconds level must stay green before Next enables

_OWW_PASS_THRESHOLD = 2
_OWW_ATTEMPTS       = 3
# Guidance audit 2026-09-13 row 912: there is no "Test Wake Word..." control
# anywhere in Settings; the live detector test is this wizard's own step 3
# and the debug window on its last page.
_OWW_NO_MODEL_TIP = ('For a live detector test, click "Open Wake Word Debug (advanced)" '
                     'on the last page of this guide.')
_WAKE_OFF_NOTE = ('Wake word is currently off (Settings -> Modes -> Wake word). '
                  'This test still runs; turn it on to use the phrase day to day.')
_OWW_ATTEMPT_TIMEOUT = 8.0
# What the detector is FED (67): the live wake path's own frames and gain.
# On the level and wake steps the guide reads the AudioCaptureEngine ring --
# the frames WakeConsumer reads -- and feeds OpenWakeWord through
# samsara.audio_engine.wake_prefilter.oww_prefilter, the function the live
# consumer calls. This file must not grow its own resampler or gain constants
# (tests/test_wizard_wake_signal_path.py): its old sounddevice stream scored
# 0.007-0.058 on words the live detector scored 0.977 / 0.436 (2026-09-15).
# How the step JUDGES an attempt (51): derived from the measured background
# floor when there is one (see _derive_wake_levels); these are the fallbacks
# when no calibration is available.
_OWW_REARM_RMS      = 0.010     # fallback speech level / re-arm level
_OWW_REARM_CHUNKS   = 3         # quiet 100 ms chunks that re-arm after a hit
_OWW_REARM_MAX_S    = 1.5       # ...or this long after a hit, whatever the room does


def _derive_wake_levels(floor):
    """Speech and re-arm levels for the wake step from the measured background
    floor (RMS of the production 16 kHz ring, from calibrate_wake_mic or the
    saved wake_word_config.audio.measured_noise_floor).

    speech level = floor x ADAPTIVE_SPEECH_FLOOR_RATIO (1.5, the live adaptive
    gate's own ratio): a chunk at or below it is indistinguishable from the
    room. Re-arming after a detection needs 3 chunks at or below that level --
    the room at its own floor always qualifies, so re-arm never depends on the
    room going quieter than it can -- or _OWW_REARM_MAX_S, whichever is first.
    Returns {"source": "measured"|"defaults", "floor", "speech_rms", "rearm_rms"}.
    """
    try:
        value = float(floor)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return {"source": "defaults", "floor": None,
                "speech_rms": _OWW_REARM_RMS, "rearm_rms": _OWW_REARM_RMS}
    speech = value * ADAPTIVE_SPEECH_FLOOR_RATIO
    return {"source": "measured", "floor": value, "speech_rms": speech, "rearm_rms": speech}


class _AttemptStats:
    """What one wake attempt actually received. Written by the audio worker,
    summarised on the Qt thread; guarded by _WizardWindow._attempt_lock."""

    __slots__ = ("frames", "fed", "disarmed", "rms_sum", "peak_rms", "max_score")

    def __init__(self):
        self.frames = 0        # 100 ms chunks that arrived during the attempt
        self.fed = 0           # chunks the detector actually scored
        self.disarmed = 0      # chunks skipped while waiting to re-arm
        self.rms_sum = 0.0
        self.peak_rms = 0.0
        self.max_score = None  # highest OpenWakeWord score seen (None = never fed)


def _summarize_attempt(stats, levels, threshold, hit):
    """(reason, plain-words text) for one attempt. reason is one of
    detected | no_audio | never_armed | below_speech_level | score_below_threshold."""
    mean = stats.rms_sum / stats.frames if stats.frames else 0.0
    score = "never scored" if stats.max_score is None else f"{stats.max_score:.2f}"
    numbers = (f"{stats.frames} chunks, {stats.fed} scored; level mean {mean:.4f} peak {stats.peak_rms:.4f} "
               f"(speech level {levels['speech_rms']:.4f}); best score {score} vs threshold {threshold:.2f}")
    if hit:
        return "detected", f"heard it ({numbers})"
    if stats.frames == 0:
        return "no_audio", f"no audio arrived from the microphone ({numbers})"
    if stats.fed == 0:
        return "never_armed", f"the detector was still waiting to re-arm after the last detection ({numbers})"
    if stats.peak_rms <= levels["speech_rms"]:
        return "below_speech_level", (f"your voice never reached the detector above the room noise: "
                                      f"the loudest sound stayed below the speech level ({numbers})")
    return "score_below_threshold", f"speech arrived, but the wake-word score stayed below the threshold ({numbers})"


def _capture_channels(device) -> int:
    """Channel count for the guide's own preview stream: the endpoint's full
    count, of which column 0 is used. A blocking channels=1 read on a
    2-channel WASAPI endpoint (the owner's Focusrite) returned samples with
    their time structure destroyed -- lag-1 autocorrelation 0.000 against
    0.74 for the engine's callback stream on the same mic -- while a
    channels=2 blocking read's column 0 matched the engine sample for sample
    (reports/67 artifacts)."""
    try:
        info = sd.query_devices(device, kind='input')
        return max(1, int(info.get('max_input_channels', 1)))
    except Exception:
        return 1


# _detect_capture_rate is samsara.audio_devices.detect_capture_rate (shared
# with voice training's transient-stream fallback).

#: How long the Qt thread waits for the wizard's own audio worker to close
#: its stream before a production microphone switch. Bounded by one 100 ms
#: read once the stream is closed.
_AUDIO_JOIN_TIMEOUT_S = 3.0


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
    _wake_cal_done_sig = Signal(int, object, str, object)  # generation, floor, error, report
    _mic_switch_done_sig = Signal(bool, str)       # ok, error message

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

        # One guide-owned preview stream at a time. It is paused while the
        # production ACE stream is switched or performs quiet calibration.
        self._stream          = None    # the worker's open stream (read-only here: only the worker closes it)
        self._stream_lock     = threading.Lock()
        self._audio_thread    = None    # the running _audio_worker thread
        self._audio_stop      = None    # that worker's own stop Event
        self._switch_in_flight = False  # production mic switch running off the UI thread
        self._closed          = False   # set by closeEvent, cleared by showEvent
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
        self._oww_armed       = True
        self._oww_quiet_chunks = 0
        self._oww_hit_at      = None    # monotonic time of the last detection
        self._oww_threshold   = 0.2
        self._oww_levels      = None    # _derive_wake_levels() result for this test
        self._wake_floor      = None    # floor measured just before this test
        self._attempt_lock    = threading.Lock()
        self._attempt_stats   = _AttemptStats()
        self._attempt_notes   = []      # one plain-words line per finished attempt
        self._wake_cal_generation = 0
        self._wake_cal_cancel = None

        self.setWindowTitle("Microphone Setup")
        self.setFixedSize(560, 480)
        self.setStyleSheet(theme.build_stylesheet())
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        self._build_ui()
        self._level_sig.connect(self._on_level)
        self._oww_hit_sig.connect(self._on_oww_hit)
        self._wake_cal_done_sig.connect(self._on_wake_calibration_result)
        self._mic_switch_done_sig.connect(self._on_microphone_switch_done)
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
            f"background:{theme.BG1};border-bottom:1px solid {theme.BORDER};"
        )
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(24, 0, 24, 0)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_EMPHASIS}px;font-weight:bold;"
        )
        hdr_lay.addWidget(self._title_lbl, stretch=1)
        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        hdr_lay.addWidget(self._step_lbl)
        root.addWidget(hdr)

        # ---- Progress dots bar ----
        # Height must accommodate two lines: dot (10px) + label (14px) + spacing
        dots_bar = QWidget()
        dots_bar.setFixedHeight(52)
        dots_bar.setStyleSheet(
            f"background:{theme.BG1};border-bottom:1px solid {theme.BORDER};"
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
                line.setStyleSheet(f"background:{theme.BORDER};margin-bottom:14px;")
                dots_lay.addWidget(line)
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setContentsMargins(0, 0, 0, 0)
            dot = QLabel("*")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_BODY}px;font-weight:bold;")
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_MIN}px;")
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
        theme.style_footer(nav)
        nav_lay = QHBoxLayout(nav)
        nav_lay.setContentsMargins(24, 0, 24, 0)
        nav_lay.setSpacing(10)

        self._back_btn = QPushButton("Back")
        theme.make_secondary(self._back_btn)
        self._back_btn.setFixedWidth(88)
        self._back_btn.clicked.connect(self._go_back)
        nav_lay.addWidget(self._back_btn)
        nav_lay.addStretch()

        self._skip_btn = QPushButton("Skip")
        theme.make_ghost(self._skip_btn)
        self._skip_btn.setFixedWidth(72)
        self._skip_btn.clicked.connect(self._skip_step)
        nav_lay.addWidget(self._skip_btn)

        self._next_btn = QPushButton("Next")
        theme.make_primary(self._next_btn)
        self._next_btn.clicked.connect(self._go_next)
        nav_lay.addWidget(self._next_btn)
        # Minimum, not fixed -- text varies by step ("Next  ->" / "Calibrate
        # ->" / "Continue  ->" / "Finish") and a fixed 110px clipped the two
        # longest ones by 12-13px. Measured after addWidget() parents the
        # button into the styled tree -- fontMetrics() on an unparented
        # widget doesn't reflect the cascaded stylesheet font size yet.
        self._next_btn.setMinimumWidth(_button_min_width(
            self._next_btn, ["Next  ->", "Calibrate  ->", "Continue  ->", "Finish"]
        ))

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
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(80)
        refresh_btn.clicked.connect(self._on_refresh_devices)
        row.addWidget(refresh_btn)
        lay.addLayout(row)
        lay.addWidget(_label("Signal:"))
        self._device_level_bar = _LevelBar()
        lay.addWidget(self._device_level_bar)
        self._device_status = QLabel("Say something to test the mic...")
        self._device_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
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
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;"
        )
        self._level_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._level_hint)
        pace_check = QPushButton("Check speech pace (optional)")
        pace_check.clicked.connect(self._recommend_speech_pace)
        lay.addWidget(pace_check, alignment=Qt.AlignmentFlag.AlignCenter)
        self._pace_recommendation = QLabel("Read one sentence at your normal pace, then choose whether to use the recommendation.")
        self._pace_recommendation.setWordWrap(True)
        self._pace_recommendation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pace_recommendation.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._pace_recommendation)
        legend = QHBoxLayout()
        legend.addStretch()
        for color, text in [(theme.ERROR, "Too quiet"), (theme.SUCCESS, "Good"), (theme.WARNING, "Too loud")]:
            dot = QLabel("*")
            dot.setStyleSheet(f"color:{color};font-size:{theme.TYPE_MIN}px;font-weight:bold;")
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            legend.addWidget(dot)
            legend.addWidget(lbl)
            legend.addSpacing(16)
        legend.addStretch()
        lay.addLayout(legend)
        lay.addStretch()
        self._cal_status = QLabel("")
        self._cal_status.setStyleSheet(
            f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
        )
        self._cal_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._cal_status)
        return page

    def _recommend_speech_pace(self):
        """Offer, never save, a VAD-style recommendation from recent RMS frames."""
        samples = list(self._level_history)
        pauses = measured_interword_pauses(samples, 10, speech_threshold=_ZONE_LOW)
        choice = recommend_profile(pauses)
        self._pace_recommendation.setText(
            f"Suggested: {choice.replace('_', ' ')}. Choose it in Settings → Modes if it feels right; nothing was changed.")

    def _build_wake_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(14)
        wake_phrase = self._app.config.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)
        self._wake_intro = _body(
            f'Say <b>"{wake_phrase.title()}"</b> three times at your normal '
            f'speaking volume. Each circle lights up when Samsara hears it.'
        )
        lay.addWidget(self._wake_intro)
        # Row 91 of the audit: say so when the feature being tested is off.
        self._wake_off_note = QLabel(_WAKE_OFF_NOTE)
        self._wake_off_note.setWordWrap(True)
        self._wake_off_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wake_off_note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;")
        self._wake_off_note.setVisible(not bool(self._app.config.get('wake_word_enabled', False)))
        lay.addWidget(self._wake_off_note)
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
        self._oww_result_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._oww_result_lbl)
        lay.addStretch()
        self._oww_tip = QLabel("")
        self._oww_tip.setWordWrap(True)
        self._oww_tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._oww_tip.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;"
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
            f"color:{theme.SUCCESS};font-size:{theme.TYPE_TITLE}px;font-weight:bold;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        self._done_summary = QLabel("")
        self._done_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_summary.setWordWrap(True)
        self._done_summary.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._done_summary)
        lay.addStretch()
        adv_btn = QPushButton("Open Wake Word Debug (advanced)")
        theme.make_ghost(adv_btn)
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
            self._cancel_wake_calibration()
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
                dot.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_BODY}px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;")
            elif i == step:
                dot.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_BODY}px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_MIN}px;font-weight:bold;")
            else:
                dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_BODY}px;font-weight:bold;")
                lbl.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_MIN}px;")

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
            self._oww_result_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            self._oww_tip.setText("")
            self._next_btn.setEnabled(False)
            self._next_btn.setText("Continue  ->")
            self._begin_wake_calibration()

        elif step == self._STEP_DONE:
            self._build_done_summary()

    def _go_next(self):
        if self._switch_in_flight:
            return
        step = self._current_step
        if step == self._STEP_DEVICE:
            # Asynchronous: advances from _on_microphone_switch_done.
            self._begin_microphone_switch()
            return
        elif step == self._STEP_LEVEL:
            self._do_calibrate()
        elif step == self._STEP_DONE:
            self._finish()
            return
        if step < self._STEP_DONE:
            self._go_to(step + 1)

    def _go_back(self):
        if self._switch_in_flight:
            return
        if self._current_step > 0:
            self._go_to(self._current_step - 1)

    def _skip_step(self):
        if self._switch_in_flight:
            return
        if self._current_step < self._STEP_DONE:
            self._go_to(self._current_step + 1)

    def _set_nav_enabled(self, enabled: bool):
        for btn in (self._next_btn, self._back_btn, self._skip_btn):
            btn.setEnabled(enabled)

    # ----------------------------------------------------------------
    # Single persistent audio worker
    #
    # A guide-owned worker reads chunks from the selected device and emits
    # _level_sig on every chunk. The worker is paused around the production
    # runtime switch and quiet wake calibration so those paths own the mic.
    # When _current_step == _STEP_WAKE and _oww_running is True it also
    # runs OWW detection.
    # ----------------------------------------------------------------

    def _ensure_audio_running(self):
        """Start the audio worker if it isn't already running. Never while a
        production microphone switch is in flight: the wizard stream and the
        ACE stream must not cycle on the same device at the same time."""
        if self._switch_in_flight:
            return
        if not self._wizard_active:
            self._wizard_active = True
            self._selected_device = self._device_combo.currentData()
            self._capture_rate = _detect_capture_rate(self._selected_device)
            # Each worker owns its stop Event, so a worker still finishing its
            # last read after _stop_audio() cannot be revived by this restart
            # flipping _wizard_active back to True.
            stop = threading.Event()
            self._audio_stop = stop
            self._audio_thread = thread_registry.spawn(
                "wizard-audio",
                lambda: self._audio_worker(stop),
                daemon=True,
            )

    def _ring_engine(self):
        """The running AudioCaptureEngine whose ring this guide reads, or None.

        Every step except the device preview reads the ring: those steps
        judge the microphone production is using, so they must hear the
        frames the live wake consumer hears (67). The device page previews a
        microphone production may not have switched to yet, so it -- and
        every step when the engine is not running -- uses the guide's own
        stream (_read_own_stream)."""
        if self._current_step == self._STEP_DEVICE:
            return None
        return guide_capture.running_engine(self._app)

    def _audio_worker(self, stop=None):
        """Persistent audio loop. Runs until _wizard_active is False or its
        own stop Event is set, switching between the engine ring and the
        guide's own stream as the step (or the engine) changes.

        THIS thread is the only one that ever stops or closes the stream
        (48). Closing a blocking PortAudio stream from another thread while
        this one is inside stream.read() frees the stream under the read: on
        WASAPI that is an access violation in ntdll that kills the process
        (2026-09-14 20:38 and 21:02, reproduced 15/15). The read returns
        within one block (~100 ms), sees the flag, and closes here.
        """
        def running():
            return self._wizard_active and not (stop is not None and stop.is_set())

        while running():
            engine = self._ring_engine()
            if engine is not None:
                self._read_ring(engine, running)
            else:
                self._read_own_stream(running)

    def _read_ring(self, engine, running):
        """Feed the guide from the engine ring: the 16 kHz frames the live
        wake consumer reads, decoded the way it decodes them."""
        try:
            reader = engine.register_consumer("mic-setup-guide")
        except Exception as exc:
            logger.warning(f"[WIZARD] Could not read the audio engine: {exc}")
            time.sleep(0.3)
            return
        try:
            while running() and self._ring_engine() is engine:
                frame = reader.read_next()
                if frame is EMPTY:
                    time.sleep(0.01)
                    continue
                self._on_capture_frame(pcm_to_float(frame.pcm))
        except RuntimeError as exc:      # reader invalidated: engine restarted
            logger.debug(f"_read_ring: {exc}")
        finally:
            try:
                engine.unregister_consumer(reader)
            except Exception as e:
                logger.debug(f"_read_ring unregister: {e}")

    def _read_own_stream(self, running):
        """Feed the guide from its own stream on the selected device, until
        the device changes or the ring becomes the source. Blocks are
        converted to 16 kHz frames by the engine's own resampler
        (wake_prefilter.NativeRateFrames), so even this path hands the
        detector what the ring would have held."""
        device = self._selected_device
        frames = NativeRateFrames(_detect_capture_rate(device))
        stream = None
        try:
            for channels in dict.fromkeys((_capture_channels(device), 1)):
                try:
                    stream = sd.InputStream(
                        samplerate=frames.native_rate,
                        channels=channels,
                        dtype=np.float32,
                        device=device,
                        blocksize=frames.blocksize,
                    )
                    break
                except Exception:
                    if channels == 1:
                        raise
            stream.start()
            with self._stream_lock:
                self._stream = stream

            while (running() and self._selected_device == device
                    and self._ring_engine() is None):
                try:
                    data, _ = stream.read(frames.blocksize)
                    self._on_capture_frame(frames.convert(np.asarray(data)[:, 0]))
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
                except Exception as e:
                    logger.debug(f"_audio_worker: {e}")

    def _on_capture_frame(self, chunk):
        """One 16 kHz 100 ms frame (audio worker thread): level meter, and
        the wake test while it is running."""
        rms = chunk_rms(chunk)
        self._level_sig.emit(rms)
        if (self._current_step == self._STEP_WAKE
                and self._oww_running
                and self._oww_detector is not None):
            self._feed_wake_detector(chunk, rms)

    def _feed_wake_detector(self, chunk, rms16=None):
        """One 16 kHz 100 ms frame into the wake test (audio worker thread).

        Every frame is recorded in the attempt's _AttemptStats; while armed it
        is scored by OpenWakeWord after oww_prefilter -- the live consumer's
        own pre-filter. After a detection the detector re-arms after
        _OWW_REARM_CHUNKS frames at or below the derived re-arm level, or
        _OWW_REARM_MAX_S, so one utterance is not counted twice."""
        if rms16 is None:
            rms16 = chunk_rms(chunk)
        levels = self._oww_levels or _derive_wake_levels(None)
        detector = self._oww_detector
        score = None
        if self._oww_armed:
            score = float(detector.process_audio(oww_prefilter(chunk, rms16)))
        else:
            now = time.monotonic()
            self._oww_quiet_chunks = self._oww_quiet_chunks + 1 if rms16 <= levels["rearm_rms"] else 0
            if (self._oww_quiet_chunks >= _OWW_REARM_CHUNKS
                    or now - (self._oww_hit_at or now) >= _OWW_REARM_MAX_S):
                detector.reset()
                self._oww_armed = True
                self._oww_quiet_chunks = 0
                logger.debug("[WIZARD] Wake detector re-armed")
        with self._attempt_lock:
            st = self._attempt_stats
            st.frames += 1
            st.rms_sum += rms16
            st.peak_rms = max(st.peak_rms, rms16)
            if score is None:
                st.disarmed += 1
            else:
                st.fed += 1
                st.max_score = score if st.max_score is None else max(st.max_score, score)
        if score is not None and score >= self._oww_threshold:
            self._oww_armed = False
            self._oww_quiet_chunks = 0
            self._oww_hit_at = time.monotonic()
            logger.info(f"[OWW] Wake word detected! score={score:.3f} threshold={self._oww_threshold}")
            self._oww_hit_sig.emit()

    def _stop_audio(self, join: bool = False) -> bool:
        """Signal the worker to exit; the WORKER closes its own stream.

        Never stop()/close() the stream from here: this runs on the Qt thread
        while the worker may be blocked in stream.read(), and closing under a
        read is the access violation that killed the app (see _audio_worker).
        The worker leaves within one ~100 ms block.

        join=True also waits for the worker thread to finish (its stream is
        then fully closed). Returns False only when a join was requested and
        the worker did not exit within _AUDIO_JOIN_TIMEOUT_S.
        """
        self._wizard_active = False
        self._oww_running = False
        stop = self._audio_stop
        if stop is not None:
            stop.set()
        thread = self._audio_thread
        if not join or thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(_AUDIO_JOIN_TIMEOUT_S)
        if thread.is_alive():
            logger.warning("[WIZARD] Audio worker did not exit within %.1fs", _AUDIO_JOIN_TIMEOUT_S)
            return False
        self._audio_thread = None
        return True

    # ----------------------------------------------------------------
    # Level monitoring
    # ----------------------------------------------------------------

    def _populate_devices(self):
        """Populate the device combo from the app's single enumeration path
        (DictationApp.get_available_microphones()) -- WASAPI-filtered,
        de-duped, same source settings_qt.py's mic dropdown uses."""
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._input_devices = self._app.get_available_microphones()
        current_cfg = self._app.config.get('microphone')
        default_idx = 0
        self._device_combo.addItem("System default", userData=None)
        for i, dev in enumerate(self._input_devices):
            self._device_combo.addItem(dev['name'], userData=dev['id'])
            if dev['id'] == current_cfg:
                default_idx = i + 1
        if current_cfg is None:
            default_idx = 0
        self._device_combo.setCurrentIndex(default_idx)
        self._device_combo.blockSignals(False)
        self._selected_device = self._device_combo.currentData()

    def _on_refresh_devices(self):
        """Re-enumerate input devices, preserving the current selection by name.

        This wizard keeps its OWN persistent meter InputStream open for the
        whole device-selection page (see _audio_worker/_ensure_audio_running)
        -- invisible to DictationApp._mic_refresh_blocked(). Refreshing
        safely means tearing that stream down first, THEN checking whether
        a real dictation hold is active elsewhere (the one thing
        refresh_audio_devices() itself still can't work around -- see its
        docstring), THEN re-enumerating, and always restarting our own
        meter afterward regardless of outcome.
        """
        # join: re-enumeration cycles the production stream, and the preview
        # stream is now closed by its worker, so wait until it really is.
        self._stop_audio(join=True)
        try:
            if self._app._mic_refresh_blocked():
                self._device_status.setText("Stop dictation elsewhere to refresh devices.")
                self._device_status.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;")
                return
            try:
                fresh = self._app.refresh_audio_devices()
            except Exception as exc:
                logger.debug(f"_on_refresh_devices: {exc}")
                return

            preserved_name = self._device_combo.currentText()
            self._device_combo.blockSignals(True)
            self._device_combo.clear()
            self._input_devices = fresh
            self._device_combo.addItem("System default", userData=None)
            for dev in fresh:
                self._device_combo.addItem(dev['name'], userData=dev['id'])
            # +1 to account for the "System default" item at index 0.
            idx = pick_index_by_name(fresh, preserved_name)
            self._device_combo.setCurrentIndex(idx + 1 if idx is not None else 0)
            self._device_combo.blockSignals(False)
            self._selected_device = self._device_combo.currentData()
        finally:
            self._ensure_audio_running()

    def _on_device_changed(self, _index: int):
        """When the user picks a new device, update the flag.

        The audio worker's inner loop detects the device mismatch and
        exits; the outer loop reopens a stream for the new device.
        No explicit restart is needed -- the worker handles it.
        """
        self._selected_device = self._device_combo.currentData()
        self._capture_rate = _detect_capture_rate(self._selected_device)

    def _begin_microphone_switch(self):
        """Next on the Device step, without blocking the Qt thread.

        1. disable Next/Back/Skip,
        2. stop the wizard's own audio worker and JOIN it (its stream is
           fully closed before the production engine touches the device),
        3. run switch_microphone on a worker thread,
        4. _on_microphone_switch_done re-enables navigation and only then
           restarts the wizard's audio.
        """
        if self._switch_in_flight:
            return
        mic_id = self._device_combo.currentData()
        mic_name = self._device_combo.currentText()
        self._switch_in_flight = True
        self._set_nav_enabled(False)
        self._device_status.setText("Switching microphone...")
        self._device_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")

        if not self._stop_audio(join=True):
            self._mic_switch_done_sig.emit(
                False, "The microphone preview did not stop. Try again.")
            return

        def _run():
            try:
                ok = self._apply_selected_microphone(mic_id=mic_id, mic_name=mic_name)
            except Exception as exc:   # _apply_selected_microphone already logs
                ok = False
                logger.debug(f"_begin_microphone_switch: {exc}")
            self._mic_switch_done_sig.emit(
                ok, "" if ok else "Could not switch microphones. Check the log and try again.")

        thread_registry.spawn("wizard-mic-switch", _run, daemon=True)

    def _on_microphone_switch_done(self, ok: bool, error: str):
        self._switch_in_flight = False
        self._set_nav_enabled(True)
        if self._closed:
            return
        if ok:
            self._device_status.setText("")
            if self._current_step == self._STEP_DEVICE:
                self._go_to(self._STEP_DEVICE + 1)   # starts audio on the new device
            else:
                self._ensure_audio_running()
            return
        self._device_status.setText(error)
        self._device_status.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;")
        self._ensure_audio_running()

    def _apply_selected_microphone(self, mic_id=..., mic_name=None) -> bool:
        """Apply the selected device through the production runtime switch.

        Runs on the wizard-mic-switch worker (the caller has already stopped
        and joined the wizard's own audio worker); mic_id/mic_name are read
        from the combo on the Qt thread and passed in. Called without them
        (tests, legacy callers) it reads the combo itself.
        """
        if mic_id is ...:
            mic_id = self._device_combo.currentData()
            mic_name = self._device_combo.currentText()
        current_id = self._app.config.get('microphone')

        # Do not write config first: switch_microphone() has a same-ID guard,
        # and would leave ACE on its old stream if config were pre-updated.
        try:
            if mic_id != current_id:
                self._app.switch_microphone(mic_id)
                # Concrete devices are persisted by switch_microphone(). The
                # System-default row has no available_mics entry, so persist
                # its display name explicitly after the runtime switch.
                if mic_id is None:
                    self._app.update_config_and_save({
                        'microphone': None,
                        'microphone_name': mic_name,
                    })
            else:
                self._app.update_config_and_save({
                    'microphone': mic_id,
                    'microphone_name': mic_name,
                })
            return True
        except Exception as exc:
            # Worker thread: no widget access here; the caller signals the UI.
            logger.exception(f"[WIZARD] Could not switch microphone: {exc}")
            return False

    def _on_level(self, rms: float):
        level = min(rms / 0.20, 1.0)
        step = self._current_step

        if step == self._STEP_DEVICE:
            self._device_level_bar.set_level(level)
            if rms > 0.008:
                self._device_status.setText("Signal detected -- mic is working.")
                self._device_status.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;")
            else:
                self._device_status.setText("Say something to test the mic...")
                self._device_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")

        elif step == self._STEP_LEVEL:
            self._level_bar.set_level(level)
            self._level_history.append(rms)
            now = time.monotonic()

            if level < _ZONE_LOW:
                self._level_hint.setText(
                    "Essentially silent -- check the mic is connected and selected above."
                )
                self._level_hint.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;")
                self._green_since = None
            elif level > _ZONE_HIGH:
                self._level_hint.setText(
                    "Very loud -- you may get clipping. Back off slightly or reduce gain."
                )
                self._level_hint.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;")
                self._green_since = None
            else:
                self._level_hint.setText("Level looks good -- keep talking naturally.")
                self._level_hint.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;")
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

    def _cancel_wake_calibration(self):
        """Cancel the current quiet calibration and invalidate late results."""
        self._wake_cal_generation += 1
        cancel = self._wake_cal_cancel
        self._wake_cal_cancel = None
        if cancel is not None:
            cancel.set()

    def _begin_wake_calibration(self):
        """Measure the production wake floor without blocking Qt."""
        self._cancel_wake_calibration()
        self._stop_audio()
        self._wake_cal_generation += 1
        generation = self._wake_cal_generation
        cancel = threading.Event()
        self._wake_cal_cancel = cancel

        self._wake_intro.setText(
            "Please stay quiet for <b>3 seconds</b> while Samsara measures "
            "the background sound around your microphone."
        )
        self._oww_result_lbl.setText("Calibrating background level...")
        self._oww_tip.setText("You can close this guide safely to cancel.")

        def _run():
            floor = None
            error = ""
            report = None
            try:
                floor = self._app.calibrate_wake_mic(
                    seconds=3.0, cancel_event=cancel,
                )
                report = getattr(self._app, "last_wake_calibration", None)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception(f"[WIZARD] Wake calibration failed: {exc}")
            if not cancel.is_set():
                self._wake_cal_done_sig.emit(generation, floor, error, report)

        thread_registry.spawn("wizard-wake-calibration", _run, daemon=True)

    def _on_wake_calibration_result(self, generation: int, floor, error: str, report=None):
        """Qt-thread completion handler for the quiet calibration. The result
        line always says what was measured -- floor, frames and seconds -- so
        an absurd sample is visible to the person running it (48)."""
        if generation != self._wake_cal_generation or self._current_step != self._STEP_WAKE:
            return
        self._wake_cal_cancel = None
        wake_phrase = self._app.config.get(
            'wake_word_config', {}
        ).get('phrase', DEFAULT_WAKE_PHRASE)
        self._wake_intro.setText(
            f'Now say <b>"{wake_phrase.title()}"</b> three times at your normal '
            f'speaking volume. Each circle lights up when Samsara hears it.'
        )
        report = report if isinstance(report, dict) else {}
        self._wake_floor = floor if (not error and floor is not None) else None
        if error:
            self._oww_result_lbl.setText(
                f"Background calibration failed ({error}). Nothing was saved; "
                f"the existing setting stays."
            )
            self._oww_result_lbl.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;")
            logger.warning(f"[WIZARD] Wake calibration failed: {error}")
        elif floor is None:
            message = report.get("message") or (
                "Background calibration was unavailable; continuing with the existing setting."
            )
            self._oww_result_lbl.setText(message)
            if report.get("status") == "insufficient":
                self._oww_result_lbl.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;")
            logger.warning(f"[WIZARD] Wake calibration not saved: {message}")
        else:
            detail = ""
            if report.get("frames"):
                detail = f": floor {float(floor):.4f} from {report['frames']} frames ({report['seconds']:.1f} s)"
            self._oww_result_lbl.setText(
                f"Background level calibrated{detail}. Listening for the wake word..."
            )
            logger.info(f"[WIZARD] Production wake floor calibrated: {float(floor):.5f}")
        self._oww_tip.setText("")
        self._setup_oww_test()

    def _setup_oww_test(self):
        """Initialise OWW detector and start the attempt timer."""
        wake_phrase = self._app.config.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)
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
                _OWW_NO_MODEL_TIP
            )
            self._next_btn.setEnabled(True)
            return

        self._oww_threshold = oww_threshold
        floor = self._wake_floor
        if floor is None:
            floor = self._app.config.get('wake_word_config', {}).get('audio', {}).get('measured_noise_floor')
        self._oww_levels = _derive_wake_levels(floor)
        levels = self._oww_levels
        if levels["source"] == "measured":
            level_note = (f"Speech level {levels['speech_rms']:.4f} from your measured background "
                          f"{levels['floor']:.4f}.")
        else:
            level_note = (f"No background calibration available -- using default levels "
                          f"(speech level {levels['speech_rms']:.3f}).")
        logger.info(f"[WIZARD] Wake test levels ({levels['source']}): floor={levels['floor']} "
                    f"speech_rms={levels['speech_rms']:.5f} rearm_rms={levels['rearm_rms']:.5f} "
                    f"threshold={oww_threshold}")
        self._oww_tip.setText(level_note)
        self._attempt_notes = []

        self._oww_detector.reset()
        self._oww_armed = True
        self._oww_quiet_chunks = 0
        self._oww_hit_at = None
        with self._attempt_lock:
            self._attempt_stats = _AttemptStats()
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
        with self._attempt_lock:
            stats, self._attempt_stats = self._attempt_stats, _AttemptStats()
        levels = self._oww_levels or _derive_wake_levels(None)
        reason, words = _summarize_attempt(stats, levels, self._oww_threshold, hit)
        self._attempt_notes.append(f"Attempt {idx + 1}: {words}")
        self._oww_tip.setText("\n".join(self._attempt_notes))
        logger.info(
            f"[WIZARD] Wake attempt {idx + 1}/{_OWW_ATTEMPTS}: {'detected' if hit else 'missed'} "
            f"reason={reason} frames={stats.frames} fed={stats.fed} disarmed={stats.disarmed} "
            f"rms_mean={(stats.rms_sum / stats.frames if stats.frames else 0.0):.5f} "
            f"rms_peak={stats.peak_rms:.5f} speech_rms={levels['speech_rms']:.5f} ({levels['source']}) "
            f"max_score={'none' if stats.max_score is None else f'{stats.max_score:.3f}'} "
            f"threshold={self._oww_threshold}"
        )
        if hit:
            self._oww_hits += 1
        else:
            if self._oww_detector is not None:
                self._oww_detector.reset()
            self._oww_armed = True
            self._oww_quiet_chunks = 0
        self._oww_attempt_idx += 1
        self._attempt_started = time.monotonic()

        if self._oww_attempt_idx >= _OWW_ATTEMPTS:
            self._finish_oww_test()
        else:
            wake_phrase = self._app.config.get(
                'wake_word_config', {}
            ).get('phrase', DEFAULT_WAKE_PHRASE)
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
                f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
            )
        else:
            self._oww_result_lbl.setText(
                f"Only detected {self._oww_hits}/{_OWW_ATTEMPTS} times."
            )
            self._oww_result_lbl.setStyleSheet(
                f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
            )
            tips = (
                "Try speaking more directly toward the mic and a little slower."
                if self._oww_hits == 0 else
                "Speak at a steady, natural pace -- don't rush the word."
            )
            if self._oww_hits < _OWW_PASS_THRESHOLD:
                tips += (
                    "  If it keeps missing, lower 'Wake-word threshold' "
                    "in Settings -> Modes (try 0.10)."
                )
            self._oww_tip.setText("\n".join(self._attempt_notes + [tips]))

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
        # Device selection is applied when leaving the device page.
        self.close()

    def _open_debug(self):
        try:
            self._app.open_wake_word_debug()
        except Exception as e:
            logger.debug(f"_open_debug: {e}")

    # ----------------------------------------------------------------
    # Cleanup
    # ----------------------------------------------------------------

    def showEvent(self, e):
        self._closed = False
        super().showEvent(e)

    def closeEvent(self, e):
        # A switch finishing after close must not restart the preview stream.
        self._closed = True
        self._cancel_wake_calibration()
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
            f"QProgressBar{{background:{theme.BG1};border:1px solid {theme.BORDER};"
            f"border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{theme.SUCCESS};border-radius:4px;}}"
        )
        lay.addWidget(self._bar)

    def set_level(self, level: float):
        pct = int(min(max(level, 0.0), 1.0) * 100)
        self._bar.setValue(pct)
        if level < _ZONE_LOW:
            chunk_color = theme.ERROR
        elif level > _ZONE_HIGH:
            chunk_color = theme.WARNING
        else:
            chunk_color = theme.SUCCESS
        self._bar.setStyleSheet(
            f"QProgressBar{{background:{theme.BG1};border:1px solid {theme.BORDER};"
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
        self._apply_style(str(number), theme.BG2, theme.TEXT_SECONDARY, theme.BORDER)
        lay.addWidget(self._circle)
        self._lbl = QLabel("waiting")
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._lbl)

    def _apply_style(self, text, bg, fg, border):
        self._circle.setText(text)
        self._circle.setStyleSheet(
            f"border-radius:{self._SIZE // 2}px;"
            f"background:{bg};"
            f"color:{fg};"
            f"font-size:{theme.TYPE_TITLE}px;font-weight:bold;"
            f"border:2px solid {border};"
        )

    def reset(self):
        self._apply_style(str(self._number), theme.BG2, theme.TEXT_SECONDARY, theme.BORDER)
        self._lbl.setText("waiting")
        self._lbl.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_MIN}px;")

    def set_result(self, heard: bool):
        if heard:
            self._apply_style("OK", theme.SUCCESS, theme.BG0, theme.SUCCESS)
            self._lbl.setText("heard")
            self._lbl.setStyleSheet(
                f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
            )
        else:
            self._apply_style("--", theme.BG2, theme.ERROR, theme.ERROR)
            self._lbl.setText("missed")
            self._lbl.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;")


def _button_min_width(btn: QPushButton, texts: list[str], h_padding: int = 48) -> int:
    """Minimum width that fits the widest of `texts` in btn's actual font,
    plus theme button padding (24px each side). Used instead of
    setFixedWidth() so buttons never clip text the theme's font metrics
    don't fit in an old, pre-theme pixel count."""
    fm = btn.fontMetrics()
    widest = max((fm.horizontalAdvance(t) for t in texts), default=0)
    return widest + h_padding


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
    return lbl
