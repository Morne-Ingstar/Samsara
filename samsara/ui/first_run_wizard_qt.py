"""
PySide6 first-run setup wizard for Samsara.

Runs synchronously on the main thread (same contract as the Tkinter version):
    wizard = FirstRunWizardQt(config_path)
    result = wizard.run()   # blocks until wizard finishes or is closed
    # result is a dict on success, None on hard cancel
"""

import copy
import json
import logging
import math
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QRadioButton, QCheckBox, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QComboBox, QPushButton, QButtonGroup,
    QFrame, QProgressBar, QScrollArea,
)

from samsara import config_defaults, listening_display
from samsara.constants import DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_PHRASE_OPTIONS
from samsara import session_modes
from samsara.runtime import thread_registry
from samsara.ui import qt_runtime, theme
from samsara.ui.first_run_pages_qt import Decisions, initial_config
from samsara.onboarding.first_run_recommend import COPY
from samsara.ui.widgets.step_indicator import StepIndicator
from samsara.ui.listening_display_page import setup_page, STEP_LABEL_GAP
from samsara.ui.tutorial_display_qt import setup_placement_page, TITLE as PLACEMENT_TITLE
from samsara.onboarding.display_placement import seed_fresh
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer
from samsara.audio_devices import force_rescan, list_microphones, pick_index_by_name

from samsara.log import get_logger

logger = get_logger(__name__)

_logger = logging.getLogger("Samsara")

# If the wizard window never appears within this many seconds of posting its
# creation, give up and continue boot with defaults rather than hang forever.
# Never applies once the window has actually shown -- a user genuinely
# taking their time in the wizard is never timed out.
_WIZARD_SHOW_TIMEOUT_S = 120.0
MIC_ACCESS_DENIED_MESSAGE = (
    "Microphone access was denied — enable Windows microphone privacy and press Refresh"
)  # OWNER COPY — REVIEW

# ---------------------------------------------------------------------------
# Shared Settings/setup key capture
# ---------------------------------------------------------------------------
from samsara.ui.settings.shared_qt import _HotkeyButton


class _HotkeyBtn(_HotkeyButton):
    pass


# ---------------------------------------------------------------------------
# Live microphone level meter
# ---------------------------------------------------------------------------

