"""Guided setup wizard for Ava, Samsara's local AI assistant.

A 4-step wizard that walks users through:
  1. What Ava does (value prop + concrete examples)
  2. Installing Ollama
  3. Choosing and pulling a model
  4. Finishing up (enables the AI pack, confirms everything works)

No audio required — background threads handle HTTP checks and subprocess pulls.

Public API (same wrapper pattern as MicSetupWizardQt):
    AvaGuideQt(app).show()
"""

import weakref

import json
import re
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget, QScrollArea,
)

from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara import ai_preferences
from samsara.runtime import thread_registry
from samsara.ui import ava_consent_qt, qt_runtime

from samsara.log import get_logger
from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Colours — identical to mic_setup_wizard_qt.py
# ---------------------------------------------------------------------------


# Colour comes from samsara.ui.theme, never from a literal here: this
# module used to keep its own copy of the dark palette, which is exactly
# how a second palette leaves one window unreadable (queue 129).

def _primary_ss():
    return (
        f"QPushButton{{background:{theme.ACCENT};color:{theme.BG0};"
        f"border:none;border-radius:4px;font-weight:bold;padding:6px 18px;}}"
        f"QPushButton:hover{{background:{theme.ACCENT_HOVER};color:{theme.BG0};}}"
        f"QPushButton:disabled{{background:{theme.BG2};color:{theme.TEXT_DISABLED};"
        f"border:1px solid {theme.BORDER};}}"
    )


def _ss() -> str:
    """The window's stylesheet, built on demand. Never a module
    constant: an f-string evaluated at import time freezes the
    palette that happened to be live then (queue 129)."""
    return f"""
    QDialog, QWidget {{
        color: {theme.TEXT_PRIMARY};
        font-family: 'Segoe UI', sans-serif;
        font-size: {theme.TYPE_BODY}px;
    }}
    QDialog, QScrollArea, QStackedWidget, QWidget#avaPage {{ background: {theme.BG0}; }}
    QPushButton {{
        background: {theme.BG2};
        color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER};
        padding: 6px 18px;
        border-radius: 4px;
        min-width: 80px;
    }}
    QPushButton:hover {{
        background: {theme.ACCENT};
        color: {theme.BG0};
        border-color: {theme.ACCENT};
    }}
    QPushButton:disabled {{
        background: {theme.BG2};
        color: {theme.TEXT_DISABLED};
        border-color: {theme.BORDER};
    }}
    QPushButton#primary {{
        background: {theme.ACCENT};
        color: {theme.BG0};
        border-color: {theme.ACCENT};
        font-weight: bold;
    }}
    QPushButton#primary:hover {{ background: {theme.ACCENT_HOVER}; }}
    QPushButton#ghost {{
        background: transparent;
        color: {theme.TEXT_SECONDARY};
        border: none;
    }}
    QPushButton#ghost:hover {{ color: {theme.TEXT_PRIMARY}; background: transparent; }}
    QComboBox {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {theme.BG1};
        color: {theme.TEXT_PRIMARY};
        selection-background-color: {theme.ACCENT};
        selection-color: {theme.BG0};
        border: 1px solid {theme.BORDER};
    }}
    QLabel {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: {theme.BG0}; width: 12px; }}
    QScrollBar::handle:vertical {{ background: {theme.BORDER}; min-height: 24px; border-radius: 4px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""

# ---------------------------------------------------------------------------
# Ollama model recommendations
# ---------------------------------------------------------------------------

_MODELS = [
    ("qwen2.5:3b",  "~2 GB",    "Fastest — best for quick command help"),
    ("llama3.2",    "~2 GB",    "Balanced quality and speed"),
    ("qwen2.5:7b",  "~4.7 GB",  "Smarter — handles complex questions"),
    ("mistral",     "~4.1 GB",  "Reliable all-rounder, good reasoning"),
]

_OLLAMA_URL = "http://localhost:11434"
_OLLAMA_DOWNLOAD = "https://ollama.com/download"
_OLLAMA_RECHECK_INTERVAL_MS = 5_000
_OLLAMA_RECHECK_LIMIT = 120  # ten minutes at one check every five seconds
# PLACEHOLDER COPY (462)
_AVA_CLOUD_GUIDE_NOTE = ('Ava is set to the cloud in Settings > Ava. '
                        'This guide sets up Ava on this PC.')

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)

_AVA_KEY_OPTIONS = [
    ("Right Alt (default)", "right_alt"),
    ("Right Ctrl",          "rctrl"),
    ("F13",                 "f13"),
]


def _ping_ollama(timeout: int = 3, host: str = _OLLAMA_URL) -> tuple[bool, list[str]]:
    """Return (is_running, list_of_installed_model_names)."""
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            return True, models
    except Exception:
        return False, []


def _clean_ollama_output(text: str) -> str:
    """Keep model-pull progress readable without leaking terminal controls."""
    text = _ANSI_RE.sub("", text).replace("\r", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class AvaGuideQt:
    """Drop-in Qt wizard — same open/close pattern as MicSetupWizardQt."""

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
        self._window = _WizardWindow(self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
        self._init_posted = False


# ---------------------------------------------------------------------------
# Wizard window
# ---------------------------------------------------------------------------

class _AvaStepIndicator(StepIndicator):
    """Keep the painted step labels clear of Ava's divider at every refresh."""

    def _refresh(self):
        super()._refresh()
        self.setFixedHeight(self.height() + round(theme.SPACE_1 * self.ui_scale))
        self._size_items()


