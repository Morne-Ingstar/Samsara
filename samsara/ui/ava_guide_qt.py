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

import json
import subprocess
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Colours — identical to mic_setup_wizard_qt.py
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
QPushButton:disabled {{
    background: {_ELEVATED};
    color: {_MUTED};
    border-color: {_BORDER};
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


def _ping_ollama(timeout: int = 3) -> tuple[bool, list[str]]:
    """Return (is_running, list_of_installed_model_names)."""
    try:
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            return True, models
    except Exception:
        return False, []


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class AvaGuideQt:
    """Drop-in Qt wizard — same open/close pattern as MicSetupWizardQt."""

    def __init__(self, app):
        self.app = app
        self._window: "_WizardWindow | None" = None

    @property
    def window(self):
        return self._window

    def show(self):
        qt_app = QApplication.instance()
        if qt_app is None:
            return
        if self._window is not None:
            QTimer.singleShot(0, self._window.show)
            QTimer.singleShot(0, self._window.raise_)
            QTimer.singleShot(0, self._window.activateWindow)
            return
        QTimer.singleShot(0, qt_app, self._init_window)

    def close(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.close)

    def _init_window(self):
        self._window = _WizardWindow(self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None


# ---------------------------------------------------------------------------
# Wizard window
# ---------------------------------------------------------------------------

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
        "You're ready",
    ]

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._pull_proc: "subprocess.Popen | None" = None
        self._pulling = False
        self._installed_models: list[str] = []
        self._selected_model_name = _MODELS[0][0]

        self.setWindowTitle("Ava Setup Guide")
        self.setFixedSize(580, 500)
        self.setStyleSheet(_SS)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        self._build_ui()
        self._ollama_status_sig.connect(self._on_ollama_status)
        self._pull_line_sig.connect(self._on_pull_line)
        self._pull_done_sig.connect(self._on_pull_done)
        self._go_to(self._STEP_INTRO)

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
        hdr.setStyleSheet(f"background:{_SURFACE};border-bottom:1px solid {_BORDER};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(24, 0, 24, 0)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            f"color:{_TEXT_PRI};font-size:15px;font-weight:bold;"
        )
        hdr_lay.addWidget(self._title_lbl, stretch=1)
        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        hdr_lay.addWidget(self._step_lbl)
        root.addWidget(hdr)

        # Progress dots
        dots_bar = QWidget()
        dots_bar.setFixedHeight(28)
        dots_bar.setStyleSheet(
            f"background:{_SURFACE};border-bottom:1px solid {_BORDER};"
        )
        dots_lay = QHBoxLayout(dots_bar)
        dots_lay.setContentsMargins(24, 0, 24, 0)
        dots_lay.setSpacing(0)
        self._dots: list[tuple[QLabel, QLabel]] = []
        for i, name in enumerate(["Intro", "Ollama", "Model", "Done"]):
            if i > 0:
                line = QFrame()
                line.setFixedHeight(2)
                line.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
                )
                line.setStyleSheet(f"background:{_BORDER};")
                dots_lay.addWidget(line)
            col = QVBoxLayout()
            col.setSpacing(2)
            dot = QLabel("●")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setStyleSheet(f"color:{_MUTED};font-size:9px;")
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
        nav.setStyleSheet(f"background:{_SURFACE};border-top:1px solid {_BORDER};")
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

        self._next_btn = QPushButton("Next  ->")
        self._next_btn.setObjectName("primary")
        self._next_btn.setFixedWidth(110)
        self._next_btn.clicked.connect(self._go_next)
        nav_lay.addWidget(self._next_btn)

        root.addWidget(nav)

    # ---- Page builders ----

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 16)
        lay.setSpacing(14)

        lay.addWidget(_body(
            "Ava is Samsara's AI assistant. It runs entirely on your machine "
            "— nothing leaves your computer."
        ))
        lay.addWidget(_body(
            "The main thing Ava does: <b>you don't have to memorise command phrases.</b> "
            "Say what you mean in plain language, and Ava figures out the right action."
        ))

        # Example table
        examples_frame = QWidget()
        examples_frame.setStyleSheet(
            f"background:{_SURFACE};border-radius:6px;border:1px solid {_BORDER};"
        )
        ex_lay = QVBoxLayout(examples_frame)
        ex_lay.setContentsMargins(16, 12, 16, 12)
        ex_lay.setSpacing(6)

        ex_hdr = QHBoxLayout()
        ex_hdr.addWidget(_small_bold("Instead of remembering...", _TEXT_SEC), stretch=1)
        ex_hdr.addWidget(_small_bold("You can say...", _ACCENT), stretch=1)
        ex_lay.addLayout(ex_hdr)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color:{_BORDER};")
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
                f"color:{_TEXT_SEC};font-size:11px;font-family:'Consolas',monospace;"
            )
            rgt = QLabel(natural)
            rgt.setStyleSheet(f"color:{_TEXT_PRI};font-size:11px;font-style:italic;")
            row.addWidget(lft, stretch=1)
            row.addWidget(rgt, stretch=1)
            ex_lay.addLayout(row)

        lay.addWidget(examples_frame)

        lay.addWidget(_body(
            "Ava uses a small AI model that runs locally via Ollama. "
            "The next steps get that set up."
        ))

        lay.addStretch()
        return page

    def _build_ollama_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 16)
        lay.setSpacing(14)

        lay.addWidget(_body(
            "Ollama is the runtime that lets AI models run locally. "
            "It installs as a background service and Samsara connects to it automatically."
        ))

        # Status card
        status_card = QWidget()
        status_card.setStyleSheet(
            f"background:{_SURFACE};border-radius:6px;border:1px solid {_BORDER};"
        )
        sc_lay = QHBoxLayout(status_card)
        sc_lay.setContentsMargins(16, 14, 16, 14)
        sc_lay.setSpacing(12)

        self._ollama_dot = QLabel("●")
        self._ollama_dot.setStyleSheet(f"color:{_MUTED};font-size:16px;")
        sc_lay.addWidget(self._ollama_dot)

        self._ollama_status_lbl = QLabel("Checking...")
        self._ollama_status_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:13px;")
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
            "Ollama is not running. To install it:"
        ))

        steps = [
            ("1.", f"Go to  {_OLLAMA_DOWNLOAD}  and download Ollama for Windows."),
            ("2.", "Run the installer — it starts Ollama automatically."),
            ("3.", 'Click "Check again" above once it\'s installed.'),
        ]
        for num, text in steps:
            step_row = QHBoxLayout()
            step_row.setSpacing(8)
            num_lbl = QLabel(num)
            num_lbl.setFixedWidth(20)
            num_lbl.setStyleSheet(f"color:{_ACCENT};font-weight:bold;font-size:13px;")
            step_row.addWidget(num_lbl)
            txt_lbl = QLabel(text)
            txt_lbl.setWordWrap(True)
            txt_lbl.setStyleSheet(f"color:{_TEXT_PRI};font-size:13px;")
            step_row.addWidget(txt_lbl, stretch=1)
            inst_lay.addLayout(step_row)

        note = QLabel(
            "Ollama only uses resources when Ava is actively answering — "
            "it idles silently in the background otherwise."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;font-style:italic;")
        inst_lay.addWidget(note)

        lay.addWidget(self._install_instructions)
        lay.addStretch()
        return page

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 16)
        lay.setSpacing(14)

        lay.addWidget(_body(
            "Ava needs a language model to understand you. "
            "Pick one below and click Pull — it downloads once and runs offline forever."
        ))

        # Model selector
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_row.addWidget(_label("Model:"))

        self._model_combo = QComboBox()
        self._model_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for name, size, desc in _MODELS:
            self._model_combo.addItem(f"{name}  ({size})", userData=name)
        self._model_combo.currentIndexChanged.connect(self._on_model_selected)
        model_row.addWidget(self._model_combo, stretch=1)
        lay.addLayout(model_row)

        # Description label
        self._model_desc = QLabel(_MODELS[0][2])
        self._model_desc.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:11px;font-style:italic;"
        )
        lay.addWidget(self._model_desc)

        # Pull controls
        pull_row = QHBoxLayout()
        pull_row.setSpacing(10)

        self._model_status_dot = QLabel("●")
        self._model_status_dot.setStyleSheet(f"color:{_MUTED};font-size:14px;")
        pull_row.addWidget(self._model_status_dot)

        self._model_status_lbl = QLabel("Not checked yet")
        self._model_status_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
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
            f"color:{_TEXT_SEC};font-size:11px;"
            f"font-family:'Consolas',monospace;"
            f"background:{_SURFACE};border-radius:4px;padding:6px 8px;"
        )
        self._pull_progress.setVisible(False)
        lay.addWidget(self._pull_progress)

        # Already have a model note
        self._installed_note = QLabel("")
        self._installed_note.setWordWrap(True)
        self._installed_note.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:11px;font-style:italic;"
        )
        lay.addWidget(self._installed_note)

        lay.addStretch()
        return page

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 32, 32, 16)
        lay.setSpacing(14)

        title = QLabel("Ava is ready.")
        title.setStyleSheet(
            f"color:{_SUCCESS};font-size:18px;font-weight:bold;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        self._done_summary = QLabel("")
        self._done_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._done_summary.setWordWrap(True)
        self._done_summary.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:12px;"
        )
        lay.addWidget(self._done_summary)

        lay.addSpacing(8)

        # How to use section
        usage_frame = QWidget()
        usage_frame.setStyleSheet(
            f"background:{_SURFACE};border-radius:6px;border:1px solid {_BORDER};"
        )
        uf_lay = QVBoxLayout(usage_frame)
        uf_lay.setContentsMargins(16, 12, 16, 12)
        uf_lay.setSpacing(6)

        uf_lay.addWidget(_small_bold("How to use Ava", _ACCENT))

        wake = self._app.config.get("wake_word_config", {}).get("phrase", "Jarvis")
        usage_lines = [
            f'Say  "{wake.title()}, hey Ava"  then speak naturally.',
            f'Say  "{wake.title()}, Ava local"  to force a local model response.',
            "Ava reads your active command list and tries to route your request.",
            'Say "Ava cancel" at any time to stop a running response.',
        ]
        for line in usage_lines:
            row = QHBoxLayout()
            row.setSpacing(8)
            bullet = QLabel("·")
            bullet.setFixedWidth(12)
            bullet.setStyleSheet(f"color:{_ACCENT};font-size:16px;")
            row.addWidget(bullet)
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{_TEXT_PRI};font-size:12px;")
            row.addWidget(lbl, stretch=1)
            uf_lay.addLayout(row)

        lay.addWidget(usage_frame)

        # Enable pack button (shown if AI pack is disabled)
        self._enable_pack_btn = QPushButton("Enable AI commands pack")
        self._enable_pack_btn.setObjectName("primary")
        self._enable_pack_btn.setFixedWidth(220)
        self._enable_pack_btn.clicked.connect(self._enable_ai_pack)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._enable_pack_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        lay.addStretch()
        return page

    # ----------------------------------------------------------------
    # Step navigation
    # ----------------------------------------------------------------

    def _go_to(self, step: int):
        self._current_step = step
        self._stack.setCurrentIndex(step)

        self._title_lbl.setText(self._STEP_TITLES[step])
        self._step_lbl.setText(f"Step {step + 1} of 4")

        for i, (dot, lbl) in enumerate(self._dots):
            if i < step:
                dot.setStyleSheet(f"color:{_SUCCESS};font-size:9px;")
                lbl.setStyleSheet(f"color:{_SUCCESS};font-size:10px;")
            elif i == step:
                dot.setStyleSheet(f"color:{_ACCENT};font-size:9px;")
                lbl.setStyleSheet(
                    f"color:{_ACCENT};font-size:10px;font-weight:bold;"
                )
            else:
                dot.setStyleSheet(f"color:{_MUTED};font-size:9px;")
                lbl.setStyleSheet(f"color:{_MUTED};font-size:10px;")

        self._back_btn.setVisible(step > 0)

        if step == self._STEP_DONE:
            self._skip_btn.hide()
            self._next_btn.setText("Finish")
            self._next_btn.setEnabled(True)
        else:
            self._skip_btn.show()
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

    def _go_next(self):
        if self._current_step == self._STEP_DONE:
            self._finish()
            return
        if self._current_step < self._STEP_DONE:
            self._go_to(self._current_step + 1)

    def _go_back(self):
        if self._current_step > 0:
            self._go_to(self._current_step - 1)

    def _skip_step(self):
        if self._current_step < self._STEP_DONE:
            self._go_to(self._current_step + 1)

    # ----------------------------------------------------------------
    # Ollama step
    # ----------------------------------------------------------------

    def _check_ollama(self):
        self._ollama_dot.setStyleSheet(f"color:{_MUTED};font-size:16px;")
        self._ollama_status_lbl.setText("Checking...")
        self._ollama_check_btn.setEnabled(False)

        def _run():
            running, models = _ping_ollama()
            self._ollama_status_sig.emit(running, models)

        threading.Thread(target=_run, daemon=True, name="ava-ping").start()

    def _on_ollama_status(self, running: bool, models: list):
        self._ollama_check_btn.setEnabled(True)
        self._installed_models = models

        if running:
            self._ollama_dot.setStyleSheet(f"color:{_SUCCESS};font-size:16px;")
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
            self._ollama_dot.setStyleSheet(f"color:{_ERROR};font-size:16px;")
            self._ollama_status_lbl.setText("Ollama is not running")
            self._install_instructions.setVisible(True)

    # ----------------------------------------------------------------
    # Model step
    # ----------------------------------------------------------------

    def _on_model_selected(self, index: int):
        if 0 <= index < len(_MODELS):
            self._selected_model_name = _MODELS[index][0]
            self._model_desc.setText(_MODELS[index][2])
        self._refresh_model_status()

    def _check_installed_models(self):
        self._model_status_dot.setStyleSheet(f"color:{_MUTED};font-size:14px;")
        self._model_status_lbl.setText("Checking Ollama...")
        self._pull_btn.setEnabled(False)
        self._next_btn.setEnabled(False)

        def _run():
            running, models = _ping_ollama()
            self._ollama_status_sig.emit(running, models)   # reuse signal
            # also fire model refresh via same signal path
            self._pull_line_sig.emit("")  # sentinel to trigger _refresh_model_status

        threading.Thread(
            target=_run, daemon=True, name="ava-model-check"
        ).start()

    def _refresh_model_status(self):
        sel = self._selected_model_name
        # A model is "installed" if any installed name starts with the base name
        base = sel.split(":")[0]
        matched = [m for m in self._installed_models if m.startswith(base)]

        if matched:
            self._model_status_dot.setStyleSheet(f"color:{_SUCCESS};font-size:14px;")
            self._model_status_lbl.setText(f"Installed: {matched[0]}")
            self._pull_btn.setEnabled(True)
            self._pull_btn.setText("Re-pull")
            self._next_btn.setEnabled(True)
        else:
            self._model_status_dot.setStyleSheet(f"color:{_WARNING};font-size:14px;")
            self._model_status_lbl.setText("Not installed — click Pull to download")
            self._pull_btn.setEnabled(True)
            self._pull_btn.setText("Pull model")

        # Show any installed models as a note
        if self._installed_models and not matched:
            others = ", ".join(self._installed_models[:4])
            if len(self._installed_models) > 4:
                others += f" +{len(self._installed_models) - 4} more"
            self._installed_note.setText(f"You already have: {others}")
            # Any installed model means Ava can work — enable Next
            self._next_btn.setEnabled(True)
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
        self._model_status_dot.setStyleSheet(f"color:{_ACCENT};font-size:14px;")
        self._model_status_lbl.setText(f"Downloading {model}...")

        def _run():
            success = False
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", model],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    ),
                )
                self._pull_proc = proc
                for raw_line in proc.stdout:
                    line = raw_line.strip()
                    if line:
                        self._pull_line_sig.emit(line)
                proc.wait()
                success = proc.returncode == 0
            except FileNotFoundError:
                self._pull_line_sig.emit(
                    "ollama not found on PATH — is Ollama installed?"
                )
            except Exception as exc:
                self._pull_line_sig.emit(f"Error: {exc}")
            finally:
                self._pull_proc = None
                self._pulling = False
            self._pull_done_sig.emit(success)

        threading.Thread(target=_run, daemon=True, name="ava-pull").start()

    def _on_pull_done(self, success: bool):
        self._pull_btn.setEnabled(True)
        self._pull_btn.setText("Pull model")
        if success:
            self._pull_progress.setText("Download complete.")
            self._model_status_dot.setStyleSheet(f"color:{_SUCCESS};font-size:14px;")
            self._model_status_lbl.setText(f"Installed: {self._selected_model_name}")
            self._next_btn.setEnabled(True)
            # Save model choice to config
            self._save_model_choice(self._selected_model_name)
        else:
            self._model_status_dot.setStyleSheet(f"color:{_ERROR};font-size:14px;")
            self._model_status_lbl.setText("Download failed — see above for details")

    def _save_model_choice(self, model_name: str):
        try:
            cfg = dict(self._app.config.get("ollama", {}))
            cfg["model"] = model_name
            self._app.update_config({"ollama": cfg}, save=True)
        except Exception:
            pass

    # ----------------------------------------------------------------
    # Done step
    # ----------------------------------------------------------------

    def _build_done_summary(self):
        cfg_model = self._app.config.get("ollama", {}).get("model", "llama3")
        base = cfg_model.split(":")[0]
        matched = [m for m in self._installed_models if m.startswith(base)]
        model_line = matched[0] if matched else cfg_model

        ai_enabled = self._app.config.get(
            "command_packs", {}
        ).get("ai", True)

        parts = [
            f"Ollama:   running",
            f"Model:    {model_line}",
            f"AI pack:  {'enabled' if ai_enabled else 'disabled (see button below)'}",
        ]
        self._done_summary.setText("\n".join(parts))
        self._enable_pack_btn.setVisible(not ai_enabled)

    def _enable_ai_pack(self):
        try:
            packs = dict(self._app.config.get("command_packs", {}))
            packs["ai"] = True
            self._app.update_config({"command_packs": packs}, save=True)
            self._enable_pack_btn.setText("AI pack enabled — restart to activate")
            self._enable_pack_btn.setEnabled(False)
        except Exception as exc:
            self._enable_pack_btn.setText(f"Error: {exc}")

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
        if self._pull_proc is not None:
            try:
                self._pull_proc.terminate()
            except Exception:
                pass
        e.accept()


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------

def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setStyleSheet(f"color:{_TEXT_PRI};font-size:13px;line-height:1.5;")
    return lbl


def _small_bold(text: str, color: str = _TEXT_PRI) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color};font-size:11px;font-weight:bold;letter-spacing:0.5px;"
    )
    return lbl