class _MicLevelMeter(QProgressBar):
    """Horizontal bar meter: colour zones, peak hold, smoothed ballistics.

    Gain mapping: sqrt(rms * 20) — a 0.01 RMS headset maps to ~45 % fill,
    clearly visible without distorting louder inputs.
    Attack 0.70, decay 0.12 per 40 ms tick; peak holds ~1.1 s then falls.
    """

    # Read per paint, not stored: a QColor built at class-definition time
    # freezes the palette that was live at import (queue 129).
    @staticmethod
    def _bg() -> QColor:
        return theme.qcolor(theme.BG1)

    @staticmethod
    def _zones():
        """Level bands: comfortable (to 50%), loud (to 75%), clipping.

        The BAND is carried by colour alone -- the bar's height is constant
        and only the fill length changes, so "loud" and "clipping" differ by
        hue only. The level itself is carried by length, so a user who cannot
        tell amber from red still sees how loud they are; what they lose is
        the boundary. Queue 129 reports this rather than fixing it: the fix
        is a tick mark at each threshold, which is a layout change, not a
        token change."""
        return [
            (0.50, theme.qcolor(theme.ACCENT)),
            (0.75, theme.qcolor(theme.WARNING)),
            (1.01, theme.qcolor(theme.ERROR)),
        ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.setValue(round(self._level * 100))
        self.update()

    def reset(self) -> None:
        self._level = self._peak = 0.0
        self._peak_hold = 0
        self.setValue(0)
        self.update()

    def paintEvent(self, event) -> None:
        w = self.width()
        h = self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(0, 0, w, h, self._bg())

        lv   = max(0.0, min(1.0, self._level))
        prev = 0
        for thresh, color in self._zones():
            seg = int(min(thresh, lv) * w)
            if seg > prev:
                p.fillRect(prev, 2, seg - prev, h - 4, color)
            prev = seg
            if lv <= thresh:
                break

        pk = max(0.0, min(1.0, self._peak))
        if pk > 0.02:
            px = int(pk * w)
            p.fillRect(max(0, px - 1), 1, 2, h - 2,
                       theme.qcolor(theme.wash(0.78)))

        p.setPen(theme.qcolor(theme.wash(0.10)))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class _WizardChoiceIndicator(QRadioButton):
    """Painter-based circular indicator with smooth antialiased checked state."""

    _SIZE = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setStyleSheet("margin:0;")

    def sizeHint(self) -> QSize:
        return QSize(self._SIZE, self._SIZE)

    def setChecked(self, checked: bool) -> None:
        if checked or self.group() is not None:
            super().setChecked(checked)
            return
        # Preserve the original standalone setter without changing the native
        # radio semantics of the grouped choices in the wizard.
        exclusive = self.autoExclusive()
        self.setAutoExclusive(False)
        try:
            super().setChecked(checked)
        finally:
            self.setAutoExclusive(exclusive)

    def hitButton(self, pos) -> bool:
        # Keep the original QAbstractButton hit area despite native radio semantics.
        return self.rect().contains(pos)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        center = bounds.center()

        if self.isEnabled():
            base = QColor(theme.BG2)
            stroke = QColor(theme.BORDER)
            mark = QColor(theme.BG0)
            if self.isChecked():
                stroke = QColor(theme.ACCENT)
                base = QColor(theme.ACCENT)
                mark = QColor(theme.BG0)
            elif self.isDown() or self.underMouse():
                stroke = QColor(theme.ACCENT)
        else:
            base = QColor(theme.BG2)
            base.setAlpha(120)
            stroke = QColor(theme.BORDER)
            stroke.setAlpha(90)

        p.setBrush(base)
        p.setPen(QPen(stroke, 1.5))
        p.drawEllipse(bounds.adjusted(2.0, 2.0, -2.0, -2.0))

        if self.isChecked():
            inner = QRectF(
                center.x() - bounds.width() * 0.16,
                center.y() - bounds.height() * 0.16,
                bounds.width() * 0.32,
                bounds.height() * 0.32,
            )
            p.setBrush(QColor(theme.BG0))
            p.setPen(QPen(QColor(theme.BG0), 1.0))
            p.drawEllipse(inner)

            check_pen = QPen(mark, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(check_pen)
            p.drawLine(
                center.x() - 3.0,
                center.y(),
                center.x() - 0.6,
                center.y() + 3.5,
            )
            p.drawLine(
                center.x() - 0.6,
                center.y() + 3.5,
                center.x() + 3.5,
                center.y() - 3.5,
            )

        if self.hasFocus():
            focus = bounds.adjusted(-2.0, -2.0, 2.0, 2.0)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(theme.ACCENT), 1.2))
            p.drawEllipse(focus)


_STEPS = [
    ("Welcome",    "Welcome to Samsara"),
    ("Microphone", "Select Your Microphone"),
    ("Components", "Optional Components"),
    # PLACEHOLDER COPY (373)
    ("Recognition", COPY["recognition.title"]),
    ("Talking", COPY["talking.title"]),
    ("Music", COPY["music.title"]),
    ("Voice", COPY["voice.title"]),
    (listening_display.LABEL, listening_display.QUESTION),
    ("Position", PLACEMENT_TITLE),  # PLACEHOLDER COPY (490)
    ("Complete",   "Setup Complete!"),
]

def _merge_wake_word_config(config: dict, phrase=None, wake_command_timeout=None) -> dict:
    """Read-merge-write config["wake_word_config"] (and its "audio" sub-dict):
    keys already there survive, only the given values change."""
    wwc = dict(config.get("wake_word_config") or {})
    audio = dict(wwc.get("audio") or {})
    if phrase is not None:
        wwc["phrase"] = phrase
    if wake_command_timeout is not None:
        audio["wake_command_timeout"] = float(wake_command_timeout)
    if audio:
        wwc["audio"] = audio
    config["wake_word_config"] = wwc
    return config