class _WizardWindow(QDialog):

    _ollama_status_sig = Signal(bool, list)   # (is_running, model_names)
    _pull_line_sig     = Signal(str)           # one line of pull output
    _pull_done_sig     = Signal(bool)          # success flag

    _STEP_INTRO  = 0
    _STEP_OLLAMA = 1
    _STEP_MODEL  = 2
    _STEP_DONE   = 3

    _STEP_TITLES = [
        "Meet Ava",
        "Install Ollama",
        "Choose a model",
        "Setup status",
    ]

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._pull_proc: "subprocess.Popen | None" = None
        self._pulling = False
        self._installed_models: list[str] = []
        self._ollama_running: bool | None = None
        self._skipped_steps: set[int] = set()
        configured_model = (app.config.get("ollama") or {}).get("model")
        self._selected_model_name = (
            configured_model if configured_model in {name for name, _, _ in _MODELS}
            else _MODELS[0][0]
        )
        self._ollama_recheck_attempts = 0
        self._ollama_recheck_timer = QTimer(self)
        self._ollama_recheck_timer.setInterval(_OLLAMA_RECHECK_INTERVAL_MS)
        self._ollama_recheck_timer.timeout.connect(self._auto_recheck_ollama)

        self.setWindowTitle("Ava Setup Guide")
        self.setFixedWidth(580)
        self.setStyleSheet(_ss())
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        self._build_ui()
        theme.apply_design_scale(self)
        self._ollama_status_sig.connect(self._on_ollama_status)
        self._pull_line_sig.connect(self._on_pull_line)
        self._pull_done_sig.connect(self._on_pull_done)
        self._go_to(self._STEP_INTRO)
        self._size_for_pages()

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(60)
        hdr.setObjectName('avaHeader')
        hdr.setStyleSheet(f"QWidget#avaHeader{{background:{theme.BG0};border-bottom:1px solid {theme.BORDER_FAINT};}}")
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

        self._step_strip = QWidget()
        self._step_strip.setObjectName('avaStepContainer')
        self._step_strip.setStyleSheet(f'QWidget#avaStepContainer{{border-bottom:1px solid {theme.BORDER_FAINT};}}')
        step_layout = QVBoxLayout(self._step_strip)
        step_layout.setContentsMargins(0, 0, 0, theme.SPACE_2)
        self._step_indicator = _AvaStepIndicator(["Intro", "Ollama", "Model", "Done"])
        self._step_indicator.setObjectName("avaGuideStepStrip")
        step_layout.addWidget(self._step_indicator)
        root.addWidget(self._step_strip)

        # Page stack
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_intro_page())
        self._stack.addWidget(self._build_ollama_page())
        self._stack.addWidget(self._build_model_page())
        self._stack.addWidget(self._build_done_page())
        root.addWidget(self._stack, stretch=1)

        # Nav bar
        nav = QWidget()
        nav.setFixedHeight(64)
        nav.setObjectName('avaNavigation')
        nav.setStyleSheet(f"QWidget#avaNavigation{{background:{theme.BG1};border-top:1px solid {theme.BORDER};}}")
        nav_lay = QHBoxLayout(nav)
        nav_lay.setContentsMargins(24, 0, 24, 0)
        nav_lay.setSpacing(10)

        self._back_btn = QPushButton("Back")
        self._back_btn.clicked.connect(self._go_back)
        nav_lay.addWidget(self._back_btn)

        self._skip_btn = QPushButton("Skip")
        self._skip_btn.setObjectName("ghost")
        self._skip_btn.clicked.connect(self._skip_step)
        nav_lay.addWidget(self._skip_btn)

        nav_lay.addStretch()

        self._next_btn = QPushButton("Next  ->")
        self._next_btn.setStyleSheet(_primary_ss())
        self._next_btn.clicked.connect(self._go_next)
        nav_lay.addWidget(self._next_btn)

        root.addWidget(nav)

    def _available_height(self):
        return self.screen().availableGeometry().height() - 40

    def _size_for_pages(self):
        """Reserve the tallest page once; scrolling is only a short-screen fallback."""
        self.ensurePolished()
        for widget in self.findChildren(QWidget):
            widget.ensurePolished()
        for button, labels in ((self._back_btn, ('Back',)),
                               (self._skip_btn, ('Skip', 'Set up later')),
                               (self._next_btn, ('Next  ->', 'Finish', 'Set up later', 'Enable Ava'))):
            original = button.text()
            widths = []
            for label in labels:
                button.setText(label)
                button.ensurePolished()
                widths.append(button.sizeHint().width())
            button.setText(original)
            button.setFixedWidth(max(widths) + theme.SPACE_1)
        dynamic = (self._install_instructions, self._pull_progress, self._installed_note,
                   self._key_combo.reason_label())
        hidden = [widget.isHidden() for widget in dynamic]
        for widget in dynamic:
            widget.show()
        summary = self._done_summary.text()
        self._done_summary.setText(
            f'Ollama: not reachable\nModel: {self._selected_model_name} (not installed)\n{_AVA_CLOUD_GUIDE_NOTE}')
        heights = []
        for index in range(self._stack.count()):
            page = self._stack.widget(index).widget()
            page.ensurePolished()
            page.resize(568, self._available_height())
            layout = page.layout()
            layout.activate()
            layout.invalidate()
            # Reserve the scrollbar width too, so an initial narrow viewport
            # cannot keep a needless scrollbar alive through word wrapping.
            heights.append(max(layout.minimumSize().height(), layout.totalHeightForWidth(568)))
        for widget, was_hidden in zip(dynamic, hidden):
            widget.setHidden(was_hidden)
        self._done_summary.setText(summary)
        strip_height = max(self._step_strip.sizeHint().height(), self._step_strip.minimumSizeHint().height())
        chrome = self.layout().itemAt(0).widget().height() + strip_height + 64
        self.setFixedHeight(min(max(heights) + chrome + theme.SPACE_2, self._available_height()))

    # ---- Page builders ----

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, theme.SPACE_2, 32, theme.SPACE_1)
        lay.setSpacing(theme.SPACE_1)

        lay.addWidget(_body(_ava_intro_text(self._app.config)))
        lay.addWidget(_body(
            "The main thing Ava does: <b>you don't have to memorise command phrases.</b> "
            "Say what you mean in plain language, and Ava figures out the right action."
        ))
        lay.addWidget(_body(_confirmation_text()))

        # Example table
        examples_frame = QWidget()
        examples_frame.setObjectName('avaExamples')
        examples_frame.setStyleSheet(
            f"QWidget#avaExamples{{background:{theme.BG1};border-radius:6px;border:1px solid {theme.BORDER};}}"
        )
        ex_lay = QVBoxLayout(examples_frame)
        ex_lay.setContentsMargins(16, 12, 16, 12)
        ex_lay.setSpacing(theme.SPACE_1 // 2)

        ex_hdr = QHBoxLayout()
        ex_hdr.addWidget(_small_bold("Instead of remembering...", theme.TEXT_SECONDARY), stretch=1)
        ex_hdr.addWidget(_small_bold("You can say...", theme.ACCENT), stretch=1)
        ex_lay.addLayout(ex_hdr)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color:{theme.BORDER};")
        ex_lay.addWidget(div)

        _EXAMPLES = [
            ('"scroll down a little"',          '"scroll just a tiny bit"'),
            ('"pain level 6"',                  '"my pain is about a 6 today"'),
            ('"took ibuprofen 400mg"',           '"I just took my ibuprofen"'),
            ('"complete alarm"',                 '"I finished my stretch"'),
            ('"read alarms"',                    '"what alarms do I have set?"'),
        ]
        for exact, natural in _EXAMPLES:
            row = QHBoxLayout()
            row.setSpacing(12)
            lft = QLabel(exact)
            lft.setStyleSheet(
                f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-family:'Consolas',monospace;"
            )
            rgt = QLabel(natural)
            rgt.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_MIN}px;font-style:italic;")
            row.addWidget(lft, stretch=1)
            row.addWidget(rgt, stretch=1)
            ex_lay.addLayout(row)

        lay.addWidget(examples_frame)

        lay.addWidget(_body(
            "Ava uses a small AI model that runs locally via Ollama. "
            "The next steps get that set up."
        ))

        lay.addStretch()
        return _scroll_page(page)

    def _build_ollama_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 16)
        lay.setSpacing(14)

        lay.addWidget(_body(
            # OWNER COPY: explain local runtime setup without promising automatic service startup.
            "Ollama runs local AI models. This guide checks whether Samsara can reach it."
        ))

        # Status card
        status_card = QWidget()
        status_card.setObjectName('avaOllamaStatus')
        status_card.setStyleSheet(
            f"QWidget#avaOllamaStatus{{background:{theme.BG1};border-radius:6px;border:1px solid {theme.BORDER};}}"
        )
        sc_lay = QHBoxLayout(status_card)
        sc_lay.setContentsMargins(16, 14, 16, 14)
        sc_lay.setSpacing(12)

        self._ollama_dot = QLabel("●")
        self._ollama_dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_HEADING}px;")
        sc_lay.addWidget(self._ollama_dot)

        self._ollama_status_lbl = QLabel("Checking...")
        self._ollama_status_lbl.setWordWrap(True)
        self._ollama_status_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        sc_lay.addWidget(self._ollama_status_lbl, stretch=1)

        self._ollama_check_btn = QPushButton("Check again")
        self._ollama_check_btn.setFixedWidth(110)
        self._ollama_check_btn.clicked.connect(self._check_ollama)
        sc_lay.addWidget(self._ollama_check_btn)

        lay.addWidget(status_card)

        # Download instructions (shown when Ollama not found)
        self._install_instructions = QWidget()
        inst_lay = QVBoxLayout(self._install_instructions)
        inst_lay.setContentsMargins(0, 0, 0, 0)
        inst_lay.setSpacing(8)

        inst_lay.addWidget(_body(
            # OWNER COPY: a failed HTTP check cannot prove Ollama is absent.
            "Samsara cannot connect to Ollama. To check its installation:"
        ))

        self._download_ollama_btn = QPushButton("Download Ollama")
        self._download_ollama_btn.setObjectName("primary")
        self._download_ollama_btn.setFixedWidth(170)
        self._download_ollama_btn.clicked.connect(self._open_ollama_download)
        inst_lay.addWidget(self._download_ollama_btn)

        steps = [
            ("1.", "Click Download Ollama above."),
            ("2.", "Run the installer."),
            ("3.", "Come back. This page checks automatically."),
        ]
        for num, text in steps:
            step_row = QHBoxLayout()
            step_row.setSpacing(8)
            num_lbl = QLabel(num)
            num_lbl.setFixedWidth(20)
            num_lbl.setStyleSheet(f"color:{theme.ACCENT};font-weight:bold;font-size:{theme.TYPE_BODY}px;")
            step_row.addWidget(num_lbl)
            txt_lbl = QLabel(text)
            txt_lbl.setWordWrap(True)
            txt_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
            step_row.addWidget(txt_lbl, stretch=1)
            inst_lay.addLayout(step_row)

        note = QLabel(_keep_warm_note(self._app.config))
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;")
        inst_lay.addWidget(note)

        lay.addWidget(self._install_instructions)
        lay.addStretch()
        return _scroll_page(page)

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 16)
        lay.setSpacing(14)

        lay.addWidget(_body(
            # OWNER COPY: qualify local model availability and route selection.
            "Choose a local model below. Pull downloads it for use when the local Ava route is enabled."
        ))

        # Model selector
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_label = _label("Model:")
        model_row.addWidget(model_label)

        self._model_combo = QComboBox()
        model_label.setBuddy(self._model_combo)
        self._model_combo.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        for name, size, desc in _MODELS:
            self._model_combo.addItem(f"{name}  ({size})", userData=name)
        selected_index = self._model_combo.findData(self._selected_model_name)
        self._model_combo.setCurrentIndex(selected_index)
        self._model_combo.currentIndexChanged.connect(self._on_model_selected)
        model_row.addWidget(self._model_combo)
        model_row.addStretch()
        lay.addLayout(model_row)

        # Description label
        self._model_desc = QLabel(_MODELS[selected_index][2])
        self._model_desc.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;"
        )
        lay.addWidget(self._model_desc)

        # Pull controls
        pull_row = QHBoxLayout()
        pull_row.setSpacing(10)

        self._model_status_dot = QLabel("●")
        self._model_status_dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_BODY}px;")
        pull_row.addWidget(self._model_status_dot)

        self._model_status_lbl = QLabel("Not checked yet")
        self._model_status_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        pull_row.addWidget(self._model_status_lbl, stretch=1)

        self._pull_btn = QPushButton("Pull model")
        self._pull_btn.setFixedWidth(110)
        self._pull_btn.clicked.connect(self._pull_model)
        pull_row.addWidget(self._pull_btn)

        lay.addLayout(pull_row)

        # Pull progress readout
        self._pull_progress = QLabel("")
        self._pull_progress.setWordWrap(True)
        self._pull_progress.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;"
            f"font-family:'Consolas',monospace;"
            f"background:{theme.BG1};border-radius:4px;padding:6px 8px;"
        )
        self._pull_progress.setVisible(False)
        lay.addWidget(self._pull_progress)

        # Already have a model note
        self._installed_note = QLabel("")
        self._installed_note.setWordWrap(True)
        self._installed_note.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-style:italic;"
        )
        lay.addWidget(self._installed_note)

        lay.addStretch()
        return _scroll_page(page)

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, theme.SPACE_2, 32, theme.SPACE_1)
        lay.setSpacing(theme.SPACE_1)

        # OWNER COPY: review the incomplete setup heading.
        title = QLabel("Ava is not ready.")
        self._done_title = title
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

        # ---- Activation key selector ----
        key_frame = QWidget()
        key_frame.setObjectName('avaKeyGroup')
        key_frame.setStyleSheet(
            f"QWidget#avaKeyGroup{{background:{theme.BG1};border-radius:6px;border:1px solid {theme.BORDER};}}"
        )
        kf_lay = QVBoxLayout(key_frame)
        kf_lay.setContentsMargins(16, 12, 16, 12)
        kf_lay.setSpacing(8)

        key_row = QHBoxLayout()
        key_row.setSpacing(theme.SPACE_2)
        key_text = QVBoxLayout()
        key_text.setSpacing(theme.SPACE_1)
        key_text.addWidget(_small_bold("Activation key", theme.TEXT_PRIMARY))
        key_text.addWidget(_body(
            "Hold this key and speak — Ava listens while you hold it, "
            "then responds when you release."
        ))

        key_row.addLayout(key_text, stretch=1)

        from samsara.ui.settings.shared_qt import _HotkeyButton
        owner_ref = weakref.ref(self)
        def key_changed():
            owner = owner_ref()
            if owner is not None:
                owner._on_ava_key_changed()
        def key_config():
            owner = owner_ref()
            return owner._app.config if owner is not None else {}
        self._key_combo = _HotkeyButton(self._app.config.get("ava_mode_key", "right_alt"),
                                       on_change=key_changed)
        self._key_combo.configure_hold('ava_mode_key', key_config)
        self._key_combo.setAccessibleName('Activation key')  # 418: the name shares its source with the visible label
        self._key_combo.setMinimumWidth(0)
        self._key_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        key_row.addWidget(self._key_combo, alignment=Qt.AlignmentFlag.AlignVCenter)
        kf_lay.addLayout(key_row)
        kf_lay.addWidget(self._key_combo.reason_label())

        lay.addWidget(key_frame)

        # ---- How to use ----
        usage_frame = QWidget()
        usage_frame.setObjectName('avaUsage')
        usage_frame.setStyleSheet(
            f"QWidget#avaUsage{{background:{theme.BG1};border-radius:6px;border:1px solid {theme.BORDER};}}"
        )
        uf_lay = QVBoxLayout(usage_frame)
        uf_lay.setContentsMargins(16, 12, 16, 12)
        uf_lay.setSpacing(6)

        uf_lay.addWidget(_small_bold("How to use Ava", theme.ACCENT))

        wake = self._app.config.get("wake_word_config", {}).get("phrase", DEFAULT_WAKE_PHRASE)
        usage_lines = _usage_lines(self._app.config, wake)
        for line in usage_lines:
            row = QHBoxLayout()
            row.setSpacing(8)
            bullet = QLabel("·")
            bullet.setFixedWidth(12)
            bullet.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_HEADING}px;")
            row.addWidget(bullet)
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_MIN}px;")
            row.addWidget(lbl, stretch=1)
            uf_lay.addLayout(row)

        lay.addWidget(usage_frame)

        lay.addStretch()
        return _scroll_page(page)

    # ----------------------------------------------------------------
    # Step navigation
    # ----------------------------------------------------------------

    def _go_to(self, step: int):
        self._current_step = step
        self._stack.setCurrentIndex(step)

        self._title_lbl.setText(self._STEP_TITLES[step])
        self._step_lbl.setText(f"Step {step + 1} of 4")

        self._step_indicator.set_progress(step, self._skipped_steps)

        self._back_btn.setVisible(step > 0)

        if step == self._STEP_DONE:
            self._skip_btn.hide()
            self._ollama_running = None
            self._installed_models = []
            self._next_btn.setText("Set up later")
            self._next_btn.setEnabled(True)
        else:
            self._skip_btn.show()
            self._skip_btn.setText('Skip')
            self._next_btn.setText("Next  ->")

        if step == self._STEP_INTRO:
            self._next_btn.setEnabled(True)

        elif step == self._STEP_OLLAMA:
            self._next_btn.setEnabled(True)  # always skippable
            self._check_ollama()

        elif step == self._STEP_MODEL:
            self._check_installed_models()

        elif step == self._STEP_DONE:
            self._build_done_summary()
            self._check_ollama()

    def _go_next(self):
        if self._current_step == self._STEP_DONE:
            if self._consent_only_missing:
                self._enable_ai_pack()
            else:
                self._finish()
            return
        if self._current_step < self._STEP_DONE:
            self._skipped_steps.discard(self._current_step)
            self._go_to(self._current_step + 1)

    def _go_back(self):
        if self._current_step > 0:
            self._go_to(self._current_step - 1)

    def _skip_step(self):
        if self._current_step == self._STEP_DONE:
            self.close()
        elif self._current_step < self._STEP_DONE:
            self._skipped_steps.add(self._current_step)
            self._go_to(self._current_step + 1)

    # ----------------------------------------------------------------
    # Ollama step
    # ----------------------------------------------------------------

    def _open_ollama_download(self):
        QDesktopServices.openUrl(QUrl(_OLLAMA_DOWNLOAD))
        self._ollama_recheck_attempts = 0
        self._ollama_recheck_timer.start()

    def _auto_recheck_ollama(self):
        if self._ollama_recheck_attempts >= _OLLAMA_RECHECK_LIMIT:
            self._ollama_recheck_timer.stop()
            return
        self._ollama_recheck_attempts += 1
        self._check_ollama()
        if self._ollama_recheck_attempts >= _OLLAMA_RECHECK_LIMIT:
            self._ollama_recheck_timer.stop()

    def _check_ollama(self):
        self._ollama_dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_HEADING}px;")
        self._ollama_status_lbl.setText("Checking...")
        self._ollama_check_btn.setEnabled(False)

        def _run():
            host = self._app.config.get("ollama", {}).get("host") or _OLLAMA_URL
            running, models = _ping_ollama(host=host)
            self._ollama_status_sig.emit(running, models)

        thread_registry.spawn("ava-ping", _run, daemon=True)

    def _on_ollama_status(self, running: bool, models: list):
        self._ollama_check_btn.setEnabled(True)
        self._ollama_running = running
        self._installed_models = models if running else []

        if running:
            self._ollama_recheck_timer.stop()
            self._ollama_dot.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_HEADING}px;")
            if models:
                self._ollama_status_lbl.setText(
                    f"Ollama is running  |  {len(models)} model"
                    f"{'s' if len(models) != 1 else ''} installed"
                )
            else:
                self._ollama_status_lbl.setText(
                    "Ollama is running — no models yet (pull one on the next step)"
                )
            self._install_instructions.setVisible(False)
        else:
            self._ollama_dot.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_HEADING}px;")
            self._ollama_status_lbl.setText("Cannot connect to Ollama")
            self._install_instructions.setVisible(True)
        if self._current_step == self._STEP_DONE:
            self._build_done_summary()

    # ----------------------------------------------------------------
    # Model step
    # ----------------------------------------------------------------

    def _on_model_selected(self, index: int):
        if 0 <= index < len(_MODELS):
            self._selected_model_name = _MODELS[index][0]
            self._model_desc.setText(_MODELS[index][2])
        self._refresh_model_status()

    def _check_installed_models(self):
        self._model_status_dot.setStyleSheet(f"color:{theme.TEXT_DISABLED};font-size:{theme.TYPE_BODY}px;")
        self._model_status_lbl.setText("Checking Ollama...")
        self._pull_btn.setEnabled(False)
        self._next_btn.setEnabled(False)

        def _run():
            host = self._app.config.get("ollama", {}).get("host") or _OLLAMA_URL
            running, models = _ping_ollama(host=host)
            self._ollama_status_sig.emit(running, models)   # reuse signal
            # also fire model refresh via same signal path
            self._pull_line_sig.emit("")  # sentinel to trigger _refresh_model_status

        thread_registry.spawn(
            "ava-model-check", _run, daemon=True
        )

    def _refresh_model_status(self):
        sel = self._selected_model_name
        matched = sel in self._installed_models and self._ollama_running is True

        if matched:
            self._model_status_dot.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_BODY}px;")
            self._model_status_lbl.setText(f"Installed: {sel}")
            self._pull_btn.setEnabled(True)
            self._pull_btn.setText("Re-pull")
            self._next_btn.setEnabled(True)
            self._next_btn.setText("Next  ->")
        else:
            self._model_status_dot.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_BODY}px;")
            self._model_status_lbl.setText(
                "Not installed — click Pull to download" if self._ollama_running
                else "Cannot connect to Ollama"
            )
            self._pull_btn.setEnabled(self._ollama_running is True)
            self._pull_btn.setText("Pull model")
            self._next_btn.setEnabled(True)
            self._next_btn.setText("Set up later")

        # Show any installed models as a note
        if self._installed_models and not matched:
            others = ", ".join(self._installed_models[:4])
            if len(self._installed_models) > 4:
                others += f" +{len(self._installed_models) - 4} more"
            self._installed_note.setText(f"You already have: {others}")
        elif not self._installed_models:
            self._installed_note.setText("")

    def _on_pull_line(self, line: str):
        # Sentinel from _check_installed_models
        if line == "" and self._current_step == self._STEP_MODEL:
            self._refresh_model_status()
            return
        if line:
            self._pull_progress.setVisible(True)
            self._pull_progress.setText(line.strip())

    def _pull_model(self):
        if self._pulling:
            return
        model = self._selected_model_name
        self._pulling = True
        self._pull_btn.setEnabled(False)
        self._pull_btn.setText("Pulling...")
        self._pull_progress.setVisible(True)
        self._pull_progress.setText("Starting download...")
        self._model_status_dot.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_BODY}px;")
        self._model_status_lbl.setText(f"Downloading {model}...")

        def _run():
            success = False
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", model],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    ),
                )
                self._pull_proc = proc
                for raw_line in proc.stdout:
                    line = _clean_ollama_output(raw_line)
                    if line:
                        self._pull_line_sig.emit(line)
                proc.wait()
                success = proc.returncode == 0
            except FileNotFoundError:
                logger.exception("Ava model pull failed: ollama was not found on PATH")
                self._pull_line_sig.emit(
                    "Couldn't download the model: Ollama was not found on PATH."
                )
            except Exception:
                logger.exception("Ava model pull failed")
                self._pull_line_sig.emit(
                    "Couldn't download the model: Ollama could not start the download."
                )
            finally:
                self._pull_proc = None
                self._pulling = False
            self._pull_done_sig.emit(success)

        thread_registry.spawn("ava-pull", _run, daemon=True)

    def _on_pull_done(self, success: bool):
        self._pull_btn.setEnabled(True)
        self._pull_btn.setText("Pull model")
        if success:
            self._pull_progress.setText("Download complete.")
            self._check_installed_models()
            self._save_model_choice(self._selected_model_name)
        else:
            self._model_status_dot.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_BODY}px;")
            self._model_status_lbl.setText("Download failed — see above for details")

    def _save_model_choice(self, model_name: str):
        try:
            cfg = dict(self._app.config.get("ollama", {}))
            cfg["model"] = model_name
            self._app.update_config({"ollama": cfg}, save=True)
        except Exception as e:
            logger.debug(f"_save_model_choice: {e}")

    # ----------------------------------------------------------------
    # Done step
    # ----------------------------------------------------------------

    def _build_done_summary(self):
        selected = self._selected_model_name
        installed = self._ollama_running is True and selected in self._installed_models
        configured_model = self._app.config.get("ollama", {}).get("model")
        configured_and_installed = installed and selected == configured_model
        host = self._app.config.get("ollama", {}).get("host") or _OLLAMA_URL
        try:
            resolved_addresses = ("127.0.0.1", "::1") if urlsplit(host).hostname == "localhost" else ()
        except (TypeError, ValueError):
            resolved_addresses = ()
        state = ai_preferences.effective_state(
            self._app.config, "ava", provider_route="local", ready=configured_and_installed,
            resolved_addresses=resolved_addresses,
        )
        ready = configured_and_installed and state.allowed
        policy = (self._app.config.get('ava') or {}).get('provider_policy', 'off')
        self._consent_only_missing = (policy == 'local' and configured_and_installed
                                     and (self._app.config.get('ollama') or {}).get('enabled') is True
                                     and not state.allowed and 'consent' in state.reason.lower())
        # PLACEHOLDER COPY (462): exact missing prerequisite, never a generic setup label.
        if policy == 'cloud':
            missing = _AVA_CLOUD_GUIDE_NOTE
        elif policy == 'off' or state.status == 'off-by-choice':
            missing = 'Ava is set to Off in Settings > Ava.'
        elif self._ollama_running is None:
            missing = 'Ollama has not been checked yet.'
        elif not self._ollama_running:
            missing = 'Ollama is not reachable.'
        elif not installed:
            missing = 'The selected model is not installed.'
        elif not configured_and_installed:
            missing = 'The selected model is not configured in Settings > Ava.'
        elif self._consent_only_missing:
            missing = 'Ava needs your OK.'
        else:
            missing = state.reason
        self._done_title.setText("Ava is ready." if ready else "Ava is not ready.")
        self._done_title.setStyleSheet(
            f"color:{theme.SUCCESS if ready else theme.WARNING};"
            f"font-size:{theme.TYPE_TITLE}px;font-weight:bold;"
        )
        parts = [
            "Ollama: " + ("reachable" if self._ollama_running is True else
                          "not reachable" if self._ollama_running is False else "not checked yet"),
            f"Model: {selected} ({'installed' if installed else 'not installed'})",
            'Ava: enabled' if ready else missing,
        ]
        self._done_summary.setText("\n".join(parts))
        self._skip_btn.setText('Set up later')
        self._skip_btn.setVisible(self._consent_only_missing)
        self._next_btn.setText('Finish' if ready else
                               'Enable Ava' if self._consent_only_missing else 'Set up later')

    def _on_ava_key_changed(self):
        try:
            self._app.update_config({"ava_mode_key": self._key_combo.combo}, save=True)
        except Exception as e:
            logger.debug(f"_on_ava_key_changed: {e}")

    def _enable_ai_pack(self):
        self._build_done_summary()
        if not self._consent_only_missing:
            return
        try:
            policy = 'local'
            consent = ava_consent_qt.request_consent(
                self, self._app.config, cloud_enabled=policy == "cloud"
            )
            if consent is None:
                return
            self._build_done_summary()
            if not self._consent_only_missing:
                return
            ava_consent_qt.apply_accepted_ava(
                self._app, consent, policy=policy
            )
            self._build_done_summary()
        except Exception:
            logger.exception("Could not activate Ava after consent")

    def _finish(self):
        # Save model choice from combo if not already saved by a pull
        model_name = self._model_combo.currentData()
        if model_name:
            self._save_model_choice(model_name)
        self.close()

    # ----------------------------------------------------------------
    # Cleanup
    # ----------------------------------------------------------------

    def closeEvent(self, e):
        self._ollama_recheck_timer.stop()
        if self._pull_proc is not None:
            try:
                from samsara.process_exit import trace_exit
                trace_exit("model-pull-child-terminate", target_pid=self._pull_proc.pid)
                self._pull_proc.terminate()
            except Exception as e:
                logger.debug(f"closeEvent: {e}")
        e.accept()


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------

