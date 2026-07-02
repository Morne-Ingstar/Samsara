"""
PySide6 first-run setup wizard for Samsara.

Runs synchronously on the main thread (same contract as the Tkinter version):
    wizard = FirstRunWizardQt(config_path)
    result = wizard.run()   # blocks until wizard finishes or is closed
    # result is a dict on success, None on hard cancel
"""

import json
import logging
import math
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QButtonGroup, QRadioButton,
    QFrame, QScrollArea,
)

from samsara.ui import qt_runtime

_logger = logging.getLogger("Samsara")

# If the wizard window never appears within this many seconds of posting its
# creation, give up and continue boot with defaults rather than hang forever.
# Never applies once the window has actually shown -- a user genuinely
# taking their time in the wizard is never timed out.
_WIZARD_SHOW_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

_SS = """
QMainWindow, QWidget {
    background-color: #0A0A0B;
    color: #E8E8EA;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
}
QLabel { color: #E8E8EA; }
QComboBox {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 8px 12px;
    color: #E8E8EA;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #16161A;
    color: #E8E8EA;
    selection-background-color: rgba(94,234,212,0.2);
    border: 1px solid rgba(255,255,255,0.14);
}
QRadioButton { color: #E8E8EA; spacing: 8px; }
QRadioButton::indicator {
    width: 16px; height: 16px;
    border-radius: 8px;
    border: 2px solid rgba(255,255,255,0.25);
    background: #16161A;
}
QRadioButton::indicator:checked {
    background: #5EEAD4;
    border-color: #5EEAD4;
}
QPushButton {
    background-color: #5EEAD4;
    color: #0A0A0B;
    border: none;
    border-radius: 6px;
    padding: 10px 24px;
    font-weight: 600;
    font-size: 14px;
}
QPushButton:hover { background-color: #4DD8C2; color: #0A0A0B; }
QPushButton:pressed { background-color: #3DC8B0; color: #0A0A0B; }
QPushButton:disabled { background-color: #2E6F66; color: #0A0A0B; }
QPushButton[class="primary"] {
    background-color: #5EEAD4;
    color: #0A0A0B;
    border: none;
    border-radius: 6px;
    padding: 10px 24px;
    font-weight: 600;
    font-size: 14px;
}
QPushButton[class="primary"]:hover { background-color: #4DD8C2; color: #0A0A0B; }
QPushButton[class="primary"]:pressed { background-color: #3DC8B0; color: #0A0A0B; }
QPushButton[class="primary"]:disabled { background-color: #2E6F66; color: #0A0A0B; }
QPushButton[class="secondary"] {
    background-color: transparent;
    color: #8A8A92;
    border: 1px solid rgba(255,255,255,0.14);
}
QPushButton[class="secondary"]:hover {
    background-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
}
QPushButton[class="secondary"]:pressed {
    background-color: rgba(255,255,255,0.08);
    color: #E8E8EA;
}
QPushButton[class="secondary"]:disabled {
    background-color: transparent;
    color: #3A3A40;
    border-color: rgba(255,255,255,0.06);
}
"""

# ---------------------------------------------------------------------------
# Hotkey capture button (self-contained, no import from settings_qt)
# ---------------------------------------------------------------------------

_MOD_KEYS = {
    Qt.Key.Key_Control: 'ctrl',
    Qt.Key.Key_Shift:   'shift',
    Qt.Key.Key_Alt:     'alt',
    Qt.Key.Key_Meta:    'win',
}
_SPECIAL_KEYS = {
    Qt.Key.Key_Escape:   'escape',
    Qt.Key.Key_Tab:      'tab',
    Qt.Key.Key_Return:   'enter',
    Qt.Key.Key_CapsLock: 'capslock',
    Qt.Key.Key_Space:    'space',
    **{getattr(Qt.Key, f'Key_F{n}'): f'f{n}' for n in range(1, 13)},
}
_MOD_ORDER = {'ctrl': 0, 'shift': 1, 'alt': 2, 'win': 3}


def _key_name(key: int) -> str | None:
    if key in _MOD_KEYS:
        return _MOD_KEYS[key]
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    if 0x20 <= key <= 0x7E:
        return chr(key).lower()
    return None


def _combo(held: set) -> str:
    return '+'.join(sorted(held, key=lambda k: (_MOD_ORDER.get(k, 99), k)))