def _finalize_config(config: dict) -> dict:
    """The dict the wizard hands back (dictation.py writes it as config.json).
    Folds any legacy flat wake keys into wake_word_config and removes them,
    so the live key wake_word_config.audio.wake_command_timeout is what gets
    written -- never the dead top-level wake_word_timeout."""
    phrase = config.pop("wake_word", None)
    timeout = config.pop("wake_word_timeout", None)
    return _merge_wake_word_config(config, phrase=phrase, wake_command_timeout=timeout)


def _shortest(phrases) -> str:
    return min(phrases, key=lambda p: (len(p), p)) if phrases else ""


def _hands_free_tip(config: dict) -> str:
    """The chronic-pain use-case tip, built from the words the session
    actually honours (session_modes constants + the wizard config) --
    guidance audit 2026-09-13 row 336 (bare "command" was never a switch
    word) and row 336-338 (no stop / sleep word was mentioned)."""
    cm = config.get("command_mode") or {}
    try:
        from samsara.ui.quick_reference_qt import _pretty_button  # noqa: PLC0415
        button = _pretty_button(cm.get("button", "rctrl")).replace(" (default)", "")
    except Exception:
        button = "Right Ctrl"
    minutes = int(cm.get("inactivity_timeout_s", 900)) // 60
    switches = session_modes._WHOLE_UTTERANCE_SWITCHES
    cmd_word = _shortest([p for p, m in switches.items() if m is session_modes.SessionMode.COMMAND])
    dictate_word = _shortest([p for p, m in switches.items() if m is session_modes.SessionMode.DICTATE])
    ava_word = (session_modes.resolve_ava_invocations(config) or [""])[0]
    stop_word = session_modes.SESSION_STOP_PHRASES[0]
    sleep_word = session_modes.SESSION_SLEEP_PHRASES[0]
    exit_word = session_modes.GLOBAL_SESSION_EXIT_PHRASES[0]
    return (
        f"Tap {button} once to start hands-free dictation. It closes after {minutes} minutes without activity. "
        f"Say '{cmd_word}', '{dictate_word}' or '{ava_word}' to switch lanes. In Dictate, "
        f"say '{session_modes.DICTATE_COMMIT_PHRASE}' by itself to paste your thought and keep dictating. "
        f"Say '{stop_word}' to stop what is running (your draft is kept); say '{sleep_word}' "
        f"or '{exit_word}' to leave hands-free mode."
    )


def _builtin_hotkeys_text(config: dict) -> str:
    """Keys that exist with no wizard control: read from the wizard config
    when set, else the same fallback table the Quick Reference uses."""
    from samsara.ui.quick_reference_qt import _HOTKEY_FALLBACKS, _pretty_key_combo  # noqa: PLC0415

    def key(name):
        return _pretty_key_combo(config.get(name, _HOTKEY_FALLBACKS.get(name, "")))

    return ("Also built in (change them in Settings -> Modes): "
            f"undo last dictation {key('undo_hotkey')} - cancel a recording {key('cancel_hotkey')} - "
            f"Ava key {config.get('ava_mode_key', 'right_alt').replace('_', ' ').title()} - "
            f"voice memo {key('memo_hotkey')} - correction capture {key('hotkeys.capture_correction')}. "
            "The full list is in Quick Reference (tray menu).")


def _wake_options_text(phrase: str = DEFAULT_WAKE_PHRASE) -> str:
    """Row 879: the alternatives exist today -- name them instead of
    promising them."""
    alternates = [p for p in DEFAULT_WAKE_PHRASE_OPTIONS if p != phrase]
    return ("Also answers to: " + ", ".join(alternates)
            + ". Change the phrase later in Settings -> Modes.")