def _scroll_page(page: QWidget) -> QScrollArea:
    page.setObjectName('avaPage')
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    return scroll


def _ava_intro_text(config: dict) -> str:
    """Describe authoritative Ava policy without guessing provider readiness."""
    state = ai_preferences.effective_state(config, "ava", ready=None)
    # OWNER COPY: replace this state-derived provider explanation.
    return f"Ava is optional. {state.reason} Check Settings > Ava for the current provider route."


def _confirmation_text() -> str:
    """Describe model-proposed actions without promising every UI action asks."""
    from samsara.session_modes import SCRATCH_THAT_PHRASE  # noqa: PLC0415
    # OWNER COPY: explain the exact model-proposed action policy.
    return ("Ava may ask before a model-proposed action. Review the action, then say "
            "<b>\"yes\"</b> to continue or <b>\"ava cancel\"</b> / "
            f"<b>\"{SCRATCH_THAT_PHRASE}\"</b> to drop it.")


def _keep_warm_note(config: dict) -> str:
    """Describe the warm request without claiming Ollama memory residency."""
    keep_warm = bool((config.get("ava_command_session") or {}).get("keep_warm", True))
    # OWNER COPY: explain request and server retention separately.
    if keep_warm:
        return ("Keep model warm is on in Settings > Ava > Advanced. Samsara requests warming; "
                "Ollama controls how long the model stays loaded.")
    return ("Keep model warm is off in Settings > Ava > Advanced. "
            "Ollama controls how long the model stays loaded.")


def _usage_lines(config: dict, wake: str) -> list:
    """Show supported controls without promising a legacy provider switch."""
    from samsara import session_modes  # noqa: PLC0415
    ava_mode = next((p for p, m in session_modes._WHOLE_UTTERANCE_SWITCHES.items()
                     if m is session_modes.SessionMode.AVA), "ava mode")
    key = config.get("ava_mode_key", "right_alt")
    key_label = next((label for label, value in _AVA_KEY_OPTIONS if value == key), str(key))
    # OWNER COPY: qualify these controls against live Ava and Wake readiness.
    return [
        f"Ava key: {key_label}. Its availability follows Settings > Ava.",
        f'If Ava is enabled, say "{ava_mode}" inside a hands-free session to switch lanes.',
        "Use an active command route for Ava questions and cancellation.",
    ]


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;line-height:1.5;")
    return lbl


def _small_bold(text: str, color: str | None = None) -> QLabel:
    # None, not the token itself: a default argument is evaluated when the
    # def runs, so a token there keeps the palette that was live at import
    # (queue 129).
    if color is None:
        color = theme.TEXT_PRIMARY
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color};font-size:{theme.TYPE_MIN}px;font-weight:bold;letter-spacing:0.5px;"
    )
    return lbl
