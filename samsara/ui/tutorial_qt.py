"""
Interactive hands-on tutorial for Samsara.

Architecture mirrors first_run_wizard_qt.py exactly:
  - Same _SS stylesheet (dark #0A0A0B, teal #5EEAD4 accent, Segoe UI)
  - _WizardWindow-style class: header bar with step dots, page title,
    page stack, Back/Next nav
  - Same _padded() helper and _STEPS pattern
  - Qt-thread safety: TutorialWindow MUST be created on the Qt thread.
    Use app.show_tutorial() (calls _schedule_ui) — never construct directly
    from an audio/worker thread.

Steps (Ava step is omitted when Ava is not configured):
  0  Welcome     — passive: "takes two minutes" + Let's go
  1  Dictation   — speak → text lands in box → green check
  2  Command     — say "scroll down" → area scrolls → green check
  3  Numbers     — say "show numbers" then "click N" → click detected
  4  Ava         — hold Ava key, ask anything → Ava responds (optional)
  5  Done        — checklist recap, all four items ticked

Interaction detection hooks (registered on app, removed on window close):
  app._tutorial_hooks['dictation']  one-shot cb(text) after dictation
  app._tutorial_hooks['command']    one-shot cb(cmd_name) after any command
  app._tutorial_hooks['ava']        one-shot cb() after Ava responds
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Stylesheet — identical palette to first_run_wizard_qt.py
# ---------------------------------------------------------------------------

_SS = """
QMainWindow, QWidget {
    background-color: #0A0A0B;
    color: #E8E8EA;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
}
QLabel  { color: #E8E8EA; }
QTextEdit {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 8px;
    color: #E8E8EA;
    font-size: 15px;
}
QScrollArea { border: none; background: transparent; }
QPushButton {
    background-color: #5EEAD4;
    color: #0A0A0B;
    border: none;
    border-radius: 6px;
    padding: 10px 24px;
    font-weight: 600;
    font-size: 14px;
}
QPushButton:hover   { background-color: #4DD8C2; }
QPushButton:pressed { background-color: #3DC8B0; }
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
QPushButton[class="primary"]:hover   { background-color: #4DD8C2; color: #0A0A0B; }
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
QPushButton[class="danger"] {
    background-color: transparent;
    color: #8A8A92;
    border: none;
    font-size: 12px;
    padding: 4px 10px;
}
QPushButton[class="danger"]:hover { color: #FF8080; }
"""

# How long to wait before showing the "need a hand?" hint and skip link
_HINT_DELAY_MS = 20_000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def show_tutorial(app) -> None:
    """Create and display the tutorial window on the Qt thread.

    Call this from dictation.py via _schedule_ui so the window is
    always constructed on the correct thread.
    """
    win = TutorialWindow(app)
    win.show()
    qt_app = QApplication.instance()
    if qt_app:
        screen = qt_app.primaryScreen().availableGeometry()
        win.move(
            screen.center().x() - win.width() // 2,
            screen.center().y() - win.height() // 2,
        )


# ---------------------------------------------------------------------------
# Tutorial window
# ---------------------------------------------------------------------------

class TutorialWindow(QMainWindow):
    """Interactive post-wizard tutorial.

    Created on Qt thread. Registers lightweight one-shot hooks on `app`
    for dictation, command, and Ava detection; removes them on close.
    """

    # Signal for cross-thread UI updates (audio/worker → Qt thread)
    _step_signal = Signal(str, str)   # (event_name, payload)

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._step = 0
        self._completed: set[int] = set()
        self._hint_timer: QTimer | None = None

        self._step_signal.connect(self._on_step_signal)

        # Decide which steps to include (Ava is optional)
        self._ava_enabled = self._check_ava_enabled()
        self._build_step_list()

        self.setWindowTitle("Samsara Tutorial")
        self.setFixedSize(620, 540)
        self.setStyleSheet(_SS)
        # Stay on top so the user can still use the tutorial while interacting
        # with other windows; WindowStaysOnTopHint avoids stealing focus.
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header bar: step counter + dots --------------------------------
        header = QWidget()
        header.setStyleSheet("background:#111114;")
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)

        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet("color:#8A8A92;font-size:12px;")
        hl.addWidget(self._step_lbl)
        hl.addStretch()

        self._dots: list[QLabel] = []
        for _ in self._steps:
            dot = QLabel("●")
            dot.setStyleSheet("font-size:10px;")
            self._dots.append(dot)
            hl.addWidget(dot)

        root.addWidget(header)

        sep_top = QFrame()
        sep_top.setFrameShape(QFrame.Shape.HLine)
        sep_top.setStyleSheet("background:rgba(255,255,255,0.06);max-height:1px;")
        root.addWidget(sep_top)

        # ---- Page title -----------------------------------------------------
        title_bar = QWidget()
        title_bar.setFixedHeight(60)
        tbl = QVBoxLayout(title_bar)
        tbl.setContentsMargins(28, 14, 28, 8)
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            "color:#E8E8EA;font-size:20px;font-weight:bold;"
        )
        tbl.addWidget(self._title_lbl)
        root.addWidget(title_bar)

        # ---- Page stack -----------------------------------------------------
        self._stack = QWidget()
        self._stack_layout = QVBoxLayout(self._stack)
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack, stretch=1)

        # Build pages
        self._dictation_box: QTextEdit | None = None
        self._scroll_area:   QScrollArea | None = None
        self._pages = [self._build_page(key) for key, _ in self._steps]
        self._current_page: QWidget | None = None

        sep_bot = QFrame()
        sep_bot.setFrameShape(QFrame.Shape.HLine)
        sep_bot.setStyleSheet("background:rgba(255,255,255,0.06);max-height:1px;")
        root.addWidget(sep_bot)

        # ---- Nav bar --------------------------------------------------------
        nav = QWidget()
        nav.setFixedHeight(64)
        nav.setStyleSheet("background:#111114;")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(28, 12, 28, 12)

        self._skip_all_btn = QPushButton("Skip tutorial")
        self._skip_all_btn.setProperty("class", "danger")
        self._skip_all_btn.style().unpolish(self._skip_all_btn)
        self._skip_all_btn.style().polish(self._skip_all_btn)
        self._skip_all_btn.clicked.connect(self._skip_tutorial)
        nl.addWidget(self._skip_all_btn)

        nl.addStretch()

        self._skip_step_btn = QPushButton("Skip this step")
        self._skip_step_btn.setProperty("class", "secondary")
        self._skip_step_btn.style().unpolish(self._skip_step_btn)
        self._skip_step_btn.style().polish(self._skip_step_btn)
        self._skip_step_btn.setFixedWidth(120)
        self._skip_step_btn.clicked.connect(self._skip_step)
        nl.addWidget(self._skip_step_btn)

        self._next_btn = QPushButton("Next")
        self._next_btn.setProperty("class", "primary")
        self._next_btn.style().unpolish(self._next_btn)
        self._next_btn.style().polish(self._next_btn)
        self._next_btn.setFixedWidth(150)
        self._next_btn.clicked.connect(self._go_next)
        nl.addWidget(self._next_btn)

        root.addWidget(nav)

        self._show_step()

    # ------------------------------------------------------------------
    # Step configuration
    # ------------------------------------------------------------------

    def _check_ava_enabled(self) -> bool:
        cfg = self._app.config if hasattr(self._app, 'config') else {}
        ava = cfg.get('ava', {})
        if isinstance(ava, dict) and ava.get('enabled'):
            return True
        # Also enabled if ollama/cloud LLM configured
        cloud = cfg.get('cloud_llm', {})
        if isinstance(cloud, dict) and cloud.get('enabled'):
            return True
        return False

    def _build_step_list(self):
        base = [
            ("welcome",   "Welcome to Samsara"),
            ("dictation", "Talk → It Types"),
            ("command",   "Say a Command"),
            ("numbers",   "Click Without a Mouse"),
        ]
        if self._ava_enabled:
            base.append(("ava", "Ask Ava"))
        base.append(("done", "You're Ready"))
        self._steps: list[tuple[str, str]] = base

    # ------------------------------------------------------------------
    # Page builders
    # ------------------------------------------------------------------

    def _padded(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 16, 28, 16)
        lay.setSpacing(12)
        return w, lay

    def _instruction_box(self, text: str) -> QFrame:
        """Tinted instruction card used on interactive steps."""
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(94,234,212,0.18);}"
        )
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(16, 12, 16, 12)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#AAFAF0;font-size:13px;")
        fl.addWidget(lbl)
        return frame

    def _success_banner(self, text: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame{background:rgba(94,234,212,0.08);border-radius:8px;"
            "border:1px solid rgba(94,234,212,0.3);}"
        )
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(14, 10, 14, 10)
        fl.setSpacing(10)
        check = QLabel("✓")
        check.setStyleSheet("color:#5EEAD4;font-size:22px;font-weight:bold;")
        fl.addWidget(check, alignment=Qt.AlignmentFlag.AlignVCenter)
        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setStyleSheet("color:#AAFAF0;font-size:13px;")
        fl.addWidget(msg, stretch=1)
        return frame

    def _build_page(self, key: str) -> QWidget:
        builders = {
            "welcome":   self._build_welcome,
            "dictation": self._build_dictation,
            "command":   self._build_command,
            "numbers":   self._build_numbers,
            "ava":       self._build_ava,
            "done":      self._build_done,
        }
        return builders[key]()

    def _build_welcome(self) -> QWidget:
        w, lay = self._padded()

        headline = QLabel(
            "Samsara lets you control your computer and type with your voice."
        )
        headline.setWordWrap(True)
        headline.setStyleSheet("color:#E8E8EA;font-size:16px;")
        lay.addWidget(headline)

        sub = QLabel(
            "Let's try each thing once — it takes about two minutes. "
            "Every step is skippable."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(8)

        things_frame = QFrame()
        things_frame.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(255,255,255,0.06);}"
        )
        tf = QVBoxLayout(things_frame)
        tf.setContentsMargins(20, 16, 20, 16)
        tf.setSpacing(10)
        for icon, label in [
            ("🎙", "Talk → text appears wherever you're typing"),
            ("⚡", "Say a command → it happens"),
            ("🔢", "Say numbers → click anything, hands-free"),
            ("✨", "Ask Ava → your on-device voice assistant"),
        ]:
            row = QHBoxLayout()
            i_lbl = QLabel(icon)
            i_lbl.setFixedWidth(28)
            i_lbl.setStyleSheet("font-size:16px;")
            row.addWidget(i_lbl)
            t_lbl = QLabel(label)
            t_lbl.setStyleSheet("color:#E8E8EA;font-size:13px;")
            row.addWidget(t_lbl)
            row.addStretch()
            tf.addLayout(row)
        lay.addWidget(things_frame)
        lay.addStretch()
        return w

    def _build_dictation(self) -> QWidget:
        w, lay = self._padded()

        hotkey = self._app.config.get('hotkey', 'ctrl+shift') if hasattr(self._app, 'config') else 'ctrl+shift'
        lay.addWidget(self._instruction_box(
            f"Hold  {hotkey.upper()}  and say anything — 'hello world' works fine. "
            f"Release to transcribe. Text will appear in the box below."
        ))

        self._dictation_box = QTextEdit()
        self._dictation_box.setPlaceholderText("Your words will appear here…")
        self._dictation_box.setReadOnly(False)
        self._dictation_box.setFixedHeight(80)
        self._dictation_box.setAccessibleName("Dictation result box")
        lay.addWidget(self._dictation_box)

        # Success banner (hidden until success)
        self._dict_success = self._success_banner(
            "That's dictation. Anything you say becomes text — in any app, "
            "not just here."
        )
        self._dict_success.setVisible(False)
        lay.addWidget(self._dict_success)

        # Hint label (shown after _HINT_DELAY_MS with no success)
        self._dict_hint = QLabel(
            f"💡 Hotkey not working? Make sure Samsara is running and {hotkey.upper()} "
            f"is your configured record key."
        )
        self._dict_hint.setWordWrap(True)
        self._dict_hint.setStyleSheet("color:#8A8A92;font-size:12px;")
        self._dict_hint.setVisible(False)
        lay.addWidget(self._dict_hint)

        lay.addStretch()
        return w

    def _build_command(self) -> QWidget:
        w, lay = self._padded()

        cmd_hotkey = self._app.config.get('command_hotkey', 'ctrl+alt+c') if hasattr(self._app, 'config') else 'ctrl+alt+c'
        wake = self._app.config.get('wake_word', 'jarvis') if hasattr(self._app, 'config') else 'jarvis'
        lay.addWidget(self._instruction_box(
            f"Say a voice command. Try one of:\n"
            f"  • Hold  {cmd_hotkey.upper()}  and say  \"scroll down\"\n"
            f"  • Say  \"{wake}, scroll down\"  (wake word mode)\n"
            f"  • Or any command you know — 'open settings', 'new tab', etc."
        ))

        # Scrollable demonstration area
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFixedHeight(120)
        self._scroll_area.setStyleSheet(
            "QScrollArea{background:#111114;border:1px solid rgba(255,255,255,0.08);"
            "border-radius:6px;}"
        )
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(12, 10, 12, 10)
        inner_lay.setSpacing(4)
        for i in range(1, 15):
            lbl = QLabel(f"Line {i} — scroll down to see more of this area")
            lbl.setStyleSheet("color:#4a4a56;font-size:12px;")
            inner_lay.addWidget(lbl)
        self._scroll_area.setWidget(inner)
        lay.addWidget(self._scroll_area)

        self._cmd_success = self._success_banner(
            "Commands do things. There are 400+ of them — scroll, open apps, "
            "manage windows, type shortcuts, and more."
        )
        self._cmd_success.setVisible(False)
        lay.addWidget(self._cmd_success)

        self._cmd_hint = QLabel(
            "💡 Try saying 'scroll down', 'open settings', or 'new tab'."
        )
        self._cmd_hint.setWordWrap(True)
        self._cmd_hint.setStyleSheet("color:#8A8A92;font-size:12px;")
        self._cmd_hint.setVisible(False)
        lay.addWidget(self._cmd_hint)

        lay.addStretch()
        return w

    def _build_numbers(self) -> QWidget:
        w, lay = self._padded()

        wake = self._app.config.get('wake_word', 'jarvis') if hasattr(self._app, 'config') else 'jarvis'
        lay.addWidget(self._instruction_box(
            f"Say  \"show numbers\"  — numbered labels appear on every clickable element.\n"
            f"Then say  \"click [number]\"  to click one of the buttons below."
        ))

        # Clickable dummy buttons — the show-numbers overlay will label them
        btn_row = QHBoxLayout()
        self._num_buttons: list[QPushButton] = []
        for label in ("Button A", "Button B", "Button C"):
            btn = QPushButton(label)
            btn.setProperty("class", "secondary")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.setAccessibleName(label)
            btn.setMinimumHeight(44)
            btn.clicked.connect(self._on_numbers_click)
            self._num_buttons.append(btn)
            btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self._num_success = self._success_banner(
            "That's how you click anything, anywhere — hands-free. "
            "Numbers appear on every link, button, and control on screen."
        )
        self._num_success.setVisible(False)
        lay.addWidget(self._num_success)

        self._num_hint = QLabel(
            f"💡 Say 'show numbers' first, wait for the overlay, then say 'click 1' "
            f"(or whichever number appears on a button below)."
        )
        self._num_hint.setWordWrap(True)
        self._num_hint.setStyleSheet("color:#8A8A92;font-size:12px;")
        self._num_hint.setVisible(False)
        lay.addWidget(self._num_hint)

        lay.addStretch()
        return w

    def _build_ava(self) -> QWidget:
        w, lay = self._padded()

        cfg = self._app.config if hasattr(self._app, 'config') else {}
        ava_key = cfg.get('ava_hotkey', 'right alt')

        lay.addWidget(self._instruction_box(
            f"Hold  {ava_key.upper()}  and ask Ava anything.\n"
            f"Try: \"Ava, what can you do?\" or \"Ava, summarize what I've been working on.\""
        ))

        self._ava_status = QLabel("Waiting for Ava to respond…")
        self._ava_status.setStyleSheet("color:#8A8A92;font-size:13px;")
        lay.addWidget(self._ava_status)

        self._ava_success = self._success_banner(
            "Ava is your on-device voice assistant. Ask questions, ask her to do "
            "things, or just have a conversation."
        )
        self._ava_success.setVisible(False)
        lay.addWidget(self._ava_success)

        self._ava_hint = QLabel(
            "💡 Ava needs Ollama running locally, or a cloud LLM configured in Settings."
        )
        self._ava_hint.setWordWrap(True)
        self._ava_hint.setStyleSheet("color:#8A8A92;font-size:12px;")
        self._ava_hint.setVisible(False)
        lay.addWidget(self._ava_hint)

        lay.addStretch()
        return w

    def _build_done(self) -> QWidget:
        w, lay = self._padded()

        done_lbl = QLabel("You know the four things that matter.")
        done_lbl.setStyleSheet("color:#5EEAD4;font-size:15px;font-weight:600;")
        lay.addWidget(done_lbl)
        lay.addSpacing(4)

        # Build checklist based on which steps were in the tutorial
        checklist_items = [
            (1, "✓  Dictated text — talk, it types"),
            (2, "✓  Ran a command — voice controls your computer"),
            (3, "✓  Clicked by voice — numbers on everything"),
        ]
        if self._ava_enabled:
            checklist_items.append((4, "✓  Talked to Ava — your on-device assistant"))

        card = QFrame()
        card.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(94,234,212,0.18);}"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        cl.setSpacing(8)

        for step_idx, text in checklist_items:
            row = QHBoxLayout()
            icon_lbl = QLabel("✓")
            completed = step_idx in self._completed
            icon_lbl.setStyleSheet(
                f"color:{'#5EEAD4' if completed else '#3a3a44'};"
                f"font-size:16px;font-weight:bold;"
            )
            icon_lbl.setFixedWidth(24)
            row.addWidget(icon_lbl)
            text_lbl = QLabel(text[2:] if text.startswith("✓  ") else text)
            text_lbl.setStyleSheet(
                f"color:{'#E8E8EA' if completed else '#4a4a56'};"
                f"font-size:13px;"
            )
            row.addWidget(text_lbl)
            row.addStretch()
            cl.addLayout(row)

        lay.addWidget(card)

        more_lbl = QLabel(
            'Say "what can I say" anytime for the full command list, '
            'or open the Command Reference from the tray menu.'
        )
        more_lbl.setWordWrap(True)
        more_lbl.setStyleSheet("color:#8A8A92;font-size:12px;")
        lay.addWidget(more_lbl)
        lay.addSpacing(12)

        # --- Advanced guides ---
        next_lbl = QLabel("Go deeper when you're ready:")
        next_lbl.setStyleSheet("color:#E8E8EA;font-size:13px;font-weight:600;")
        lay.addWidget(next_lbl)
        lay.addSpacing(4)

        _GUIDES = [
            ("Mic Setup Guide",
             "Fine-tune your microphone for the best accuracy.",
             "open_mic_setup_guide"),
            ("Ava Setup Guide",
             "Install Ollama and set up your on-device AI assistant.",
             "open_ava_guide"),
            ("Voice Training",
             "Teach Samsara your specific words and corrections.",
             "open_voice_training"),
        ]

        for title, desc, method in _GUIDES:
            guide_card = QFrame()
            guide_card.setStyleSheet(
                "QFrame{background:#111114;border-radius:8px;"
                "border:1px solid rgba(255,255,255,0.06);}"
                "QFrame:hover{border-color:rgba(94,234,212,0.25);}"
            )
            gc_lay = QHBoxLayout(guide_card)
            gc_lay.setContentsMargins(14, 10, 12, 10)
            gc_lay.setSpacing(10)

            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            title_lbl = QLabel(title)
            title_lbl.setStyleSheet("color:#E8E8EA;font-size:13px;font-weight:600;")
            desc_lbl = QLabel(desc)
            desc_lbl.setStyleSheet("color:#8A8A92;font-size:11px;")
            text_col.addWidget(title_lbl)
            text_col.addWidget(desc_lbl)
            gc_lay.addLayout(text_col, stretch=1)

            open_btn = QPushButton("Open →")
            open_btn.setProperty("class", "secondary")
            open_btn.style().unpolish(open_btn)
            open_btn.style().polish(open_btn)
            open_btn.setFixedWidth(76)
            open_btn.setFixedHeight(34)
            _m = method  # capture for lambda
            open_btn.clicked.connect(
                lambda checked=False, m=_m: getattr(self._app, m, lambda: None)()
            )
            gc_lay.addWidget(open_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

            lay.addWidget(guide_card)

        lay.addStretch()
        return w

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_step(self):
        # Cancel any running hint timer from the previous step
        self._cancel_hint_timer()

        # Swap page
        page = self._pages[self._step]
        if self._current_page is not None:
            self._stack_layout.removeWidget(self._current_page)
            self._current_page.hide()
        self._stack_layout.addWidget(page)
        page.show()
        self._current_page = page

        key, title = self._steps[self._step]
        self._step_lbl.setText(f"Step {self._step + 1} of {len(self._steps)}")
        self._title_lbl.setText(title)

        # Dots
        for i, dot in enumerate(self._dots):
            dot.setStyleSheet(
                "color:#5EEAD4;font-size:10px;" if i == self._step
                else "color:#444;font-size:10px;"
            )

        is_last    = self._step == len(self._steps) - 1
        is_welcome = key == "welcome"
        is_done    = key == "done"
        is_interactive = key in ("dictation", "command", "numbers", "ava")

        # Nav button states
        self._skip_step_btn.setVisible(is_interactive)
        self._next_btn.setVisible(True)

        if is_done:
            self._next_btn.setText("Start using Samsara")
            self._next_btn.setEnabled(True)
            self._skip_all_btn.setVisible(False)
        elif is_welcome:
            self._next_btn.setText("Let's go")
            self._next_btn.setEnabled(True)
        else:
            already_done = self._step in self._completed
            self._next_btn.setText("Next")
            self._next_btn.setEnabled(already_done)

        # Register hook for interactive steps
        if is_interactive and self._step not in self._completed:
            self._install_hook(key)
            self._start_hint_timer(key)

        # Focus the dictation box so the hotkey routes text to it
        if key == "dictation" and self._dictation_box:
            QTimer.singleShot(100, self, lambda: self._dictation_box.setFocus())

    def _go_next(self):
        key = self._steps[self._step][0]
        if key == "done":
            self._finish()
            return
        self._step += 1
        self._show_step()

    def _skip_step(self):
        """Skip the current interactive step without marking it complete."""
        key = self._steps[self._step][0]
        self._remove_hook(key)
        self._cancel_hint_timer()
        self._step += 1
        self._show_step()

    def _skip_tutorial(self):
        """Skip the entire tutorial and close."""
        self._remove_all_hooks()
        self._cancel_hint_timer()
        self._mark_tutorial_complete()
        self.close()

    def _finish(self):
        self._remove_all_hooks()
        self._cancel_hint_timer()
        self._mark_tutorial_complete()
        self.close()

    def _mark_tutorial_complete(self):
        if not hasattr(self._app, 'config'):
            return
        self._app.config['tutorial_complete'] = True
        if hasattr(self._app, 'persist_config'):
            try:
                self._app.persist_config()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Hint timer
    # ------------------------------------------------------------------

    def _start_hint_timer(self, key: str):
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(lambda: self._show_hint(key))
        self._hint_timer.start(_HINT_DELAY_MS)

    def _cancel_hint_timer(self):
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None

    def _show_hint(self, key: str):
        hints = {
            "dictation": getattr(self, '_dict_hint', None),
            "command":   getattr(self, '_cmd_hint',  None),
            "numbers":   getattr(self, '_num_hint',  None),
            "ava":       getattr(self, '_ava_hint',  None),
        }
        hint_widget = hints.get(key)
        if hint_widget:
            hint_widget.setVisible(True)
        # Make skip-step button more visible
        self._skip_step_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#5EEAD4;"
            "border:1px solid rgba(94,234,212,0.3);border-radius:6px;"
            "font-size:12px;padding:4px 10px;}"
        )

    # ------------------------------------------------------------------
    # Interaction hooks
    # ------------------------------------------------------------------

    def _install_hook(self, key: str):
        """Register a one-shot callback on the app for step `key`."""
        if not hasattr(self._app, '_tutorial_hooks'):
            self._app._tutorial_hooks = {}

        if key == "dictation":
            def _cb(text: str):
                self._step_signal.emit("dictation_done", text or "")
            self._app._tutorial_hooks['dictation'] = _cb

        elif key == "command":
            def _cb(cmd_name: str):
                self._step_signal.emit("command_done", cmd_name or "")
            self._app._tutorial_hooks['command'] = _cb

        elif key == "ava":
            def _cb():
                self._step_signal.emit("ava_done", "")
            self._app._tutorial_hooks['ava'] = _cb
        # "numbers" success is detected locally via button clicks — no app hook needed

    def _remove_hook(self, key: str):
        if hasattr(self._app, '_tutorial_hooks'):
            self._app._tutorial_hooks.pop(key, None)

    def _remove_all_hooks(self):
        if hasattr(self._app, '_tutorial_hooks'):
            for key in ('dictation', 'command', 'ava'):
                self._app._tutorial_hooks.pop(key, None)

    # ------------------------------------------------------------------
    # Step completion
    # ------------------------------------------------------------------

    def _on_step_signal(self, event: str, payload: str):
        """Handle success signals delivered from worker threads via Signal."""
        if event == "dictation_done":
            self._complete_dictation(payload)
        elif event == "command_done":
            self._complete_command(payload)
        elif event == "ava_done":
            self._complete_ava()

    def _complete_dictation(self, text: str):
        if 1 in self._completed:
            return
        self._completed.add(1)
        self._cancel_hint_timer()
        if self._dictation_box and text:
            self._dictation_box.setPlainText(text)
        if hasattr(self, '_dict_success'):
            self._dict_success.setVisible(True)
        if hasattr(self, '_dict_hint'):
            self._dict_hint.setVisible(False)
        self._next_btn.setEnabled(True)

    def _complete_command(self, cmd_name: str):
        if 2 in self._completed:
            return
        self._completed.add(2)
        self._cancel_hint_timer()
        # Scroll the demo area a bit to make it visible
        if self._scroll_area:
            sb = self._scroll_area.verticalScrollBar()
            sb.setValue(sb.value() + 60)
        if hasattr(self, '_cmd_success'):
            self._cmd_success.setVisible(True)
        if hasattr(self, '_cmd_hint'):
            self._cmd_hint.setVisible(False)
        self._next_btn.setEnabled(True)

    def _on_numbers_click(self):
        """Called when any tutorial button is clicked (by voice or mouse)."""
        # Find step index for "numbers"
        for i, (key, _) in enumerate(self._steps):
            if key == "numbers":
                numbers_step = i
                break
        else:
            return
        if numbers_step in self._completed:
            return
        self._completed.add(numbers_step)
        self._cancel_hint_timer()
        if hasattr(self, '_num_success'):
            self._num_success.setVisible(True)
        if hasattr(self, '_num_hint'):
            self._num_hint.setVisible(False)
        # Disable the buttons so they can't be clicked twice
        for btn in self._num_buttons:
            btn.setEnabled(False)
        self._next_btn.setEnabled(True)

    def _complete_ava(self):
        # Find Ava step index
        for i, (key, _) in enumerate(self._steps):
            if key == "ava":
                ava_step = i
                break
        else:
            return
        if ava_step in self._completed:
            return
        self._completed.add(ava_step)
        self._cancel_hint_timer()
        if hasattr(self, '_ava_status'):
            self._ava_status.setVisible(False)
        if hasattr(self, '_ava_success'):
            self._ava_success.setVisible(True)
        if hasattr(self, '_ava_hint'):
            self._ava_hint.setVisible(False)
        self._next_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._remove_all_hooks()
        self._cancel_hint_timer()
        event.accept()