_DEFAULTS = {
    "tts": {"feedback_level": config_defaults.cfg_get({}, "tts.feedback_level"), "enabled": True},
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
    "command_mode": {"command_matching_enabled": False},
    # Nested, live keys (guidance audit 2026-09-13 rows 371/372): the flat
    # legacy "wake_word" / "wake_word_timeout" keys are migrated away or
    # silently dropped by dictation.load_config(). _finalize_config()
    # read-merge-writes this dict; nothing replaces wake_word_config wholesale.
    "wake_word_config": {
        "phrase": DEFAULT_WAKE_PHRASE,
        "audio": {"wake_command_timeout": 5.0},
    },
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
        self._config, self._existing_config = initial_config(_DEFAULTS, config_path)
        seed_fresh(self._config, fresh=not Path(config_path).exists())
        self._mics: list[dict] = []
        self._mic_result.connect(self._on_mic_result)
        self._mic_announcer = LiveSurfaceAnnouncer(self)

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
        self.setStyleSheet(theme.build_stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header bar ---------------------------------------------------
        header = QWidget()
        header.setStyleSheet(f"background:{theme.BG0};")
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)

        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        hl.addWidget(self._step_lbl)

        hl.addStretch()

        root.addWidget(header)
        self._step_indicator = StepIndicator([name for name, _ in _STEPS])
        root.addWidget(self._step_indicator)
        root.addSpacing(STEP_LABEL_GAP)
        sep_top = QFrame()
        sep_top.setFrameShape(QFrame.Shape.HLine)
        sep_top.setStyleSheet(f"background:{theme.BORDER_FAINT};max-height:1px;")
        root.addWidget(sep_top)

        # ---- Page title ---------------------------------------------------
        title_bar = QWidget()
        title_bar.setContentsMargins(0, 0, 0, 0)
        title_bar.setMinimumHeight(60)
        tbl = QVBoxLayout(title_bar)
        tbl.setContentsMargins(28, 14, 28, 8)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_TITLE}px;font-weight:bold;"
        )
        tbl.addWidget(self._title_lbl)
        root.addWidget(title_bar)

        # ---- Page stack ---------------------------------------------------
        self._stack = QWidget()
        self._stack_layout = QVBoxLayout(self._stack)
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack, stretch=1)

        self._mic_combo: QComboBox | None = None
        self._mic_status: QLabel | None = None
        self._mic_page: QWidget | None = None
        self._model_group: QButtonGroup | None = None
        self._hotkey_btns: dict[str, _HotkeyBtn] = {}
        self._summary_labels: list[QLabel] = []
        self._no_hints_cb: QCheckBox | None = None
        self._meter: _MicLevelMeter | None = None
        self._meter_timer: QTimer | None = None
        self._mic_scan_error: str | None = None
        self._meter_ace_reader = None
        self._meter_stream = None
        self._meter_rms_holder: list = [0.0]
        self._last_meter_rms: float = 0.0
        self._meter_passed: bool = False
        self._mic_capture_ready: bool = False

        self._components_page = None
        self._decisions = Decisions(self)
        self._pages = [
            self._build_welcome(),
            self._build_microphone(),
            self._build_components(),
            self._build_model(),
            self._decisions.talking(),
            self._decisions.music(),
            self._decisions.voice(),
            setup_page(self),
            setup_placement_page(self),
            self._build_complete(),
        ]
        self._current_page: QWidget | None = None

        sep_bot = QFrame()
        sep_bot.setFrameShape(QFrame.Shape.HLine)
        sep_bot.setStyleSheet(f"background:{theme.BORDER_FAINT};max-height:1px;")
        root.addWidget(sep_bot)

        # ---- Nav buttons --------------------------------------------------
        nav = QWidget()
        nav.setFixedHeight(theme.HIT_TARGET_MIN + 24)
        theme.style_footer(nav)
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(28, 12, 28, 12)

        self._back_btn = QPushButton("Back")
        theme.make_secondary(self._back_btn)
        self._back_btn.setFixedWidth(90)
        self._back_btn.clicked.connect(self._go_back)
        nl.addWidget(self._back_btn)

        nl.addStretch()

        self._next_btn = QPushButton("Next")
        theme.make_primary(self._next_btn)
        self._next_btn.setFixedWidth(150)
        self._next_btn.clicked.connect(self._go_next)
        nl.addWidget(self._next_btn)

        root.addWidget(nav)

        # Enumerate microphones in background so page 2 is ready
        thread_registry.spawn("first_run_wizard_qt._load_mics", self._load_mics, daemon=True)

        self._decisions.scroll_existing_pages()
        theme.apply_design_scale(self)
        self._decisions.prepare_navigation()
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
        sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(sub)
        lay.addSpacing(8)

        steps_frame = QFrame()
        theme.style_card(steps_frame)
        sf_layout = QVBoxLayout(steps_frame)
        sf_layout.setContentsMargins(20, 16, 20, 16)
        sf_layout.setSpacing(10)

        for icon, text in [
            # PLACEHOLDER COPY (373)
            ("1.", "Select your microphone"),
            ("2.", "Add optional components, now or later"),
            ("3.", COPY["welcome.recognition"]),
            ("4.", COPY["welcome.talking"]),
            ("5.", "Choose a listening display"),
        ]:
            row = QHBoxLayout()
            icon_lbl = QLabel(icon)
            icon_lbl.setMinimumWidth(28)
            icon_lbl.setStyleSheet(f"font-size:{theme.TYPE_TITLE}px;")
            row.addWidget(icon_lbl)
            text_lbl = QLabel(text)
            text_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
            row.addWidget(text_lbl)
            row.addStretch()
            sf_layout.addLayout(row)

        lay.addWidget(steps_frame)
        lay.addSpacing(8)

        note = QLabel("Setup is quick. You can change everything later in Settings.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(note)
        lay.addStretch()
        return w

    def _build_components(self) -> QWidget:
        """34: the one page after the microphone step that offers whatever
        the installer did not fetch. Built by samsara.ui.first_run_qt; the
        wizard only hosts it and wires "Later" to Next."""
        from samsara.ui.first_run_qt import ComponentsPage

        self._components_page = ComponentsPage(self._stack, on_later=self._go_next)
        return self._components_page.widget

    def _build_microphone(self) -> QWidget:
        w, lay = self._padded()
        self._mic_page = w  # saved for _on_mic_result lookup
        sub = QLabel("Choose the microphone Samsara will listen on.")
        sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(sub)
        lay.addSpacing(4)

        mic_row = QHBoxLayout()
        self._mic_combo = QComboBox()
        self._mic_combo.addItem("Scanning for microphones…")
        self._mic_combo.setEnabled(False)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_device_changed)
        mic_row.addWidget(self._mic_combo, 1)
        mic_refresh_btn = QPushButton("Refresh")
        mic_refresh_btn.setFixedWidth(80)
        mic_refresh_btn.clicked.connect(self._on_refresh_mics_clicked)
        mic_row.addWidget(mic_refresh_btn)
        lay.addLayout(mic_row)

        lay.addSpacing(8)
        meter_lbl = QLabel("Input Level")
        meter_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(meter_lbl)

        self._meter = _MicLevelMeter()
        self._meter.setAccessibleName(meter_lbl.text())
        lay.addWidget(self._meter)

        self._mic_status = QLabel("Speak to test your microphone")
        self._meter.setAccessibleDescription(self._mic_status.text())
        self._mic_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(self._mic_status)

        lay.addStretch()
        return w

    def _build_model(self) -> QWidget:
        return self._decisions.recognition()

    def _build_complete(self) -> QWidget:
        return self._decisions.complete()

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

        self._step_indicator.set_progress(self._step)

        # Button states
        self._back_btn.setEnabled(self._step > 0)
        is_last = self._step == len(_STEPS) - 1
        self._next_btn.setText("Start Samsara" if is_last else "Next")
        self._decisions.prepare_step(name)

        # Populate complete page summary when we reach it
        if is_last:
            self._fill_summary()

        # Start level meter on microphone step
        if _STEPS[self._step][0] == "Microphone":
            self._next_btn.setEnabled(False)
            self._start_meter()

    def _go_next(self):
        self._collect_step()
        if _STEPS[self._step][0] == "Microphone" and not self._mic_capture_ready:
            if self._mic_status:
                self._mic_status.setText("Select a working microphone before continuing.")
                self._mic_status.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_BODY}px;")
            return
        if self._step == len(_STEPS) - 1:
            self._finish()
            return
        self._step += 1
        self._show_step()

    def _go_back(self):
        self._collect_step()
        if self._step > 0:
            self._step -= 1
            self._show_step()

    def _collect_step(self):
        step_name = _STEPS[self._step][0]
        if step_name in ("Recognition", "Talking", "Music", "Voice"):
            self._decisions.collect(step_name)
        elif step_name == listening_display.LABEL:
            self._listening_display_page.save_to(self._config)
        elif step_name == "Microphone" and self._mic_combo:
            mic_name = self._mic_combo.currentText()
            for m in self._mics:
                if m['name'] == mic_name:
                    self._config['microphone'] = m['id']
                    break

    def _fill_summary(self):
        self._decisions.summary()

    def _finish(self):
        if self._config.get('microphone') is None:
            return
        self._collect_step()
        self._listening_display_page.save_to(self._config)
        self._decisions.finish()
        self._config['first_run_complete'] = True
        if self._no_hints_cb is not None and self._no_hints_cb.isChecked():
            self._config['hints_enabled'] = False
        self.result = _finalize_config(self._config)
        self.close()

    def closeEvent(self, event):
        self._decisions.shutdown()
        self._stop_meter()
        # A window close is a cancel, not a successful first run. The caller
        # keeps its existing config (or generates defaults) and can retry the
        # wizard on the next launch.
        self._finished.emit(self.result)
        event.accept()

    # ------------------------------------------------------------------
    # Microphone helpers
    # ------------------------------------------------------------------

    def _enumerate_mics(self) -> list:
        """Shared enumeration logic for both the initial load and a refresh.

        Plain re-query only -- does NOT force PortAudio to re-scan (that
        requires DictationApp.refresh_audio_devices(), only available when
        self._samsara_app is set), so a device connected after this process
        started may not appear via this path alone. See _refresh_mics().
        """
        if self._app_ready():
            mics = self._samsara_app.get_available_microphones()
            return [{'id': m['id'], 'name': m['name']} for m in mics]
        return list_microphones()

    def _load_mics(self):
        """Enumerate microphones in a background thread, update combo when done."""
        try:
            self._mics = self._enumerate_mics()
            self._mic_scan_error = None
        except Exception as exc:
            logger.warning(
                "Microphone enumeration failed while loading first-run wizard",
                exc_info=True,
            )
            self._mics = []
            self._mic_scan_error = type(exc).__name__
            self._mic_result.emit("_load_error_", "")
            return
        self._mic_result.emit("_load_done_", "")

    def _app_ready(self) -> bool:
        """True only when the DictationApp handle is fully constructed.

        At FIRST RUN the wizard is launched from DictationApp.__init__
        BEFORE self.config / self.recording exist, so calling the app's
        enumeration/refresh methods raises AttributeError on the
        half-built instance (the original root cause of the shipped
        "No microphones detected" -- 2026-07-24 Stranger Test). A handle
        without .config must be treated exactly like no handle at all.
        """
        return (
            self._samsara_app is not None
            and getattr(self._samsara_app, 'config', None) is not None
        )

    def _on_refresh_mics_clicked(self):
        """Stop our own meter (it may hold a stream _mic_refresh_blocked()
        can't see), then re-enumerate.

        2026-07-17: when a live DictationApp is available, the refresh now
        goes through DictationApp.refresh_audio_devices() DIRECTLY here on
        the Qt thread (this is a .clicked signal handler, so it already IS
        the Qt thread) instead of being handed to a background thread the
        way it used to be. That used to be harmless because the old guard
        always blocked before doing any real work; now that
        refresh_audio_devices() actually stops/restarts the ACE engine
        around a PortAudio re-init, it needs the Qt-thread-only contract
        its docstring documents (see there) honored by every caller, not
        just internally correct. The no-app fallback path
        (_enumerate_mics(), a plain sd.query_devices() read with no engine
        involved) still runs on a background thread -- unaffected by any
        of this."""
        self._stop_meter()
        if self._mic_status:
            self._mic_status.setText("Refreshing devices…")
            self._mic_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        if self._app_ready():
            self._refresh_mics_via_app()
        else:
            thread_registry.spawn(
                "first_run_wizard_qt._refresh_mics", self._refresh_mics, daemon=True,
            )

    def _refresh_mics_via_app(self):
        """Qt-thread counterpart of _on_refresh_mics_clicked() when a live
        DictationApp is available -- see that method's docstring for why
        this must run here rather than on a background thread.

        Uses DictationApp.refresh_audio_devices() -- the one path that
        forces PortAudio to re-scan (via sd._terminate()/_initialize()),
        so a just-connected device actually shows up. Falls back to a
        plain re-query on failure.
        """
        if self._samsara_app._mic_refresh_blocked():
            self._mic_result.emit("__refresh_skipped__", "")
            return
        try:
            mics = self._samsara_app.refresh_audio_devices()
            self._mics = [{'id': m['id'], 'name': m['name']} for m in mics]
            self._mic_scan_error = None
        except Exception as exc:
            logger.warning(
                "Microphone refresh failed in first-run wizard (app path)",
                exc_info=True,
            )
            try:
                self._mics = self._enumerate_mics()
            except Exception:
                self._mics = []
                self._mic_scan_error = type(exc).__name__
            else:
                self._mic_scan_error = type(exc).__name__
        self._mic_result.emit("_refresh_done_", "")

    def _refresh_mics(self):
        """Background-thread fallback for _on_refresh_mics_clicked() when
        no live DictationApp instance is available -- plain re-query only
        (_enumerate_mics() never touches the ACE engine or PortAudio
        re-init), safe off the Qt thread."""
        try:
            force_rescan()
            self._mics = list_microphones()
            self._mic_scan_error = None
        except Exception as exc:
            logger.warning(
                "Microphone refresh failed in first-run wizard (no app path)",
                exc_info=True,
            )
            self._mics = []
            self._mic_scan_error = type(exc).__name__
        self._mic_result.emit("_refresh_done_", "")

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
            self._mic_capture_ready = False
            self._next_btn.setEnabled(False)
            return

        mic_id = self._get_current_mic_id()
        if mic_id is None:
            self._mic_capture_ready = False
            if self._mic_status:
                self._on_mic_result("Select a working microphone before continuing.", theme.WARNING)
            self._next_btn.setEnabled(False)
            return

        from samsara.audio_engine.guide_capture import RingLevelMeter, running_engine
        ace = running_engine(self._samsara_app)

        if ace is not None:
            self._meter_ace_reader = RingLevelMeter(ace, "wizard-meter")
            self._mic_capture_ready = True
            print("[WIZARD] Meter: ACE ring consumer")
        else:
            # ACE engine not yet started (typical at first-run — wizard runs
            # before _start_ace_engine() in __init__).  Open a minimal
            # transient InputStream solely for level metering; fully closed
            # in _stop_meter() when the mic step is exited.
            print(f"[WIZARD] Meter: transient sounddevice stream (device={mic_id!r})")
            self._meter_stream = self._open_meter_stream(mic_id)
            self._mic_capture_ready = self._meter_stream is not None
            if self._meter_stream is None:
                self._next_btn.setEnabled(False)
                return
        self._next_btn.setEnabled(self._mic_capture_ready)

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
            try:
                self._meter_ace_reader.close()
            except Exception as e:
                logger.debug(f"_stop_meter: {e}")
            self._meter_ace_reader = None

        if self._meter_stream is not None:
            try:
                self._meter_stream.stop()
                self._meter_stream.close()
            except Exception as e:
                logger.debug(f"_stop_meter: {e}")
            self._meter_stream = None

        self._meter_rms_holder = [0.0]
        self._last_meter_rms = 0.0
        self._meter_passed = False
        self._mic_capture_ready = False
        self._mic_announcer.reset()
        if self._meter is not None:
            self._meter.reset()
        if self._mic_status is not None:
            self._mic_status.setText("Speak to test your microphone")
            self._mic_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
            if self._meter is not None:
                self._meter.setAccessibleDescription(self._mic_status.text())

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
            if self._mic_status is not None:
                message = (
                    MIC_ACCESS_DENIED_MESSAGE
                    if isinstance(exc, PermissionError) or "denied" in str(exc).lower()
                    else "Microphone could not be opened — press Refresh"
                )
                self._on_mic_result(message, theme.WARNING)
            return None

    def _meter_tick(self):
        """Qt-thread timer callback: read audio, update meter widget."""
        if self._meter is None:
            return

        rms = 0.0
        if self._meter_ace_reader is not None:
            try:
                # samsara.audio_engine.guide_capture.RingLevelMeter: drains
                # the ring, decays when no new frame arrived this tick.
                rms = self._meter_ace_reader.read_rms()
                self._last_meter_rms = rms
            except Exception as e:
                logger.debug(f"_meter_tick: {e}")
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
            self._mic_status.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_BODY}px;")
            self._meter.setAccessibleDescription(self._mic_status.text())
            self._mic_announcer.announce_transition(
                ("microphone-active", self._mic_combo.currentData()), self._mic_status.text()
            )

    def _on_mic_result(self, msg: str, color: str):
        if msg == "_load_done_":
            self._populate_mic_combo()
            return
        if msg == "_load_error_":
            self._populate_mic_combo()
            return
        if msg == "_refresh_done_":
            self._populate_mic_combo(preserve_selection=True)
            return
        if msg == "__refresh_skipped__":
            if self._mic_status:
                self._mic_status.setText("Stop dictation elsewhere to refresh devices.")
                self._mic_status.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_BODY}px;")
            # We stopped our own meter before attempting the refresh --
            # restart it since the refresh didn't happen (nothing else will).
            if _STEPS[self._step][0] == "Microphone":
                self._start_meter()
            return
        if self._mic_status:
            self._mic_status.setText(msg)
            self._mic_status.setStyleSheet(f"color:{color};font-size:{theme.TYPE_BODY}px;")
            self._meter.setAccessibleDescription(self._mic_status.text())
            self._mic_announcer.announce_transition(
                ("microphone-result", self._mic_combo.currentData(), msg), self._mic_status.text()
            )

    def _populate_mic_combo(self, preserve_selection: bool = False):
        if self._mic_combo is None:
            return
        preserved_name = self._mic_combo.currentText() if preserve_selection else None
        self._mic_combo.blockSignals(True)
        self._mic_combo.clear()
        if self._mics:
            self._mic_combo.addItems([m['name'] for m in self._mics])
            self._mic_combo.setEnabled(True)
            if preserve_selection:
                idx = pick_index_by_name(self._mics, preserved_name)
                if idx is not None:
                    self._mic_combo.setCurrentIndex(idx)
            if self._mic_status:
                self._mic_status.setText("Speak to test your microphone")
                self._mic_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        else:
            if self._mic_scan_error is not None:
                self._mic_combo.addItem("Couldn't scan audio devices — press Refresh")
                if self._mic_status:
                    self._mic_status.setText(
                        f"Mic scan failed ({self._mic_scan_error})",
                    )
                    self._mic_status.setStyleSheet(
                        f"color:{theme.WARNING};font-size:{theme.TYPE_BODY}px;",
                    )
            else:
                self._mic_combo.addItem("No microphones detected")
                if self._mic_status:
                    self._mic_status.setText("")
            self._mic_combo.setEnabled(False)
        self._mic_combo.blockSignals(False)
        # Restart meter now that device list is ready
        if _STEPS[self._step][0] == "Microphone":
            self._start_meter()