class _HotkeyBtn(QPushButton):
    _IDLE = (
        "QPushButton{background:#16161A;border:1px solid rgba(255,255,255,0.14);"
        "border-radius:6px;color:#E8E8EA;font-size:12px;"
        "font-family:'Consolas','Courier New',monospace;padding:6px 14px;}"
        "QPushButton:hover{background:#1E1E24;}"
    )
    _ACTIVE = (
        "QPushButton{background:rgba(94,234,212,0.08);border:1px solid #5EEAD4;"
        "border-radius:6px;color:#5EEAD4;font-size:12px;"
        "font-family:'Consolas','Courier New',monospace;padding:6px 14px;}"
    )

    def __init__(self, combo: str):
        super().__init__(combo or "—")
        self._combo = combo
        self._capturing = False
        self._held: set[str] = set()
        self.setMinimumWidth(160)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(self._IDLE)
        self.clicked.connect(self._start)

    @property
    def combo(self) -> str:
        return self._combo

    def _start(self):
        self._capturing = True
        self._held = set()
        self.setText("Press keys…")
        self.setStyleSheet(self._ACTIVE)
        self.setFocus()

    def _finish(self):
        self._capturing = False
        if self._held:
            self._combo = _combo(self._held)
        self.setText(self._combo or "—")
        self.setStyleSheet(self._IDLE)

    def keyPressEvent(self, e):
        if not self._capturing:
            super().keyPressEvent(e)
            return
        name = _key_name(e.key())
        if name:
            self._held.add(name)
            self.setText(_combo(self._held) or "Press keys…")
        e.accept()

    def keyReleaseEvent(self, e):
        if not self._capturing:
            super().keyReleaseEvent(e)
            return
        self._finish()
        e.accept()

    def focusOutEvent(self, e):
        if self._capturing:
            self._finish()
        super().focusOutEvent(e)


# ---------------------------------------------------------------------------
# Live microphone level meter
# ---------------------------------------------------------------------------

class _MicLevelMeter(QWidget):
    """Horizontal bar meter: colour zones, peak hold, smoothed ballistics.

    Gain mapping: sqrt(rms * 20) — a 0.01 RMS headset maps to ~45 % fill,
    clearly visible without distorting louder inputs.
    Attack 0.70, decay 0.12 per 40 ms tick; peak holds ~1.1 s then falls.
    """

    _BG    = QColor(14, 14, 18)
    _ZONES = [
        (0.50, QColor(94,  234, 212)),  # 0-50 %  teal
        (0.75, QColor(232, 144,  32)),  # 50-75 % amber
        (1.01, QColor(255,  68,  68)),  # 75-100% red
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self._level     = 0.0
        self._peak      = 0.0
        self._peak_hold = 0

    def set_rms(self, rms_raw: float) -> None:
        target = math.sqrt(min(rms_raw * 20.0, 1.0))
        if target > self._level:
            self._level += (target - self._level) * 0.70   # fast attack
        else:
            self._level += (target - self._level) * 0.12   # slow decay
        if self._level > self._peak:
            self._peak = self._level
            self._peak_hold = 28                            # ~1.1 s at 40 ms
        else:
            self._peak_hold -= 1
            if self._peak_hold <= 0:
                self._peak = max(0.0, self._peak - 0.018)
        self.update()

    def reset(self) -> None:
        self._level = self._peak = 0.0
        self._peak_hold = 0
        self.update()

    def paintEvent(self, event) -> None:
        w = self.width()
        h = self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(0, 0, w, h, self._BG)

        lv   = max(0.0, min(1.0, self._level))
        prev = 0
        for thresh, color in self._ZONES:
            seg = int(min(thresh, lv) * w)
            if seg > prev:
                p.fillRect(prev, 2, seg - prev, h - 4, color)
            prev = seg
            if lv <= thresh:
                break

        pk = max(0.0, min(1.0, self._peak))
        if pk > 0.02:
            px = int(pk * w)
            p.fillRect(max(0, px - 1), 1, 2, h - 2, QColor(255, 255, 255, 200))

        p.setPen(QColor(255, 255, 255, 25))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


# ---------------------------------------------------------------------------
# Step metadata
# ---------------------------------------------------------------------------

_STEPS = [
    ("Welcome",    "Welcome to Samsara"),
    ("Use Case",   "How Will You Use Samsara?"),
    ("Microphone", "Select Your Microphone"),
    ("Model",      "Choose Recognition Quality"),
    ("Shortcuts",  "Shortcuts & Wake Word"),
    ("Complete",   "Setup Complete!"),
]

# Config deltas applied when the user selects a use case.
# Nested dicts are merged into the existing config sub-dict.
_USE_CASE_CONFIGS = {
    "chronic_pain": {
        "wake_word_enabled": True,
        "tts": {"enabled": True},
        "mode": "hold",
        "audio_feedback": True,
    },
    "privacy": {
        "cloud_llm": {"enabled": False},
        "wake_word_enabled": True,
    },
    "power_user": {
        "wake_word_enabled": True,
        "streaming_mode": True,
        "command_mode_enabled": True,
    },
    "just_dictation": {
        "wake_word_enabled": False,
        "mode": "hold",
    },
}

_USE_CASE_TIPS = {
    "chronic_pain": (
        "Try saying: 'Jarvis, pain level 6' to log your pain level, or "
        "'Jarvis, took ibuprofen 400mg' to track medication. "
        "Say 'health summary' for a spoken overview of your day."
    ),
    "privacy": (
        "All your data stays on this machine. Voice recognition runs locally "
        "via Whisper — nothing is sent to the cloud."
    ),
    "power_user": (
        "CapsLock streaming is enabled. Hold CapsLock for live transcription. "
        "Check out the Command Reference for all available voice commands."
    ),
    "just_dictation": (
        "Hold Ctrl+Shift, speak, and release. "
        "Text appears wherever your cursor is."
    ),
}

_DEFAULTS = {
    "hotkey":              "ctrl+shift",
    "continuous_hotkey":   "ctrl+alt+d",
    "wake_word_hotkey":    "ctrl+alt+w",
    "command_hotkey":      "ctrl+alt+c",
    "mode":                "hold",
    "model_size":          "base",
    "language":            "en",
    "auto_paste":          True,
    "add_trailing_space":  True,
    "auto_capitalize":     True,
    "format_numbers":      True,
    "device":              "auto",
    "microphone":          None,
    "silence_threshold":   2.0,
    "min_speech_duration": 0.3,
    "command_mode_enabled": False,
    "wake_word":           "jarvis",
    "wake_word_timeout":   5.0,
    "show_all_audio_devices": False,
    "audio_feedback":      True,
    "first_run_complete":  True,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class FirstRunWizardQt:
    """Drop-in replacement for FirstRunWizard with a Qt UI.

    run() blocks the calling thread until the wizard is complete or
    dismissed, without owning the event loop.
    """

    def __init__(self, config_path, app=None):
        self.config_path = config_path
        self._app = app

    def run(self) -> dict | None:
        _logger.debug(
            "[WIZ-DIAG] FirstRunWizardQt.run() entry, thread ident=%s",
            threading.get_ident(),
        )
        qt_runtime.ensure_started()
        _logger.debug("[WIZ-DIAG] qt_runtime.ensure_started() returned")

        result_holder = [None]
        done = threading.Event()
        shown_holder = [False]
        samsara_app = self._app
        # Every other Qt window in this codebase keeps its window alive via a
        # persistent `self._window` attribute (see history_qt.py, settings_qt.py,
        # main_window_qt.py, etc.). This wizard was the one exception: `win` was
        # a bare local inside _create(), so once _create() returned, nothing
        # held a Python reference to it -- CPython's refcounting GC'd the
        # QMainWindow almost immediately after show(), silently destroying it
        # before closeEvent()/_finished could ever fire. Because shown_holder
        # was already True by then, the watchdog below disarms its own timeout
        # ("once shown, wait indefinitely"), so done.wait() below blocked
        # forever with no window on screen and no recovery -- a real,
        # unrecoverable hang. win_holder pins a live reference for run()'s
        # entire blocking wait, exactly mirroring the self._window pattern.
        win_holder = [None]

        def _create():
            _logger.debug(
                "[WIZ-DIAG] _create entered on thread ident=%s",
                threading.get_ident(),
            )
            # A broken wizard must never zombie the app. Anything that
            # throws here (frozen-build asset paths, the mic-meter's
            # transient InputStream, screen-geometry calls, etc.) is caught,
            # logged with a full traceback, and treated as "wizard failed" --
            # boot continues with defaults exactly like a hard cancel.
            try:
                win = _WizardWindow(self.config_path, samsara_app)
                win_holder[0] = win  # keep alive -- see win_holder comment above
                _logger.debug("[WIZ-DIAG] _create: _WizardWindow constructed")
                app = QApplication.instance()
                if app:
                    screen = app.primaryScreen().availableGeometry()
                    win.move(
                        screen.center().x() - win.width() // 2,
                        screen.center().y() - win.height() // 2,
                    )
                win._finished.connect(lambda r: (
                    result_holder.__setitem__(0, r),
                    done.set(),
                ))
                win.show()
                shown_holder[0] = True
                _logger.debug("[WIZ-DIAG] _create: win.show() completed, shown_holder=True")
            except Exception:
                _logger.exception(
                    "[WIZARD] Creation failed — continuing with default config"
                )
                result_holder[0] = None
                done.set()

        _logger.debug(
            "[WIZ-DIAG] about to call qt_runtime.post(_create), thread ident=%s",
            threading.get_ident(),
        )
        qt_runtime.post(_create)
        _logger.debug("[WIZ-DIAG] qt_runtime.post(_create) returned, about to wait on Event")

        # Watchdog: if the window never even appeared within the timeout,
        # give up and proceed with defaults. Once shown_holder is True the
        # user is looking at it -- wait indefinitely, no cap.
        if not done.wait(timeout=_WIZARD_SHOW_TIMEOUT_S) and not shown_holder[0]:
            _logger.warning(
                "[WIZARD] Timed out before showing — continuing with defaults"
            )
            return None
        done.wait()
        _logger.debug("[WIZ-DIAG] run(): done, returning result")
        return result_holder[0]


# ---------------------------------------------------------------------------
# Wizard window
# ---------------------------------------------------------------------------

class _WizardWindow(QMainWindow):
    _mic_result = Signal(str, str)   # (message, hex-color)
    _finished   = Signal(object)     # emits result just before close

    def __init__(self, config_path, samsara_app=None):
        super().__init__()
        self.config_path = config_path
        self._samsara_app = samsara_app
        self.result = None
        self._step = 0
        self._config = dict(_DEFAULTS)
        self._mics: list[dict] = []
        self._mic_result.connect(self._on_mic_result)

        self.setWindowTitle("Samsara Setup")
        _scr = QApplication.primaryScreen()
        if _scr:
            _av = _scr.availableGeometry()
            _w = max(720, min(1100, int(_av.width()  * 0.46)))
            _h = max(680, min(980,  int(_av.height() * 0.72)))
        else:
            _w, _h = 860, 760
        self.setMinimumSize(720, 680)
        self.resize(_w, _h)
        self.setStyleSheet(_SS)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header bar ---------------------------------------------------
        header = QWidget()
        header.setStyleSheet("background:#111114;")
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)

        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet("color:#8A8A92;font-size:12px;")
        hl.addWidget(self._step_lbl)

        hl.addStretch()

        # Step dots
        self._dots: list[QLabel] = []
        for _ in _STEPS:
            dot = QLabel("●")
            dot.setStyleSheet("font-size:10px;")
            self._dots.append(dot)
            hl.addWidget(dot)

        root.addWidget(header)

        sep_top = QFrame()
        sep_top.setFrameShape(QFrame.Shape.HLine)
        sep_top.setStyleSheet("background:rgba(255,255,255,0.06);max-height:1px;")
        root.addWidget(sep_top)

        # ---- Page title ---------------------------------------------------
        title_bar = QWidget()
        title_bar.setContentsMargins(0, 0, 0, 0)
        title_bar.setFixedHeight(60)
        tbl = QVBoxLayout(title_bar)
        tbl.setContentsMargins(28, 14, 28, 8)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            "color:#E8E8EA;font-size:20px;font-weight:bold;"
        )
        tbl.addWidget(self._title_lbl)
        root.addWidget(title_bar)

        # ---- Page stack ---------------------------------------------------
        self._stack = QWidget()
        self._stack_layout = QVBoxLayout(self._stack)
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack, stretch=1)

        # Build all pages up front (they're cheap)
        self._mic_combo: QComboBox | None = None
        self._mic_status: QLabel | None = None
        self._mic_page: QWidget | None = None
        self._model_group: QButtonGroup | None = None
        self._hotkey_btns: dict[str, _HotkeyBtn] = {}
        self._summary_labels: list[QLabel] = []
        self._use_case_group: QButtonGroup | None = None
        self._tip_lbl: QLabel | None = None
        self._no_hints_cb: QCheckBox | None = None
        self._meter: _MicLevelMeter | None = None
        self._meter_timer: QTimer | None = None
        self._meter_ace_reader = None
        self._meter_stream = None
        self._meter_rms_holder: list = [0.0]
        self._last_meter_rms: float = 0.0
        self._meter_passed: bool = False

        self._pages = [
            self._build_welcome(),
            self._build_use_case(),
            self._build_microphone(),
            self._build_model(),
            self._build_shortcuts(),
            self._build_complete(),
        ]
        self._current_page: QWidget | None = None

        sep_bot = QFrame()
        sep_bot.setFrameShape(QFrame.Shape.HLine)
        sep_bot.setStyleSheet("background:rgba(255,255,255,0.06);max-height:1px;")
        root.addWidget(sep_bot)

        # ---- Nav buttons --------------------------------------------------
        nav = QWidget()
        nav.setFixedHeight(64)
        nav.setStyleSheet("background:#111114;")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(28, 12, 28, 12)

        self._back_btn = QPushButton("Back")
        self._back_btn.setProperty("class", "secondary")
        self._back_btn.style().unpolish(self._back_btn)
        self._back_btn.style().polish(self._back_btn)
        self._back_btn.setFixedWidth(90)
        self._back_btn.clicked.connect(self._go_back)
        nl.addWidget(self._back_btn)

        nl.addStretch()

        self._next_btn = QPushButton("Next")
        self._next_btn.setProperty("class", "primary")
        self._next_btn.style().unpolish(self._next_btn)
        self._next_btn.style().polish(self._next_btn)
        self._next_btn.setFixedWidth(150)
        self._next_btn.clicked.connect(self._go_next)
        nl.addWidget(self._next_btn)

        root.addWidget(nav)

        # Enumerate microphones in background so page 2 is ready
        threading.Thread(target=self._load_mics, daemon=True).start()

        self._show_step()

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _padded(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 16, 28, 16)
        lay.setSpacing(12)
        return w, lay

    def _build_welcome(self) -> QWidget:
        w, lay = self._padded()
        sub = QLabel("Voice dictation for Windows — free, local, fast.")
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(8)

        steps_frame = QFrame()
        steps_frame.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(255,255,255,0.06);}"
        )
        sf_layout = QVBoxLayout(steps_frame)
        sf_layout.setContentsMargins(20, 16, 20, 16)
        sf_layout.setSpacing(10)

        for icon, text in [
            ("1.", "Choose how you'll use Samsara"),
            ("2.", "Select your microphone"),
            ("3.", "Choose speech recognition quality"),
            ("4.", "Set your keyboard shortcuts"),
        ]:
            row = QHBoxLayout()
            icon_lbl = QLabel(icon)
            icon_lbl.setFixedWidth(28)
            icon_lbl.setStyleSheet("font-size:18px;")
            row.addWidget(icon_lbl)
            text_lbl = QLabel(text)
            text_lbl.setStyleSheet("color:#E8E8EA;font-size:13px;")
            row.addWidget(text_lbl)
            row.addStretch()
            sf_layout.addLayout(row)

        lay.addWidget(steps_frame)
        lay.addSpacing(8)

        note = QLabel("Setup takes about one minute. You can change everything later in Settings.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(note)
        lay.addStretch()
        return w

    def _build_use_case(self) -> QWidget:
        w, lay = self._padded()
        sub = QLabel("Pick the option that fits best. You can change everything later in Settings.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(4)

        self._use_case_group = QButtonGroup(w)
        _CASES = [
            ("chronic_pain",   "I have chronic pain or limited mobility",
             "Set up for hands-free use with health tracking, voice reminders, and spoken feedback."),
            ("privacy",        "I value privacy and want local-only voice control",
             "Everything stays on your machine. No cloud, no accounts, no data leaves your computer."),
            ("power_user",     "I'm a power user / developer",
             "Scriptable voice macros, command packs, and deep customization."),
            ("just_dictation", "Just dictation",
             "Simple speech-to-text. Press a key, speak, release."),
        ]
        for i, (value, title, desc) in enumerate(_CASES):
            card = QFrame()
            card.setStyleSheet(
                "QFrame{background:#111114;border-radius:8px;"
                "border:1px solid rgba(255,255,255,0.06);}"
            )
            cl = QHBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(10)

            rb = QRadioButton()
            rb.setProperty("_value", value)
            if value == "just_dictation":
                rb.setChecked(True)
            self._use_case_group.addButton(rb, i)
            cl.addWidget(rb, alignment=Qt.AlignmentFlag.AlignTop)

            text_col = QVBoxLayout()
            text_col.setSpacing(3)
            name_lbl = QLabel(title)
            name_lbl.setStyleSheet("font-weight:600;font-size:13px;color:#E8E8EA;")
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color:#8A8A92;font-size:12px;")
            text_col.addWidget(name_lbl)
            text_col.addWidget(desc_lbl)
            cl.addLayout(text_col, stretch=1)

            lay.addWidget(card)

        lay.addStretch()
        return w

    def _build_microphone(self) -> QWidget:
        w, lay = self._padded()
        self._mic_page = w  # saved for _on_mic_result lookup
        sub = QLabel("Choose the microphone Samsara will listen on.")
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(4)

        self._mic_combo = QComboBox()
        self._mic_combo.addItem("Scanning for microphones…")
        self._mic_combo.setEnabled(False)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_device_changed)
        lay.addWidget(self._mic_combo)

        lay.addSpacing(8)
        meter_lbl = QLabel("Input Level")
        meter_lbl.setStyleSheet("color:#8A8A92;font-size:11px;")
        lay.addWidget(meter_lbl)

        self._meter = _MicLevelMeter()
        lay.addWidget(self._meter)

        self._mic_status = QLabel("Speak to test your microphone")
        self._mic_status.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(self._mic_status)

        lay.addStretch()
        return w

    def _build_model(self) -> QWidget:
        w, lay = self._padded()
        sub = QLabel(
            "Larger models are more accurate but use more memory and are slower to start."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(4)

        self._model_group = QButtonGroup(w)
        _MODELS = [
            ("tiny",  "Fastest",              "~75 MB — lowest accuracy, instant startup"),
            ("base",  "Balanced (Recommended)", "~150 MB — good accuracy, fast startup"),
            ("small", "Best Quality",          "~500 MB — highest accuracy, slower startup"),
        ]
        for i, (value, title, desc) in enumerate(_MODELS):
            card = QFrame()
            card.setStyleSheet(
                "QFrame{background:#111114;border-radius:8px;"
                "border:1px solid rgba(255,255,255,0.06);}"
            )
            cl = QHBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)

            rb = QRadioButton()
            rb.setProperty("_value", value)
            if value == "base":
                rb.setChecked(True)
            self._model_group.addButton(rb, i)
            cl.addWidget(rb)

            text_col = QVBoxLayout()
            name_lbl = QLabel(title)
            name_lbl.setStyleSheet("font-weight:600;font-size:14px;color:#E8E8EA;")
            desc_lbl = QLabel(desc)
            desc_lbl.setStyleSheet("color:#8A8A92;font-size:12px;")
            text_col.addWidget(name_lbl)
            text_col.addWidget(desc_lbl)
            cl.addLayout(text_col, stretch=1)

            lay.addWidget(card)

        note = QLabel("The model downloads on first use (once). You can change it later in Settings.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(note)
        lay.addStretch()
        return w

    def _build_shortcuts(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")

        inner, lay = self._padded()
        sub = QLabel("Click a button and press your desired key combination.")
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(4)

        _HOTKEYS = [
            ("hotkey",           "ctrl+shift",  "Hold to Record",
             "Hold to record, release to transcribe"),
            ("continuous_hotkey","ctrl+alt+d",  "Continuous Mode",
             "Toggle always-on dictation"),
            ("wake_word_hotkey", "ctrl+alt+w",  "Wake Word Mode",
             "Toggle wake word activation"),
            ("command_hotkey",   "ctrl+alt+c",  "Command Only",
             "Hold to speak a command (no text output)"),
        ]
        for key, default, label, desc in _HOTKEYS:
            row = QHBoxLayout()
            row.setSpacing(12)

            left = QVBoxLayout()
            left.setSpacing(2)
            name_lbl = QLabel(label)
            name_lbl.setStyleSheet("font-weight:600;font-size:13px;color:#E8E8EA;")
            desc_lbl = QLabel(desc)
            desc_lbl.setStyleSheet("color:#8A8A92;font-size:11px;")
            left.addWidget(name_lbl)
            left.addWidget(desc_lbl)
            row.addLayout(left, stretch=1)

            btn = _HotkeyBtn(default)
            self._hotkey_btns[key] = btn
            row.addWidget(btn, alignment=Qt.AlignmentFlag.AlignVCenter)
            lay.addLayout(row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:rgba(255,255,255,0.06);max-height:1px;")
        lay.addWidget(sep)

        # Wake word
        ww_row = QHBoxLayout()
        ww_left = QVBoxLayout()
        ww_left.setSpacing(2)
        ww_left.addWidget(QLabel("Wake Word Phrase"))
        ww_desc = QLabel('Say "Jarvis" or "Hey Jarvis" to activate voice commands')
        ww_desc.setStyleSheet("color:#8A8A92;font-size:11px;")
        ww_left.addWidget(ww_desc)
        ww_row.addLayout(ww_left, stretch=1)

        ww_lbl = QLabel("jarvis")
        ww_lbl.setStyleSheet(
            "color:#5EEAD4;font-size:13px;font-weight:bold;"
            "font-family:'Consolas','Courier New',monospace;"
        )
        ww_row.addWidget(ww_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(ww_row)

        coming = QLabel("More wake word options coming soon.")
        coming.setStyleSheet("color:#8A8A92;font-size:11px;")
        lay.addWidget(coming)
        lay.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _build_complete(self) -> QWidget:
        w, lay = self._padded()

        done_lbl = QLabel("You're all set — Samsara is ready.")
        done_lbl.setStyleSheet("color:#5EEAD4;font-size:14px;")
        lay.addWidget(done_lbl)
        lay.addSpacing(4)

        card = QFrame()
        card.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(255,255,255,0.06);}"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(6)
        # Placeholder labels — populated in _show_step when we arrive here
        for _ in range(6):
            lbl = QLabel("")
            lbl.setStyleSheet("color:#E8E8EA;font-size:13px;")
            self._summary_labels.append(lbl)
            cl.addWidget(lbl)
        lay.addWidget(card)

        # Use-case-specific tip — populated in _fill_summary()
        tip_frame = QFrame()
        tip_frame.setStyleSheet(
            "QFrame{background:rgba(94,234,212,0.06);border-radius:8px;"
            "border:1px solid rgba(94,234,212,0.18);}"
        )
        tf_lay = QVBoxLayout(tip_frame)
        tf_lay.setContentsMargins(16, 12, 16, 12)
        self._tip_lbl = QLabel("")
        self._tip_lbl.setWordWrap(True)
        self._tip_lbl.setStyleSheet("color:#AAFAF0;font-size:12px;")
        tf_lay.addWidget(self._tip_lbl)
        lay.addWidget(tip_frame)

        note = QLabel(
            "The model downloads on first use. "
            "Look for the Samsara tray icon to get started."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(note)
        lay.addSpacing(12)

        self._no_hints_cb = QCheckBox("Don't show me hints (you can re-enable this in Settings)")
        self._no_hints_cb.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(self._no_hints_cb)

        lay.addStretch()
        return w

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_step(self):
        # Tear down any running meter; restart if entering the mic step
        self._stop_meter()

        # Swap page content
        page = self._pages[self._step]
        if self._current_page is not None:
            self._stack_layout.removeWidget(self._current_page)
            self._current_page.hide()
        self._stack_layout.addWidget(page)
        page.show()
        self._current_page = page

        # Update header
        name, title = _STEPS[self._step]
        self._step_lbl.setText(f"Step {self._step + 1} of {len(_STEPS)}")
        self._title_lbl.setText(title)

        # Dots
        for i, dot in enumerate(self._dots):
            dot.setStyleSheet(
                "color:#5EEAD4;font-size:10px;" if i == self._step
                else "color:#444;font-size:10px;"
            )

        # Button states
        self._back_btn.setEnabled(self._step > 0)
        is_last = self._step == len(_STEPS) - 1
        self._next_btn.setText("Start Samsara" if is_last else "Next")

        # Populate complete page summary when we reach it
        if is_last:
            self._fill_summary()

        # Start level meter on microphone step
        if _STEPS[self._step][0] == "Microphone":
            self._start_meter()

    def _go_next(self):
        self._collect_step()
        if _STEPS[self._step][0] == "Use Case":
            self._apply_use_case_defaults()
        if self._step == len(_STEPS) - 1:
            self._finish()
            return
        self._step += 1
        self._show_step()

    def _go_back(self):
        if self._step > 0:
            self._step -= 1
            self._show_step()

    def _collect_step(self):
        """Read UI values for the current step into self._config."""
        step_name = _STEPS[self._step][0]
        if step_name == "Use Case" and self._use_case_group:
            checked = self._use_case_group.checkedButton()
            self._config['_use_case'] = (
                checked.property("_value") if checked else "just_dictation"
            )
        elif step_name == "Microphone" and self._mic_combo:
            mic_name = self._mic_combo.currentText()
            for m in self._mics:
                if m['name'] == mic_name:
                    self._config['microphone'] = m['id']
                    break
        elif step_name == "Model" and self._model_group:
            checked = self._model_group.checkedButton()
            if checked:
                self._config['model_size'] = checked.property("_value")
        elif step_name == "Shortcuts":
            for key, btn in self._hotkey_btns.items():
                self._config[key] = btn.combo

    def _fill_summary(self):
        model_names = {
            'tiny':  'Fastest (tiny)',
            'base':  'Balanced (base)',
            'small': 'Best Quality (small)',
        }
        mic_name = self._mic_combo.currentText() if self._mic_combo else "Default"
        model    = model_names.get(self._config.get('model_size', 'base'),
                                   self._config.get('model_size', 'base'))
        lines = [
            f"Microphone:       {mic_name or 'Default'}",
            f"Model:            {model}",
            f"Record:           {self._config.get('hotkey', 'ctrl+shift')}  (hold)",
            f"Continuous:       {self._config.get('continuous_hotkey', 'ctrl+alt+d')}",
            f"Wake Word Key:    {self._config.get('wake_word_hotkey', 'ctrl+alt+w')}",
            f"Wake Phrase:      jarvis",
        ]
        for lbl, text in zip(self._summary_labels, lines):
            lbl.setText(text)

        # Populate the use-case tip
        if self._tip_lbl is not None:
            use_case = self._config.get('_use_case', 'just_dictation')
            tip = _USE_CASE_TIPS.get(use_case, "")
            self._tip_lbl.setText(tip)
            self._tip_lbl.parentWidget().setVisible(bool(tip))

    def _finish(self):
        self._collect_step()
        self._config['first_run_complete'] = True
        if self._no_hints_cb is not None and self._no_hints_cb.isChecked():
            self._config['hints_enabled'] = False
        self.result = self._config
        self.close()

    def _apply_use_case_defaults(self):
        """Apply config defaults for the selected use case."""
        use_case = self._config.get('_use_case', 'just_dictation')
        updates = _USE_CASE_CONFIGS.get(use_case, {})
        for key, val in updates.items():
            if isinstance(val, dict):
                existing = self._config.setdefault(key, {})
                if isinstance(existing, dict):
                    existing.update(val)
                else:
                    self._config[key] = dict(val)
            else:
                self._config[key] = val

    def closeEvent(self, event):
        self._stop_meter()
        # Ensure result is always set before signalling done.
        if self.result is None:
            self._config['first_run_complete'] = True
            self.result = self._config
        self._finished.emit(self.result)
        event.accept()

    # ------------------------------------------------------------------
    # Microphone helpers
    # ------------------------------------------------------------------

    def _load_mics(self):
        """Enumerate microphones in a background thread, update combo when done."""
        try:
            if self._samsara_app is not None:
                mics = self._samsara_app.get_available_microphones()
                self._mics = [{'id': m['id'], 'name': m['name']} for m in mics]
            else:
                import sounddevice as sd
                devices = sd.query_devices()
                hostapis = sd.query_hostapis()
                preferred_api_idx = None
                for idx, api in enumerate(hostapis):
                    if 'WASAPI' in api['name']:
                        preferred_api_idx = idx
                        break
                mics: list[dict] = []
                seen: set[str] = set()
                for i, dev in enumerate(devices):
                    if dev['max_input_channels'] <= 0:
                        continue
                    if preferred_api_idx is not None and dev['hostapi'] != preferred_api_idx:
                        continue
                    name: str = dev['name']
                    dedup_key = name.strip().lower()
                    if not dedup_key or dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    mics.append({'id': i, 'name': name})
                self._mics = mics
        except Exception:
            self._mics = []
        # Update combo on Qt thread via signal
        self._mic_result.emit("_load_done_", "")

    # ------------------------------------------------------------------
    # Meter helpers
    # ------------------------------------------------------------------

    def _get_current_mic_id(self):
        if not self._mics or not self._mic_combo:
            return None
        name = self._mic_combo.currentText()
        for m in self._mics:
            if m['name'] == name:
                return m['id']
        return None

    def _on_mic_device_changed(self, _index: int):
        if _STEPS[self._step][0] == "Microphone":
            self._start_meter()

    def _start_meter(self):
        """Start (or restart) the level meter for the currently selected mic."""
        self._stop_meter()  # idempotent — tears down any existing resources
        if self._meter is None:
            return

        mic_id = self._get_current_mic_id()

        ace = None
        if self._samsara_app is not None:
            ace = getattr(self._samsara_app, '_ace_engine', None)
        use_ace = ace is not None and getattr(ace, '_running', False)

        if use_ace:
            self._meter_ace_reader = ace.register_consumer("wizard-meter")
            print("[WIZARD] Meter: ACE ring consumer")
        else:
            # ACE engine not yet started (typical at first-run — wizard runs
            # before _start_ace_engine() in __init__).  Open a minimal
            # transient InputStream solely for level metering; fully closed
            # in _stop_meter() when the mic step is exited.
            print(f"[WIZARD] Meter: transient sounddevice stream (device={mic_id!r})")
            self._meter_stream = self._open_meter_stream(mic_id)

        timer = QTimer(self)
        timer.setInterval(40)
        timer.timeout.connect(self._meter_tick)
        timer.start()
        self._meter_timer = timer

    def _stop_meter(self):
        """Stop the meter timer and release all audio resources."""
        if self._meter_timer is not None:
            self._meter_timer.stop()
            self._meter_timer.deleteLater()
            self._meter_timer = None

        if self._meter_ace_reader is not None:
            ace = None
            if self._samsara_app is not None:
                ace = getattr(self._samsara_app, '_ace_engine', None)
            if ace is not None:
                try:
                    ace.unregister_consumer(self._meter_ace_reader)
                except Exception:
                    pass
            self._meter_ace_reader = None

        if self._meter_stream is not None:
            try:
                self._meter_stream.stop()
                self._meter_stream.close()
            except Exception:
                pass
            self._meter_stream = None

        self._meter_rms_holder = [0.0]
        self._last_meter_rms = 0.0
        self._meter_passed = False
        if self._meter is not None:
            self._meter.reset()
        if self._mic_status is not None:
            self._mic_status.setText("Speak to test your microphone")
            self._mic_status.setStyleSheet("color:#8A8A92;font-size:12px;")

    def _open_meter_stream(self, device_id):
        """Open a minimal transient InputStream for meter-only use."""
        try:
            import numpy as np
            import sounddevice as sd
            rms_holder = self._meter_rms_holder

            def _cb(indata, frames, time_info, status):
                block = indata[:, 0]
                rms_holder[0] = float(np.sqrt(np.mean(block * block)))

            stream = sd.InputStream(
                device=device_id,
                channels=1,
                dtype='float32',
                blocksize=512,
                callback=_cb,
            )
            stream.start()
            return stream
        except Exception as exc:
            print(f"[WIZARD] Meter stream error: {exc}")
            return None

    def _meter_tick(self):
        """Qt-thread timer callback: read audio, update meter widget."""
        if self._meter is None:
            return
        import numpy as np

        rms = 0.0
        if self._meter_ace_reader is not None:
            try:
                from samsara.audio_engine.ring import EMPTY
                ace = getattr(self._samsara_app, '_ace_engine', None)
                if ace is not None:
                    chunks = []
                    while True:
                        frame = self._meter_ace_reader.read_next()
                        if frame is EMPTY:
                            break
                        chunks.append(frame.pcm.astype(np.float32) / 32767.0)
                    if chunks:
                        block = np.concatenate(chunks)
                        rms = float(np.sqrt(np.mean(block * block)))
                        self._last_meter_rms = rms
                    else:
                        # No new frame this tick; decay the stored value
                        self._last_meter_rms *= 0.85
                        rms = self._last_meter_rms
            except Exception:
                pass
        elif self._meter_stream is not None:
            rms = self._meter_rms_holder[0]

        self._meter.set_rms(rms)
        self._update_mic_pass(rms)

    def _update_mic_pass(self, rms: float):
        """Show 'Microphone active' once sustained audio is detected."""
        if self._mic_status is None or self._meter_passed:
            return
        mapped = math.sqrt(min(rms * 20.0, 1.0))
        if mapped > 0.15:   # ~0.003 raw RMS — any real audio above noise floor
            self._meter_passed = True
            self._mic_status.setText("Microphone active")
            self._mic_status.setStyleSheet("color:#5EEAD4;font-size:12px;")

    def _on_mic_result(self, msg: str, color: str):
        if msg == "_load_done_":
            self._populate_mic_combo()
            return
        if self._mic_status:
            self._mic_status.setText(msg)
            self._mic_status.setStyleSheet(f"color:{color};font-size:12px;")

    def _populate_mic_combo(self):
        if self._mic_combo is None:
            return
        self._mic_combo.blockSignals(True)
        self._mic_combo.clear()
        if self._mics:
            self._mic_combo.addItems([m['name'] for m in self._mics])
            self._mic_combo.setEnabled(True)
        else:
            self._mic_combo.addItem("No microphones detected")
            self._mic_combo.setEnabled(False)
        self._mic_combo.blockSignals(False)
        # Restart meter now that device list is ready
        if _STEPS[self._step][0] == "Microphone":
            self._start_meter()
