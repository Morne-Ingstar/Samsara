"""
PySide6 settings window for Samsara.

Runs on its own daemon thread; the main thread belongs to Tkinter.
QApplication.exec() blocks — this is the same pattern as pywebview in task_overlay.py.
"""

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QStackedWidget, QScrollArea,
    QLabel, QComboBox, QCheckBox, QPushButton, QFrame,
    QDoubleSpinBox, QSpinBox, QLineEdit, QSlider,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QMessageBox, QFormLayout, QGridLayout,
)


# ---------------------------------------------------------------------------
# Hotkey capture helpers
# ---------------------------------------------------------------------------

# Sort modifiers before regular keys
_MOD_ORDER = {'ctrl': 0, 'shift': 1, 'alt': 2, 'altgr': 3, 'win': 4}

_MODIFIER_KEYS = {
    Qt.Key.Key_Control: 'ctrl',
    Qt.Key.Key_Shift:   'shift',
    Qt.Key.Key_Alt:     'alt',
    Qt.Key.Key_Meta:    'win',
    Qt.Key.Key_AltGr:   'altgr',
}

_SPECIAL_KEYS = {
    Qt.Key.Key_Escape:    'escape',
    Qt.Key.Key_Tab:       'tab',
    Qt.Key.Key_Return:    'enter',
    Qt.Key.Key_Enter:     'enter',
    Qt.Key.Key_Backspace: 'backspace',
    Qt.Key.Key_Delete:    'delete',
    Qt.Key.Key_Insert:    'insert',
    Qt.Key.Key_Home:      'home',
    Qt.Key.Key_End:       'end',
    Qt.Key.Key_PageUp:    'page up',
    Qt.Key.Key_PageDown:  'page down',
    Qt.Key.Key_Left:      'left',
    Qt.Key.Key_Right:     'right',
    Qt.Key.Key_Up:        'up',
    Qt.Key.Key_Down:      'down',
    Qt.Key.Key_Space:     'space',
    Qt.Key.Key_CapsLock:  'capslock',
    Qt.Key.Key_F1:        'f1',
    Qt.Key.Key_F2:        'f2',
    Qt.Key.Key_F3:        'f3',
    Qt.Key.Key_F4:        'f4',
    Qt.Key.Key_F5:        'f5',
    Qt.Key.Key_F6:        'f6',
    Qt.Key.Key_F7:        'f7',
    Qt.Key.Key_F8:        'f8',
    Qt.Key.Key_F9:        'f9',
    Qt.Key.Key_F10:       'f10',
    Qt.Key.Key_F11:       'f11',
    Qt.Key.Key_F12:       'f12',
    Qt.Key.Key_F13:       'f13',
    Qt.Key.Key_NumLock:   'num lock',
    Qt.Key.Key_ScrollLock: 'scroll lock',
    Qt.Key.Key_Pause:     'pause',
    Qt.Key.Key_Print:     'print screen',
}


# ---------------------------------------------------------------------------
# Commands tab constants
# ---------------------------------------------------------------------------

_CMD_BUTTON_OPTIONS = {
    'Mouse 4 (default)': 'mouse4',
    'Mouse 5':           'mouse5',
    'Right Ctrl':        'rctrl',
    'Left Ctrl':         'lctrl',
    'Right Alt':         'ralt',
    'Left Alt':          'lalt',
    'Right Shift':       'rshift',
    'Left Shift':        'lshift',
    **{f'F{n}': f'f{n}' for n in range(13, 25)},
}
_CMD_BUTTON_KEY_TO_LABEL = {v: k for k, v in _CMD_BUTTON_OPTIONS.items()}

# ---------------------------------------------------------------------------
# Cloud LLM / Ava tab constants
# ---------------------------------------------------------------------------

_PROVIDERS = [
    ("DeepSeek (default)", "deepseek"),
    ("OpenAI",             "openai"),
    ("Anthropic",          "anthropic"),
]
_PROVIDER_DISPLAY  = [p[0] for p in _PROVIDERS]
_DISPLAY_TO_CODE   = {p[0]: p[1] for p in _PROVIDERS}
_CODE_TO_DISPLAY   = {p[1]: p[0] for p in _PROVIDERS}

_DEFAULT_MODELS = {
    "deepseek":  "deepseek-chat",
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}

_PROVIDER_INFO = {
    "deepseek":  "deepseek-chat — Best value. Cheapest per token, strong reasoning. Recommended for most users.",
    "openai":    "gpt-4o-mini — Fast and widely supported. Good for general tasks and tool use.",
    "anthropic": "claude-sonnet-4 — Best reasoning and instruction following. Higher cost per token.",
}


def _key_name(key: int) -> str | None:
    """Convert a Qt key integer to a keyboard-library-compatible name."""
    if key in _MODIFIER_KEYS:
        return _MODIFIER_KEYS[key]
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    # Printable ASCII (letters, digits, punctuation)
    if 0x20 <= key <= 0x7E:
        return chr(key).lower()
    return None


def _combo_str(held: set) -> str:
    return '+'.join(sorted(held, key=lambda k: (_MOD_ORDER.get(k, 99), k)))


# ---------------------------------------------------------------------------
# Hotkey capture button
# ---------------------------------------------------------------------------

class _HotkeyButton(QPushButton):
    """Shows the current hotkey combo; captures a new one when clicked."""

    _IDLE = (
        "QPushButton {"
        " background-color: #16161A;"
        " border: 1px solid rgba(255,255,255,0.14);"
        " border-radius: 6px;"
        " color: #E8E8EA;"
        " font-size: 12px;"
        " font-family: 'Consolas', 'Courier New', monospace;"
        " padding: 6px 14px;"
        "}"
        "QPushButton:hover {"
        " background-color: #1E1E24;"
        "}"
    )
    _CAPTURING = (
        "QPushButton {"
        " background-color: rgba(94,234,212,0.08);"
        " border: 1px solid #5EEAD4;"
        " border-radius: 6px;"
        " color: #5EEAD4;"
        " font-size: 12px;"
        " font-family: 'Consolas', 'Courier New', monospace;"
        " padding: 6px 14px;"
        "}"
    )

    def __init__(self, combo: str):
        super().__init__(combo or "—")
        self._combo = combo
        self._capturing = False
        self._held: set[str] = set()
        self.setMinimumWidth(180)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(self._IDLE)
        self.clicked.connect(self._start_capture)

    @property
    def combo(self) -> str:
        return self._combo

    def _start_capture(self):
        self._capturing = True
        self._held = set()
        self.setText("Press keys...")
        self.setStyleSheet(self._CAPTURING)
        self.setFocus()

    def _finish_capture(self):
        self._capturing = False
        if self._held:
            self._combo = _combo_str(self._held)
        self.setText(self._combo or "—")
        self.setStyleSheet(self._IDLE)

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return
        name = _key_name(event.key())
        if name:
            self._held.add(name)
            self.setText(_combo_str(self._held) or "Press keys...")
        event.accept()

    def keyReleaseEvent(self, event):
        if not self._capturing:
            super().keyReleaseEvent(event)
            return
        # Finalize on first key release (same behaviour as Tkinter version)
        self._finish_capture()
        event.accept()

    def focusOutEvent(self, event):
        if self._capturing:
            self._finish_capture()
        super().focusOutEvent(event)


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0A0A0B;
    color: #E8E8EA;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
}
QListWidget {
    background-color: #111114;
    border-right: 1px solid rgba(255,255,255,0.08);
    color: #8A8A92;
    font-size: 14px;
    padding: 8px 0;
    outline: none;
}
QListWidget::item {
    padding: 10px 20px;
    border: none;
}
QListWidget::item:selected {
    background-color: rgba(94, 234, 212, 0.12);
    color: #5EEAD4;
    border-left: 2px solid #5EEAD4;
}
QListWidget::item:hover {
    background-color: rgba(255,255,255,0.03);
}
QLabel {
    color: #E8E8EA;
}
QLabel[class="description"] {
    color: #8A8A92;
    font-size: 12px;
}
QLabel[class="section-title"] {
    color: #5EEAD4;
    font-size: 16px;
    font-weight: bold;
}
QComboBox {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 8px 12px;
    color: #E8E8EA;
    min-width: 200px;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
}
QComboBox QAbstractItemView {
    background-color: #16161A;
    color: #E8E8EA;
    selection-background-color: rgba(94, 234, 212, 0.2);
    border: 1px solid rgba(255,255,255,0.14);
}
QCheckBox {
    color: #E8E8EA;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.14);
    background-color: #16161A;
}
QCheckBox::indicator:checked {
    background-color: #5EEAD4;
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
QPushButton:hover {
    background-color: #4DD8C2;
}
QPushButton[class="secondary"] {
    background-color: transparent;
    color: #8A8A92;
    border: 1px solid rgba(255,255,255,0.14);
}
QPushButton[class="secondary"]:hover {
    background-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
}
QScrollArea {
    border: none;
    background-color: transparent;
}
QSpinBox, QDoubleSpinBox {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 6px 10px;
    color: #E8E8EA;
    min-width: 80px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background-color: transparent;
    border: none;
    width: 20px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    width: 0;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    width: 0;
}
QLineEdit {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 8px 12px;
    color: #E8E8EA;
    font-size: 13px;
}
QLineEdit:focus {
    border-color: rgba(94, 234, 212, 0.5);
}
QTableWidget {
    background-color: #111114;
    gridline-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    font-size: 13px;
    outline: none;
}
QTableWidget::item {
    padding: 5px 8px;
    border: none;
}
QTableWidget::item:selected {
    background-color: rgba(94,234,212,0.15);
    color: #E8E8EA;
}
QHeaderView::section {
    background-color: #16161A;
    color: #8A8A92;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    border-right: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
    font-weight: 600;
}
QDialog {
    background-color: #0A0A0B;
}
"""

_TAB_NAMES = [
    "General",
    "Hotkeys",
    "Commands",
    "Sounds",
    "TTS",
    "Ava / Cloud",
    "Alarms",
    "Health",
    "Advanced",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SettingsQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._thread = None

    def show(self):
        if self._window is not None:
            try:
                self._window.show()
                self._window.raise_()
                self._window.activateWindow()
                return
            except Exception:
                self._window = None

        self._thread = threading.Thread(
            target=self._create, daemon=True, name="settings-qt"
        )
        self._thread.start()

    def _create(self):
        qt_app = QApplication.instance()
        owns_app = qt_app is None
        if qt_app is None:
            qt_app = QApplication([])
        if owns_app:
            self._init_window()
            qt_app.exec()
            self._window = None
        else:
            QTimer.singleShot(0, qt_app, self._init_window)

    def _init_window(self):
        self._window = _SettingsWindow(self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _SettingsWindow(QMainWindow):
    # Emitted from worker threads to update the test-connection label safely
    _test_result = Signal(str, str)  # (message, css-color)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._widgets = {}
        self._test_result.connect(self._on_test_result)

        self.setWindowTitle("Samsara Settings")
        self.resize(920, 700)
        self.setMinimumSize(860, 600)
        self.setStyleSheet(STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Body: sidebar + stacked content
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        root.addWidget(body, stretch=1)

        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(164)
        self._sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for name in _TAB_NAMES:
            self._sidebar.addItem(QListWidgetItem(name))
        self._sidebar.setCurrentRow(0)
        body_layout.addWidget(self._sidebar)

        self._stack = QStackedWidget()
        body_layout.addWidget(self._stack, stretch=1)

        self._stack.addWidget(self._build_general_tab())    # 0  General
        self._stack.addWidget(self._build_hotkeys_tab())    # 1  Hotkeys
        self._stack.addWidget(self._build_commands_tab())   # 2  Commands
        self._stack.addWidget(self._build_sounds_tab())     # 3  Sounds
        self._stack.addWidget(self._build_tts_tab())         # 4  TTS
        self._stack.addWidget(self._build_ava_cloud_tab())  # 5  Ava / Cloud
        self._stack.addWidget(self._build_alarms_tab())     # 6  Alarms
        self._stack.addWidget(self._build_health_tab())     # 7  Health
        self._stack.addWidget(self._build_advanced_tab())   # 8  Advanced

        self._sidebar.currentRowChanged.connect(self._stack.setCurrentIndex)

        # Separator above button bar
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: rgba(255,255,255,0.08); max-height: 1px;")
        root.addWidget(sep)

        # Button bar
        btn_bar = QWidget()
        btn_bar.setFixedHeight(64)
        btn_bar.setStyleSheet("background-color: #0A0A0B;")
        btn_layout = QHBoxLayout(btn_bar)
        btn_layout.setContentsMargins(20, 12, 20, 12)
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setProperty("class", "secondary")
        cancel_btn.style().unpolish(cancel_btn)
        cancel_btn.style().polish(cancel_btn)
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.close)
        btn_layout.addWidget(cancel_btn)

        apply_btn = QPushButton("Apply && Close")
        apply_btn.setFixedWidth(140)
        apply_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #5EEAD4;"
            " color: #0A0A0B;"
            " border: none;"
            " border-radius: 6px;"
            " padding: 10px 24px;"
            " font-weight: 600;"
            " font-size: 14px;"
            "}"
            "QPushButton:hover { background-color: #4DD8C2; }"
        )
        apply_btn.clicked.connect(self._apply_and_close)
        btn_layout.addWidget(apply_btn)

        root.addWidget(btn_bar)

    # ------------------------------------------------------------------
    # Tab builders
    # ------------------------------------------------------------------

    def _build_placeholder(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("Coming soon — this tab is being migrated.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #8A8A92; font-size: 14px;")
        layout.addWidget(label)
        return w

    def _build_general_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        # Section: Microphone
        layout.addWidget(self._section_title("Microphone"))
        layout.addSpacing(4)

        mics = list(getattr(self.app, 'available_mics', None) or [])
        mic_names = [m['name'] for m in mics]
        current_mic_id = self.app.config.get('microphone')
        current_mic_name = mic_names[0] if mic_names else ""
        for m in mics:
            if m['id'] == current_mic_id:
                current_mic_name = m['name']
                break

        mic_combo = QComboBox()
        mic_combo.addItems(mic_names if mic_names else ["No microphones found"])
        if current_mic_name and current_mic_name in mic_names:
            mic_combo.setCurrentText(current_mic_name)
        self._widgets['mic_combo'] = mic_combo
        self._widgets['mic_names_to_id'] = {m['name']: m['id'] for m in mics}
        layout.addLayout(self._setting_row(
            "Microphone",
            "Audio input device used for speech recognition",
            mic_combo,
        ))
        layout.addSpacing(8)

        setup_btn = QPushButton("Run Mic Setup Guide...")
        setup_btn.setFixedWidth(190)
        setup_btn.clicked.connect(
            lambda: getattr(self.app, 'open_mic_setup_guide', lambda: None)()
        )
        layout.addWidget(setup_btn)
        layout.addSpacing(16)

        # Section: AI Model
        layout.addWidget(self._section_title("AI Model"))
        layout.addSpacing(4)

        model_options = [
            'tiny', 'tiny.en', 'base', 'base.en',
            'small', 'small.en', 'medium', 'medium.en', 'large-v3',
        ]
        current_model = self.app.config.get('model_size', 'base')
        model_combo = QComboBox()
        model_combo.addItems(model_options)
        if current_model in model_options:
            model_combo.setCurrentText(current_model)
        self._widgets['model_combo'] = model_combo
        layout.addLayout(self._setting_row(
            "Model Size",
            "Larger models are more accurate but slower. Restart required to apply.",
            model_combo,
        ))
        layout.addSpacing(12)

        from samsara.languages import LANGUAGES
        lang_name_to_code = {name: code for name, code in LANGUAGES}
        lang_code_to_name = {code: name for name, code in LANGUAGES}
        lang_names = [name for name, _ in LANGUAGES]
        current_lang_code = self.app.config.get('language', 'en')
        current_lang_display = lang_code_to_name.get(current_lang_code, 'English')
        lang_combo = QComboBox()
        lang_combo.addItems(lang_names)
        if current_lang_display in lang_names:
            lang_combo.setCurrentText(current_lang_display)
        self._widgets['lang_combo'] = lang_combo
        self._widgets['lang_name_to_code'] = lang_name_to_code
        layout.addLayout(self._setting_row(
            "Language",
            "Transcription language. Use multilingual models (no .en suffix) for non-English.",
            lang_combo,
        ))
        layout.addSpacing(16)

        # Section: Basic Options
        layout.addWidget(self._section_title("Basic Options"))
        layout.addSpacing(4)

        auto_paste = QCheckBox()
        auto_paste.setChecked(bool(self.app.config.get('auto_paste', True)))
        self._widgets['auto_paste'] = auto_paste
        layout.addLayout(self._setting_row(
            "Auto-paste",
            "Automatically paste transcribed text into the focused application",
            auto_paste,
        ))
        layout.addSpacing(8)

        trailing_space = QCheckBox()
        trailing_space.setChecked(bool(self.app.config.get('add_trailing_space', True)))
        self._widgets['trailing_space'] = trailing_space
        layout.addLayout(self._setting_row(
            "Add trailing space",
            "Append a space after each transcription so the next word joins cleanly",
            trailing_space,
        ))
        layout.addSpacing(8)

        auto_capitalize = QCheckBox()
        auto_capitalize.setChecked(bool(self.app.config.get('auto_capitalize', True)))
        self._widgets['auto_capitalize'] = auto_capitalize
        layout.addLayout(self._setting_row(
            "Auto-capitalize",
            "Capitalize the first letter of each transcription",
            auto_capitalize,
        ))
        layout.addSpacing(8)

        format_numbers = QCheckBox()
        format_numbers.setChecked(bool(self.app.config.get('format_numbers', True)))
        self._widgets['format_numbers'] = format_numbers
        layout.addLayout(self._setting_row(
            "Format numbers",
            "Convert spoken numbers to digits (e.g. 'three' to '3')",
            format_numbers,
        ))
        layout.addSpacing(12)

        cleanup_combo = QComboBox()
        cleanup_combo.addItems(['clean', 'verbatim'])
        current_cleanup = self.app.config.get('cleanup_mode', 'clean')
        cleanup_combo.setCurrentText(current_cleanup)
        self._widgets['cleanup_mode'] = cleanup_combo
        layout.addLayout(self._setting_row(
            "Cleanup mode",
            "clean: remove filler words and fix spacing.  verbatim: transcribe exactly as spoken.",
            cleanup_combo,
        ))

        layout.addSpacing(16)

        # ---- Section: Hints ------------------------------------------------
        layout.addWidget(self._section_title("Hints"))
        layout.addSpacing(4)

        hints_enabled = QCheckBox()
        hints_enabled.setChecked(bool(self.app.config.get('hints_enabled', True)))
        self._widgets['hints_enabled'] = hints_enabled
        layout.addLayout(self._setting_row(
            "Show contextual hints",
            "One-time tips that appear after key actions (first dictation, wake word, etc.)",
            hints_enabled,
        ))
        layout.addSpacing(8)

        reset_hints_btn = QPushButton("Reset hints")
        reset_hints_btn.setProperty("class", "secondary")
        reset_hints_btn.setFixedWidth(130)
        reset_hints_btn.clicked.connect(self._reset_hints)
        layout.addWidget(reset_hints_btn)
        layout.addSpacing(16)

        # ---- Section: Profiles ---------------------------------------------
        layout.addWidget(self._section_title("Profiles"))
        layout.addSpacing(4)

        prof_desc = QLabel(
            "Save and load vocabulary, correction, and command profiles."
        )
        prof_desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(prof_desc)
        layout.addSpacing(6)

        manage_btn = QPushButton("Manage Profiles…")
        manage_btn.setFixedWidth(170)
        manage_btn.clicked.connect(self._open_profile_manager)
        layout.addWidget(manage_btn)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _reset_hints(self):
        hints = getattr(self.app, 'hints', None)
        if hints is not None:
            hints.reset()
        print("[HINTS] History reset — all hints will fire again on next trigger")

    def _open_profile_manager(self):
        from pathlib import Path
        from samsara.profiles import ProfileManager
        from samsara.ui.profile_manager_qt import ProfileManagerQt
        if not hasattr(self, '_profile_manager_qt'):
            app_dir = str(Path(__file__).parent)
            pm = ProfileManager(app_dir)
            def _on_changed():
                if hasattr(self.app, 'load_commands'):
                    try:
                        self.app.load_commands()
                    except Exception:
                        pass
                if hasattr(self.app, 'load_training_data'):
                    try:
                        self.app.load_training_data()
                    except Exception:
                        pass
            self._profile_manager_qt = ProfileManagerQt(pm, _on_changed)
        self._profile_manager_qt.show()

    def _build_hotkeys_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        cfg = self.app.config
        ww_cfg = cfg.get('wake_word_config', {}) or {}
        ww_audio = ww_cfg.get('audio', {}) or {}
        cmd_cfg = cfg.get('command_mode', {}) or {}

        # ---- Section: Recording Mode -------------------------------------------
        layout.addWidget(self._section_title("Recording Mode"))
        layout.addSpacing(4)

        mode_combo = QComboBox()
        mode_combo.addItems(['hold', 'toggle', 'continuous'])
        mode_combo.setCurrentText(cfg.get('mode', 'hold'))
        self._widgets['mode'] = mode_combo
        layout.addLayout(self._setting_row(
            "Record mode",
            "hold: hold key to record, release to transcribe.  "
            "toggle: press to start/stop.  continuous: auto-transcribe on silence.",
            mode_combo,
        ))
        layout.addSpacing(8)

        wake_enabled = QCheckBox()
        wake_enabled.setChecked(bool(cfg.get('wake_word_enabled', False)))
        self._widgets['wake_word_enabled'] = wake_enabled
        layout.addLayout(self._setting_row(
            "Wake word listener",
            "Enable 'Jarvis' detection — works alongside any recording mode above",
            wake_enabled,
        ))
        layout.addSpacing(20)

        # ---- Section: Keyboard Shortcuts ----------------------------------------
        layout.addWidget(self._section_title("Keyboard Shortcuts"))
        layout.addSpacing(4)

        _hotkeys = [
            ('hotkey',            cfg.get('hotkey', 'ctrl+shift'),
             "Record",             "Hold key to record (or toggle, depending on mode)"),
            ('continuous_hotkey', cfg.get('continuous_hotkey', 'ctrl+alt+d'),
             "Toggle continuous",  "Switch into continuous auto-transcribe mode"),
            ('wake_word_hotkey',  cfg.get('wake_word_hotkey', 'ctrl+alt+w'),
             "Toggle wake word",   "Enable or disable the wake word listener at runtime"),
            ('command_hotkey',    cfg.get('command_hotkey', 'ctrl+alt+c'),
             "Command only",       "Record but only match voice commands, no text output"),
            ('streaming_hotkey',  cfg.get('streaming_hotkey', 'capslock'),
             "Streaming",          "Toggle live streaming mode (partials shown in overlay)"),
            ('cancel_hotkey',     cfg.get('cancel_hotkey', 'escape'),
             "Cancel recording",   "Abort the current recording without transcribing"),
            ('undo_hotkey',       cfg.get('undo_hotkey', 'ctrl+alt+z'),
             "Undo",               "Remove the last transcription from the focused app"),
            ('ava_mode_key',      cfg.get('ava_mode_key', 'right_alt'),
             "Ava mode",           "Hold to talk to Ava (LLM assistant mode)"),
        ]

        for config_key, default, label, desc in _hotkeys:
            btn = _HotkeyButton(cfg.get(config_key, default))
            self._widgets[config_key] = btn
            layout.addLayout(self._setting_row(label, desc, btn))
            layout.addSpacing(6)

        layout.addSpacing(14)

        # ---- Section: Wake Word -------------------------------------------------
        layout.addWidget(self._section_title("Wake Word"))
        layout.addSpacing(4)

        phrases = ww_cfg.get('phrase_options', ['jarvis'])
        primary_phrase = phrases[0] if phrases else 'jarvis'
        phrase_row = QHBoxLayout()
        phrase_label = QLabel(f'"{primary_phrase}"')
        phrase_label.setStyleSheet(
            "color: #5EEAD4; font-size: 14px; font-weight: 600; "
            "font-family: 'Consolas', 'Courier New', monospace;"
        )
        note_label = QLabel("  More wake word options coming soon.")
        note_label.setStyleSheet("color: #8A8A92; font-size: 12px;")
        phrase_row.addLayout(self._setting_row(
            "Wake phrase",
            "The word or phrase Samsara listens for to activate recording",
            phrase_label,
        ))
        layout.addLayout(phrase_row)
        layout.addSpacing(4)

        note = QLabel("More wake word options coming soon.")
        note.setStyleSheet("color: #8A8A92; font-size: 12px; margin-left: 0px;")
        layout.addWidget(note)
        layout.addSpacing(8)

        threshold_combo = QComboBox()
        threshold_combo.addItems(['auto', 'manual'])
        threshold_combo.setCurrentText(cfg.get('threshold_mode', 'auto'))
        self._widgets['threshold_mode'] = threshold_combo
        layout.addLayout(self._setting_row(
            "Speech threshold mode",
            "auto: calibrate on startup.  manual: use a fixed multiplier below",
            threshold_combo,
        ))
        layout.addSpacing(8)

        cal_spin = QDoubleSpinBox()
        cal_spin.setRange(1.0, 10.0)
        cal_spin.setSingleStep(0.1)
        cal_spin.setDecimals(1)
        cal_spin.setValue(float(cfg.get('cal_multiplier', 3.0)))
        self._widgets['cal_multiplier'] = cal_spin
        layout.addLayout(self._setting_row(
            "Calibration multiplier",
            "Auto mode: signal must be this many times louder than ambient to count as speech",
            cal_spin,
        ))
        layout.addSpacing(8)

        wake_timeout_spin = QDoubleSpinBox()
        wake_timeout_spin.setRange(1.0, 30.0)
        wake_timeout_spin.setSingleStep(0.5)
        wake_timeout_spin.setDecimals(1)
        wake_timeout_spin.setSuffix(" s")
        wake_timeout_spin.setValue(float(ww_audio.get('wake_command_timeout', 5.0)))
        self._widgets['wake_cmd_timeout'] = wake_timeout_spin
        layout.addLayout(self._setting_row(
            "Wake command timeout",
            "Seconds to wait for a voice command after the wake phrase is heard",
            wake_timeout_spin,
        ))
        layout.addSpacing(8)

        quick_silence_spin = QDoubleSpinBox()
        quick_silence_spin.setRange(0.2, 5.0)
        quick_silence_spin.setSingleStep(0.1)
        quick_silence_spin.setDecimals(1)
        quick_silence_spin.setSuffix(" s")
        quick_silence_spin.setValue(float(ww_cfg.get('quick_silence_timeout', 1.0)))
        self._widgets['quick_silence'] = quick_silence_spin
        layout.addLayout(self._setting_row(
            "Quick silence timeout",
            "Seconds of silence that ends a wake-word listening session early",
            quick_silence_spin,
        ))
        layout.addSpacing(8)

        oww_spin = QDoubleSpinBox()
        oww_spin.setRange(0.05, 1.0)
        oww_spin.setSingleStep(0.05)
        oww_spin.setDecimals(2)
        oww_spin.setValue(float(ww_cfg.get('oww_threshold', 0.20)))
        self._widgets['oww_threshold'] = oww_spin
        layout.addLayout(self._setting_row(
            "Wake word sensitivity",
            "0.05 = very sensitive, 0.50 = strict.  Only affects Jarvis/Alexa/Mycroft models.",
            oww_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Command Mode ----------------------------------------------
        layout.addWidget(self._section_title("Command Mode"))
        layout.addSpacing(4)

        _button_options = ['mouse4', 'mouse5', 'rctrl', 'f13', 'right_alt']
        cmd_button_combo = QComboBox()
        cmd_button_combo.addItems(_button_options)
        current_btn = cmd_cfg.get('button', 'mouse4')
        if current_btn not in _button_options:
            cmd_button_combo.addItem(current_btn)
        cmd_button_combo.setCurrentText(current_btn)
        self._widgets['cmd_button'] = cmd_button_combo
        layout.addLayout(self._setting_row(
            "Button",
            "Physical button that activates walkie-talkie command mode",
            cmd_button_combo,
        ))
        layout.addSpacing(8)

        cmd_mode_combo = QComboBox()
        cmd_mode_combo.addItems(['hold', 'toggle'])
        cmd_mode_combo.setCurrentText(cmd_cfg.get('mode', 'hold'))
        self._widgets['cmd_mode'] = cmd_mode_combo
        layout.addLayout(self._setting_row(
            "Mode",
            "hold: hold button to stay in command mode.  toggle: press once to enter/exit",
            cmd_mode_combo,
        ))
        layout.addSpacing(8)

        debounce_spin = QSpinBox()
        debounce_spin.setRange(0, 2000)
        debounce_spin.setSingleStep(50)
        debounce_spin.setSuffix(" ms")
        debounce_spin.setValue(int(cmd_cfg.get('enter_debounce_ms', 200)))
        self._widgets['cmd_debounce'] = debounce_spin
        layout.addLayout(self._setting_row(
            "Enter debounce",
            "Minimum hold time before the command mode earcon plays (prevents accidental activation)",
            debounce_spin,
        ))
        layout.addSpacing(8)

        timeout_spin = QSpinBox()
        timeout_spin.setRange(5, 300)
        timeout_spin.setSingleStep(5)
        timeout_spin.setSuffix(" s")
        timeout_spin.setValue(int(cmd_cfg.get('inactivity_timeout_s', 30)))
        self._widgets['cmd_timeout'] = timeout_spin
        layout.addLayout(self._setting_row(
            "Inactivity timeout",
            "Toggle mode: exit command mode after this many seconds of no speech",
            timeout_spin,
        ))
        layout.addSpacing(8)

        miss_spin = QSpinBox()
        miss_spin.setRange(1, 20)
        miss_spin.setValue(int(cmd_cfg.get('miss_limit', 5)))
        self._widgets['cmd_miss_limit'] = miss_spin
        layout.addLayout(self._setting_row(
            "Miss limit",
            "Toggle mode: exit command mode after this many unmatched recordings",
            miss_spin,
        ))

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_commands_tab(self):
        import json
        from pathlib import Path
        from samsara.command_packs import PACKS

        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 12)
        layout.setSpacing(8)

        cfg = self.app.config
        cmd_cfg = cfg.get('command_mode', {}) or {}

        # ---- Section: Command Mode Input ------------------------------------
        layout.addWidget(self._section_title("Command Mode Input"))

        desc0 = QLabel(
            "Choose which button activates command mode (walkie-talkie hold-to-talk)."
        )
        desc0.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(desc0)
        layout.addSpacing(4)

        current_btn_key   = cmd_cfg.get('button', 'mouse4')
        current_btn_label = _CMD_BUTTON_KEY_TO_LABEL.get(current_btn_key, 'Mouse 4 (default)')
        btn_combo = QComboBox()
        btn_combo.addItems(list(_CMD_BUTTON_OPTIONS.keys()))
        btn_combo.setCurrentText(current_btn_label)
        self._widgets['cmd_tab_button'] = btn_combo
        layout.addLayout(self._setting_row(
            "Command Mode Button",
            "Physical button or key that activates walkie-talkie command mode",
            btn_combo,
        ))
        layout.addSpacing(4)

        suppress_cb = QCheckBox(
            "Suppress browser-back when using Mouse 4/5 for commands"
        )
        suppress_cb.setChecked(bool(cmd_cfg.get('suppress_button', True)))
        self._widgets['cmd_tab_suppress'] = suppress_cb
        layout.addWidget(suppress_cb)

        suppress_note = QLabel(
            "    When enabled, Mouse 4/5 only triggers command mode and never navigates back."
        )
        suppress_note.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(suppress_note)
        layout.addSpacing(6)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("background-color: rgba(255,255,255,0.06); max-height: 1px;")
        layout.addWidget(sep1)
        layout.addSpacing(4)

        # ---- Section: Command Packs ----------------------------------------
        layout.addWidget(self._section_title("Command Packs"))

        desc1 = QLabel(
            "Enable the packs you use. Disabling unused packs improves recognition accuracy."
        )
        desc1.setWordWrap(True)
        desc1.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(desc1)
        layout.addSpacing(4)

        pack_scroll = QScrollArea()
        pack_scroll.setWidgetResizable(True)
        pack_scroll.setFixedHeight(210)
        pack_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pack_scroll.setStyleSheet(
            "QScrollArea { background-color: #111114; border-radius: 6px; "
            "border: 1px solid rgba(255,255,255,0.08); }"
        )

        pack_container = QWidget()
        pack_container.setStyleSheet("background: transparent;")
        pack_vlayout = QVBoxLayout(pack_container)
        pack_vlayout.setContentsMargins(10, 8, 10, 8)
        pack_vlayout.setSpacing(3)

        current_packs = cfg.get('command_packs', {}) or {}
        pack_checkboxes: dict[str, QCheckBox] = {}

        # Count commands per pack from both commands.json and plugin registry
        pack_counts: dict[str, int] = {}
        try:
            exe = getattr(self.app, 'command_executor', None)
            if exe and hasattr(exe, 'commands_path'):
                raw = json.loads(exe.commands_path.read_text(encoding='utf-8'))
                for cmd in raw.get('commands', raw).values():
                    p = cmd.get('pack', 'core')
                    pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception:
            pass
        try:
            from samsara import plugin_commands as _pc
            seen: set = set()
            for entry in _pc._REGISTRY.values():
                eid = id(entry)
                if eid in seen:
                    continue
                seen.add(eid)
                p = entry.get('pack', 'core')
                pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception:
            pass

        restart_lbl = QLabel("Restart Samsara to apply pack changes.")
        restart_lbl.setStyleSheet("color: #E2A030; font-size: 12px;")
        restart_lbl.setVisible(False)
        self._widgets['_pack_restart_lbl'] = restart_lbl

        for pack_id, meta in PACKS.items():
            always_on     = meta.get('always_on', False)
            default_on    = meta.get('default_enabled', False)
            enabled       = bool(current_packs.get(pack_id, default_on)) or always_on
            count         = pack_counts.get(pack_id, 0)
            count_str     = f"  ({count})" if count else ""

            row_w = QWidget()
            row_w.setStyleSheet("background: transparent;")
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(8)

            cb = QCheckBox()
            cb.setChecked(enabled)
            cb.setEnabled(not always_on)
            cb.toggled.connect(lambda _, lbl=restart_lbl: lbl.setVisible(True))
            pack_checkboxes[pack_id] = cb
            row_h.addWidget(cb)

            name_lbl = QLabel(
                meta.get('label', pack_id)
                + ("  •  always on" if always_on else "")
                + count_str
            )
            name_lbl.setStyleSheet(
                f"color: {'#8A8A92' if always_on else '#E8E8EA'}; "
                f"font-size: 13px; font-weight: {'normal' if always_on else '600'};"
                "background: transparent;"
            )
            row_h.addWidget(name_lbl)

            desc_lbl = QLabel(meta.get('description', ''))
            desc_lbl.setStyleSheet("color: #8A8A92; font-size: 11px; background: transparent;")
            row_h.addWidget(desc_lbl, stretch=1)

            pack_vlayout.addWidget(row_w)

        pack_vlayout.addStretch()
        pack_scroll.setWidget(pack_container)
        self._widgets['_pack_checkboxes'] = pack_checkboxes
        layout.addWidget(pack_scroll)
        layout.addWidget(restart_lbl)
        layout.addSpacing(4)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background-color: rgba(255,255,255,0.06); max-height: 1px;")
        layout.addWidget(sep2)
        layout.addSpacing(4)

        # ---- Section: Voice Commands ---------------------------------------
        cmd_header = QHBoxLayout()
        cmd_header.addWidget(self._section_title("Voice Commands"))
        cmd_header.addStretch()
        search_box = QLineEdit()
        search_box.setPlaceholderText("Search commands...")
        search_box.setFixedWidth(200)
        self._widgets['cmd_search'] = search_box
        cmd_header.addWidget(search_box)
        layout.addLayout(cmd_header)

        # Table
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(['Voice Phrase', 'Type', 'Action', 'Description'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(
            table.styleSheet()
            + "QTableWidget { alternate-background-color: rgba(255,255,255,0.02); }"
        )
        table.setMinimumHeight(260)
        self._widgets['cmd_table'] = table
        self._populate_commands_table(table, "")
        search_box.textChanged.connect(lambda txt: self._filter_commands(table, txt))
        layout.addWidget(table, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_btn = QPushButton("Add Command")
        add_btn.setFixedWidth(120)
        add_btn.clicked.connect(lambda: self._open_command_dialog(None, table))
        btn_row.addWidget(add_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.setFixedWidth(80)
        edit_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 16px; }"
            "QPushButton:hover { background-color: rgba(255,255,255,0.05); color: #E8E8EA; }"
        )
        edit_btn.clicked.connect(lambda: self._edit_selected_command(table))
        btn_row.addWidget(edit_btn)

        del_btn = QPushButton("Delete")
        del_btn.setFixedWidth(80)
        del_btn.setStyleSheet(
            "QPushButton { background-color: rgba(200,60,60,0.15); color: #FF8888; "
            "border: 1px solid rgba(200,60,60,0.3); border-radius: 6px; padding: 8px 16px; }"
            "QPushButton:hover { background-color: rgba(200,60,60,0.25); }"
        )
        del_btn.clicked.connect(lambda: self._delete_selected_command(table))
        btn_row.addWidget(del_btn)

        test_btn = QPushButton("Test")
        test_btn.setFixedWidth(70)
        test_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 12px; }"
            "QPushButton:hover { background-color: rgba(255,255,255,0.05); color: #E8E8EA; }"
        )
        test_btn.clicked.connect(lambda: self._test_selected_command(table))
        btn_row.addWidget(test_btn)

        btn_row.addStretch()

        reload_btn = QPushButton("Reload")
        reload_btn.setFixedWidth(80)
        reload_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 12px; }"
            "QPushButton:hover { background-color: rgba(255,255,255,0.05); color: #E8E8EA; }"
        )
        reload_btn.clicked.connect(lambda: self._reload_commands(table))
        btn_row.addWidget(reload_btn)

        layout.addLayout(btn_row)

        footer = QLabel("Say these phrases while recording to trigger actions.")
        footer.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(footer)

        return outer

    # ------------------------------------------------------------------
    # Commands tab helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cmd_action_text(cmd_data: dict) -> str:
        t = cmd_data.get('type', '')
        if t == 'hotkey':
            return '+'.join(k.capitalize() for k in cmd_data.get('keys', []))
        if t == 'launch':
            tgt = cmd_data.get('target', '')
            return ('...' + tgt[-24:]) if len(tgt) > 27 else tgt
        if t in ('press', 'key_down', 'key_up'):
            verb = {'press': 'Press', 'key_down': 'Hold', 'key_up': 'Release'}.get(t, t)
            return f"{verb} {cmd_data.get('key', '').upper()}"
        if t == 'mouse':
            return (f"{cmd_data.get('action','click').replace('_',' ').title()} "
                    f"({cmd_data.get('button','left')})")
        if t == 'release_all':
            return "Release all keys"
        if t == 'text':
            txt = cmd_data.get('text', '')
            return repr(txt[:30]) + ('...' if len(txt) > 30 else '')
        if t == 'macro':
            steps = cmd_data.get('steps', [])
            return f"Macro ({len(steps)} steps)"
        return str(cmd_data.get('type', ''))

    def _populate_commands_table(self, table: QTableWidget, filter_text: str = '') -> None:
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        try:
            commands = getattr(
                getattr(self.app, 'command_executor', None), 'commands', {}
            ) or {}
            fl = filter_text.lower()
            for phrase, data in sorted(commands.items()):
                if fl and not any(
                    fl in s.lower() for s in (
                        phrase,
                        data.get('type', ''),
                        data.get('description', ''),
                        data.get('pack', ''),
                    )
                ):
                    continue
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(phrase))
                table.setItem(row, 1, QTableWidgetItem(data.get('type', '')))
                table.setItem(row, 2, QTableWidgetItem(self._cmd_action_text(data)))
                table.setItem(row, 3, QTableWidgetItem(data.get('description', '')))
        finally:
            table.setUpdatesEnabled(True)

    def _filter_commands(self, table: QTableWidget, text: str) -> None:
        self._populate_commands_table(table, text)

    def _selected_phrase(self, table: QTableWidget):
        rows = table.selectedItems()
        if not rows:
            return None
        return table.item(table.currentRow(), 0).text()

    def _save_commands_to_disk(self) -> None:
        import json
        from pathlib import Path
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        path = getattr(exe, 'commands_path', None)
        if path is None:
            path = Path(__file__).parent.parent.parent / 'commands.json'
        try:
            data = {'commands': exe.commands}
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save commands:\n{e}")

    def _reload_commands(self, table: QTableWidget) -> None:
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        try:
            exe.load_commands()
            search = self._widgets.get('cmd_search')
            self._populate_commands_table(
                table, search.text() if search else ''
            )
            QMessageBox.information(
                self, "Reloaded",
                f"Loaded {len(exe.commands)} commands."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reload:\n{e}")

    def _delete_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to delete.")
            return
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete the command '{phrase}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        exe = getattr(self.app, 'command_executor', None)
        if exe and phrase in exe.commands:
            del exe.commands[phrase]
            self._save_commands_to_disk()
            search = self._widgets.get('cmd_search')
            self._populate_commands_table(
                table, search.text() if search else ''
            )

    def _test_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to test.")
            return
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        self.showMinimized()
        import threading, time
        def _run():
            time.sleep(0.4)
            try:
                ok = exe.execute_command(phrase)
                msg = f"'{phrase}' executed OK." if ok else f"'{phrase}' not found or failed."
                self._test_result.emit(msg, "#5EEAD4" if ok else "#FF6666")
            except Exception as exc:
                self._test_result.emit(f"Error: {exc}", "#FF6666")
            self.showNormal()
        threading.Thread(target=_run, daemon=True).start()

    def _edit_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to edit.")
            return
        self._open_command_dialog(phrase, table)

    def _open_command_dialog(self, edit_phrase, table: QTableWidget) -> None:
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return

        existing = exe.commands.get(edit_phrase, {}) if edit_phrase else {}
        _TYPES = ['hotkey', 'text', 'launch', 'press', 'key_down', 'key_up',
                  'mouse', 'release_all']

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Command" if edit_phrase else "Add Command")
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(self.styleSheet())

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(20, 20, 20, 16)
        dlg_layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        phrase_edit = QLineEdit(edit_phrase or '')
        phrase_edit.setPlaceholderText("e.g. open browser")
        form.addRow("Voice phrase:", phrase_edit)

        type_combo = QComboBox()
        type_combo.addItems(_TYPES)
        type_combo.setCurrentText(existing.get('type', 'hotkey'))
        form.addRow("Command type:", type_combo)

        dlg_layout.addLayout(form)

        # Dynamic fields area — QStackedWidget
        stack = QStackedWidget()
        stack.setMinimumHeight(70)

        # Page 0: hotkey
        p_hotkey = QWidget()
        pl0 = QFormLayout(p_hotkey)
        pl0.setContentsMargins(0, 0, 0, 0)
        keys_edit = QLineEdit('+'.join(existing.get('keys', [])))
        keys_edit.setPlaceholderText("e.g. ctrl+shift+a")
        pl0.addRow("Keys:", keys_edit)
        hint0 = QLabel("Use + to combine keys: ctrl, shift, alt, a-z, 0-9, f1-f12, etc.")
        hint0.setStyleSheet("color: #8A8A92; font-size: 11px;")
        pl0.addRow("", hint0)
        stack.addWidget(p_hotkey)   # 0

        # Page 1: text
        p_text = QWidget()
        pl1 = QFormLayout(p_text)
        pl1.setContentsMargins(0, 0, 0, 0)
        text_edit = QLineEdit(existing.get('text', ''))
        text_edit.setPlaceholderText("Text to insert")
        pl1.addRow("Text:", text_edit)
        stack.addWidget(p_text)     # 1

        # Page 2: launch
        p_launch = QWidget()
        pl2 = QFormLayout(p_launch)
        pl2.setContentsMargins(0, 0, 0, 0)
        target_edit = QLineEdit(existing.get('target', ''))
        target_edit.setPlaceholderText("e.g. chrome.exe or full path")
        pl2.addRow("Program:", target_edit)
        stack.addWidget(p_launch)   # 2

        # Page 3: press / key_down / key_up
        p_key = QWidget()
        pl3 = QFormLayout(p_key)
        pl3.setContentsMargins(0, 0, 0, 0)
        key_edit = QLineEdit(existing.get('key', ''))
        key_edit.setPlaceholderText("e.g. space, enter, a, shift")
        pl3.addRow("Key:", key_edit)
        stack.addWidget(p_key)      # 3

        # Page 4: mouse
        p_mouse = QWidget()
        pl4 = QFormLayout(p_mouse)
        pl4.setContentsMargins(0, 0, 0, 0)
        action_combo = QComboBox()
        action_combo.addItems(['click', 'double_click'])
        action_combo.setCurrentText(existing.get('action', 'click'))
        pl4.addRow("Action:", action_combo)
        button_combo = QComboBox()
        button_combo.addItems(['left', 'right', 'middle'])
        button_combo.setCurrentText(existing.get('button', 'left'))
        pl4.addRow("Button:", button_combo)
        stack.addWidget(p_mouse)    # 4

        # Page 5: release_all
        p_release = QWidget()
        pl5 = QVBoxLayout(p_release)
        pl5.setContentsMargins(0, 0, 0, 0)
        pl5.addWidget(QLabel("No additional settings — this releases all held keys."))
        stack.addWidget(p_release)  # 5

        _TYPE_PAGE = {
            'hotkey': 0, 'text': 1, 'launch': 2,
            'press': 3, 'key_down': 3, 'key_up': 3,
            'mouse': 4, 'release_all': 5,
        }
        stack.setCurrentIndex(_TYPE_PAGE.get(type_combo.currentText(), 0))
        type_combo.currentTextChanged.connect(
            lambda t: stack.setCurrentIndex(_TYPE_PAGE.get(t, 0))
        )
        dlg_layout.addWidget(stack)

        desc_form = QFormLayout()
        desc_form.setContentsMargins(0, 0, 0, 0)
        desc_edit = QLineEdit(existing.get('description', ''))
        desc_edit.setPlaceholderText("Optional description")
        desc_form.addRow("Description:", desc_edit)
        dlg_layout.addLayout(desc_form)

        # Buttons
        dlg_layout.addSpacing(4)
        btn_row2 = QHBoxLayout()
        btn_row2.addStretch()
        cancel_btn2 = QPushButton("Cancel")
        cancel_btn2.setFixedWidth(90)
        cancel_btn2.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; padding: 8px 16px; }"
            "QPushButton:hover { color: #E8E8EA; }"
        )
        cancel_btn2.clicked.connect(dlg.reject)
        btn_row2.addWidget(cancel_btn2)

        save_btn2 = QPushButton("Save")
        save_btn2.setFixedWidth(90)
        save_btn2.clicked.connect(lambda: self._dialog_save(
            dlg, edit_phrase, exe, table,
            phrase_edit, type_combo, keys_edit, text_edit,
            target_edit, key_edit, action_combo, button_combo, desc_edit,
        ))
        btn_row2.addWidget(save_btn2)
        dlg_layout.addLayout(btn_row2)

        dlg.exec()

    def _dialog_save(
        self, dlg, edit_phrase, exe, table,
        phrase_edit, type_combo, keys_edit, text_edit,
        target_edit, key_edit, action_combo, button_combo, desc_edit,
    ) -> None:
        phrase = phrase_edit.text().strip().lower()
        if not phrase:
            QMessageBox.warning(dlg, "Error", "Voice phrase is required.")
            return

        if not edit_phrase or phrase != edit_phrase.lower():
            if phrase in exe.commands:
                QMessageBox.warning(
                    dlg, "Error",
                    f"A command '{phrase}' already exists."
                )
                return

        t = type_combo.currentText()
        data: dict = {'type': t, 'description': desc_edit.text().strip()}

        if t == 'hotkey':
            keys = [k.strip().lower() for k in keys_edit.text().split('+') if k.strip()]
            if not keys:
                QMessageBox.warning(dlg, "Error", "Specify at least one key.")
                return
            data['keys'] = keys
        elif t == 'text':
            txt = text_edit.text().strip()
            if not txt:
                QMessageBox.warning(dlg, "Error", "Specify text to insert.")
                return
            data['text'] = txt
        elif t == 'launch':
            tgt = target_edit.text().strip()
            if not tgt:
                QMessageBox.warning(dlg, "Error", "Specify a program to launch.")
                return
            data['target'] = tgt
        elif t in ('press', 'key_down', 'key_up'):
            k = key_edit.text().strip().lower()
            if not k:
                QMessageBox.warning(dlg, "Error", "Specify a key.")
                return
            data['key'] = k
        elif t == 'mouse':
            data['action'] = action_combo.currentText()
            data['button'] = button_combo.currentText()

        if edit_phrase and phrase != edit_phrase.lower():
            exe.commands.pop(edit_phrase, None)

        exe.commands[phrase] = data
        self._save_commands_to_disk()

        search = self._widgets.get('cmd_search')
        self._populate_commands_table(table, search.text() if search else '')

        dlg.accept()
        QMessageBox.information(self, "Saved", f"Command '{phrase}' saved.")

    def _build_sounds_tab(self):
        from pathlib import Path
        import shutil

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        cfg = self.app.config
        sounds_dir = getattr(self.app, 'sounds_dir', None)
        if sounds_dir is None:
            sounds_dir = Path(__file__).parent.parent.parent / 'sounds'

        # ---- Section: Audio Feedback -------------------------------------------
        layout.addWidget(self._section_title("Audio Feedback"))
        layout.addSpacing(4)

        feedback_cb = QCheckBox("Enable audio feedback sounds")
        feedback_cb.setChecked(bool(cfg.get('audio_feedback', True)))
        self._widgets['sound_feedback'] = feedback_cb
        layout.addWidget(feedback_cb)
        layout.addSpacing(10)

        # Volume row: label + slider + percentage label + test button
        vol_row = QHBoxLayout()
        vol_row.setSpacing(12)
        vol_lbl = QLabel("Volume:")
        vol_lbl.setStyleSheet("color: #E8E8EA; font-size: 14px;")
        vol_lbl.setFixedWidth(70)

        raw_vol = float(cfg.get('sound_volume', 0.5))
        vol_slider = QSlider(Qt.Orientation.Horizontal)
        vol_slider.setRange(0, 100)
        vol_slider.setValue(int(raw_vol * 100))
        vol_slider.setFixedWidth(200)
        vol_slider.setStyleSheet(
            "QSlider::groove:horizontal {"
            "  height: 4px; background: rgba(255,255,255,0.12); border-radius: 2px;"
            "}"
            "QSlider::handle:horizontal {"
            "  width: 16px; height: 16px; margin: -6px 0;"
            "  border-radius: 8px; background: #5EEAD4;"
            "}"
            "QSlider::sub-page:horizontal {"
            "  background: #5EEAD4; border-radius: 2px;"
            "}"
        )
        self._widgets['sound_volume_slider'] = vol_slider

        vol_pct = QLabel(f"{int(raw_vol * 100)}%")
        vol_pct.setStyleSheet("color: #E8E8EA; font-size: 13px;")
        vol_pct.setFixedWidth(40)
        vol_slider.valueChanged.connect(lambda v: vol_pct.setText(f"{v}%"))

        test_btn = QPushButton("Test")
        test_btn.setFixedWidth(60)
        test_btn.clicked.connect(lambda: self._play(sounds_dir, 'success'))

        vol_row.addWidget(vol_lbl)
        vol_row.addWidget(vol_slider)
        vol_row.addWidget(vol_pct)
        vol_row.addWidget(test_btn)
        vol_row.addStretch()
        layout.addLayout(vol_row)
        layout.addSpacing(20)

        # ---- Section: Sound Theme ----------------------------------------------
        layout.addWidget(self._section_title("Sound Theme"))
        layout.addSpacing(4)

        themes_dir = sounds_dir / 'themes'
        if themes_dir.exists():
            available_themes = sorted(
                d.name for d in themes_dir.iterdir()
                if d.is_dir() and (d / 'start.wav').exists()
            )
        else:
            available_themes = ['cute', 'warm', 'zen', 'classic', 'chirpy']

        current_theme = cfg.get('sound_theme', 'cute')
        theme_combo = QComboBox()
        theme_combo.addItems(available_themes)
        if current_theme in available_themes:
            theme_combo.setCurrentText(current_theme)
        self._widgets['sound_theme_combo'] = theme_combo

        apply_theme_btn = QPushButton("Apply Theme")
        apply_theme_btn.setFixedWidth(110)
        apply_theme_btn.clicked.connect(
            lambda: self._apply_sound_theme(
                theme_combo.currentText(), sounds_dir, themes_dir
            )
        )

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        theme_row.addLayout(self._setting_row(
            "Theme",
            "cute = playful bloops  •  warm = OS boot vibes  •  "
            "zen = singing bowls  •  classic = original  •  chirpy = bright",
            theme_combo,
        ))
        layout.addLayout(theme_row)

        apply_row = QHBoxLayout()
        apply_row.addWidget(apply_theme_btn)
        apply_row.addStretch()
        layout.addLayout(apply_row)
        layout.addSpacing(20)

        # ---- Section: Earcon Preview -------------------------------------------
        layout.addWidget(self._section_title("Earcon Preview"))
        desc = QLabel("Preview the audio cues for the active theme.")
        desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(desc)
        layout.addSpacing(6)

        _EARCONS = [
            ('start',            'Recording start'),
            ('stop',             'Recording stop'),
            ('success',          'Transcription success'),
            ('error',            'Error'),
            ('capture_started',  'Capture started'),
            ('capture_saved',    'Capture saved'),
            ('agent_routing',    'Agent routing'),
            ('agent_response',   'Agent response'),
            ('confirm_required', 'Confirm required'),
            ('action_complete',  'Action complete'),
            ('thinking_pulse',   'Thinking pulse'),
        ]

        grid_widget = QWidget()
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        cols = 3
        for idx, (sound_key, label_text) in enumerate(_EARCONS):
            row_i = idx // cols
            col_i = (idx % cols) * 2

            name_lbl = QLabel(label_text)
            name_lbl.setStyleSheet("color: #E8E8EA; font-size: 13px;")
            play_btn = QPushButton("▶")
            play_btn.setFixedWidth(36)
            play_btn.setStyleSheet(
                "QPushButton { background-color: #16161A; border: 1px solid rgba(255,255,255,0.14);"
                " border-radius: 5px; color: #5EEAD4; font-size: 13px; padding: 4px; }"
                "QPushButton:hover { background-color: rgba(94,234,212,0.12); }"
            )
            play_btn.clicked.connect(
                lambda _=False, k=sound_key: self._play(sounds_dir, k)
            )
            grid.addWidget(name_lbl, row_i, col_i)
            grid.addWidget(play_btn, row_i, col_i + 1)

        for c in range(cols * 2):
            if c % 2 == 0:
                grid.setColumnStretch(c, 1)

        layout.addWidget(grid_widget)
        layout.addSpacing(20)

        # ---- Section: Sound Files ----------------------------------------------
        layout.addWidget(self._section_title("Sound Files"))
        files_desc = QLabel(
            f"Active sound files from: {sounds_dir}"
        )
        files_desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        files_desc.setWordWrap(True)
        layout.addWidget(files_desc)
        layout.addSpacing(6)

        _CORE_SOUNDS = [
            ('start',   'Recording start'),
            ('stop',    'Recording stop'),
            ('success', 'Success'),
            ('error',   'Error'),
        ]
        for sound_key, label_text in _CORE_SOUNDS:
            wav = sounds_dir / f"{sound_key}.wav"
            exists = wav.exists()
            file_row = QHBoxLayout()
            file_row.setSpacing(10)

            name_lbl = QLabel(label_text + ":")
            name_lbl.setStyleSheet("color: #E8E8EA; font-size: 13px;")
            name_lbl.setFixedWidth(150)

            fname_lbl = QLabel(wav.name if exists else "not found")
            fname_lbl.setStyleSheet(
                f"color: {'#8A8A92' if exists else '#FF6666'}; font-size: 12px;"
            )
            fname_lbl.setFixedWidth(140)

            play_btn = QPushButton("▶")
            play_btn.setFixedWidth(36)
            play_btn.setEnabled(exists)
            play_btn.setStyleSheet(
                "QPushButton { background-color: #16161A; border: 1px solid rgba(255,255,255,0.14);"
                " border-radius: 5px; color: #5EEAD4; font-size: 13px; padding: 4px; }"
                "QPushButton:hover { background-color: rgba(94,234,212,0.12); }"
                "QPushButton:disabled { color: #444; border-color: rgba(255,255,255,0.06); }"
            )
            play_btn.clicked.connect(
                lambda _=False, k=sound_key: self._play(sounds_dir, k)
            )

            file_row.addWidget(name_lbl)
            file_row.addWidget(fname_lbl)
            file_row.addWidget(play_btn)
            file_row.addStretch()
            layout.addLayout(file_row)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Sounds tab helpers
    # ------------------------------------------------------------------

    def _play(self, sounds_dir, sound_key: str) -> None:
        """Play a sound — tries app.play_sound first, falls back to winsound."""
        try:
            self.app.play_sound(sound_key)
            return
        except Exception:
            pass
        try:
            import winsound
            wav = sounds_dir / f"{sound_key}.wav"
            if wav.exists():
                winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            print(f"[SOUNDS] Could not play {sound_key}: {e}")

    def _apply_sound_theme(self, theme: str, sounds_dir, themes_dir) -> None:
        """Copy WAV files from the selected theme folder into sounds_dir."""
        import shutil
        theme_path = themes_dir / theme
        if not theme_path.exists():
            print(f"[SOUNDS] Theme folder not found: {theme_path}")
            return

        def _do():
            for wav in theme_path.glob('*.wav'):
                try:
                    shutil.copy2(wav, sounds_dir / wav.name)
                except Exception as e:
                    print(f"[SOUNDS] copy {wav.name}: {e}")
            try:
                self.app._load_sound_cache()
            except Exception:
                pass
            try:
                self.app.play_sound('success')
            except Exception:
                pass
            print(f"[SOUNDS] Theme applied: {theme}")

        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # TTS tab
    # ------------------------------------------------------------------

    def _build_tts_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        cfg    = self.app.config.get('tts', {}) or {}
        ac_cfg = self.app.config.get('audio_coordinator', {}) or {}

        # ---- Master toggle --------------------------------------------------
        layout.addWidget(self._section_title("Text-to-Speech"))
        layout.addSpacing(4)

        tts_enabled = QCheckBox("Enable text-to-speech")
        tts_enabled.setChecked(bool(cfg.get('enabled', False)))
        self._widgets['tts_enabled'] = tts_enabled
        layout.addWidget(tts_enabled)

        restart_note = QLabel("Restart Samsara to apply enable/disable changes.")
        restart_note.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(restart_note)
        layout.addSpacing(16)

        # ---- Engine & Voice -------------------------------------------------
        layout.addWidget(self._section_title("Voice"))
        layout.addSpacing(4)

        engine_combo = QComboBox()
        engine_combo.addItems(['winrt', 'edge'])
        engine_combo.setCurrentText(cfg.get('engine', 'winrt'))
        self._widgets['tts_engine'] = engine_combo
        layout.addLayout(self._setting_row(
            "Engine",
            "winrt: Windows built-in voices.  edge: Azure Neural (requires internet). Restart required.",
            engine_combo,
        ))
        layout.addSpacing(8)

        # Populate voice list from current engine instance
        voice_labels: list[str] = []
        label_to_id: dict[str, str] = {}
        id_to_label: dict[str, str] = {}
        engine_obj = getattr(self.app, 'tts_engine', None)
        if engine_obj is not None:
            try:
                for v in engine_obj.list_voices():
                    lbl = f"{v.display_name} ({v.language})"
                    voice_labels.append(lbl)
                    label_to_id[lbl] = v.voice_id
                    id_to_label[v.voice_id] = lbl
            except Exception:
                pass

        current_voice_id = cfg.get('voice_id')
        if voice_labels:
            initial_lbl = id_to_label.get(current_voice_id, voice_labels[0])
        else:
            initial_lbl = current_voice_id or "No voices — enable TTS and restart"

        voice_combo = QComboBox()
        voice_combo.addItems(voice_labels or [initial_lbl])
        voice_combo.setCurrentText(initial_lbl)
        voice_combo.setEnabled(bool(voice_labels))
        self._widgets['tts_voice_combo']        = voice_combo
        self._widgets['tts_voice_label_to_id']  = label_to_id
        layout.addLayout(self._setting_row(
            "Voice",
            "Voice used for Ava's spoken responses",
            voice_combo,
        ))
        layout.addSpacing(16)

        # ---- Voice tuning ---------------------------------------------------
        layout.addWidget(self._section_title("Voice Tuning"))
        layout.addSpacing(4)

        speed_spin = QDoubleSpinBox()
        speed_spin.setRange(0.5, 2.0)
        speed_spin.setSingleStep(0.1)
        speed_spin.setDecimals(2)
        speed_spin.setValue(float(cfg.get('speed', 1.0)))
        self._widgets['tts_speed'] = speed_spin
        layout.addLayout(self._setting_row(
            "Speed",
            "1.0 = normal, 0.5 = half-speed, 2.0 = double-speed",
            speed_spin,
        ))
        layout.addSpacing(8)

        pitch_spin = QDoubleSpinBox()
        pitch_spin.setRange(0.5, 2.0)
        pitch_spin.setSingleStep(0.1)
        pitch_spin.setDecimals(2)
        pitch_spin.setValue(float(cfg.get('pitch', 1.0)))
        self._widgets['tts_pitch'] = pitch_spin
        layout.addLayout(self._setting_row(
            "Pitch",
            "1.0 = normal pitch, lower values deepen, higher values raise",
            pitch_spin,
        ))
        layout.addSpacing(8)

        raw_vol = float(cfg.get('volume', 0.8))
        vol_slider = QSlider(Qt.Orientation.Horizontal)
        vol_slider.setRange(0, 100)
        vol_slider.setValue(int(raw_vol * 100))
        vol_slider.setFixedWidth(200)
        vol_slider.setStyleSheet(
            "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.12);border-radius:2px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-6px 0;border-radius:8px;background:#5EEAD4;}"
            "QSlider::sub-page:horizontal{background:#5EEAD4;border-radius:2px;}"
        )
        vol_pct = QLabel(f"{int(raw_vol * 100)}%")
        vol_pct.setStyleSheet("color: #E8E8EA; font-size: 13px;")
        vol_pct.setFixedWidth(40)
        vol_slider.valueChanged.connect(lambda v: vol_pct.setText(f"{v}%"))
        self._widgets['tts_volume_slider'] = vol_slider

        vol_container = QWidget()
        vol_h = QHBoxLayout(vol_container)
        vol_h.setContentsMargins(0, 0, 0, 0)
        vol_h.setSpacing(8)
        vol_h.addWidget(vol_slider)
        vol_h.addWidget(vol_pct)
        layout.addLayout(self._setting_row(
            "Volume",
            "TTS speech volume (0 = silent, 100 = full)",
            vol_container,
        ))
        layout.addSpacing(16)

        # ---- Audio ducking --------------------------------------------------
        layout.addWidget(self._section_title("Audio Ducking"))
        layout.addSpacing(4)

        duck_desc = QLabel(
            "Reduce background audio while Ava is speaking so her voice is clearly audible."
        )
        duck_desc.setWordWrap(True)
        duck_desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(duck_desc)
        layout.addSpacing(6)

        duck_enabled = QCheckBox("Duck audio while Ava is speaking")
        duck_enabled.setChecked(bool(ac_cfg.get('enabled', True)))
        self._widgets['tts_duck_enabled'] = duck_enabled
        layout.addWidget(duck_enabled)
        layout.addSpacing(8)

        raw_duck = float(ac_cfg.get('duck_factor', 0.7))
        duck_slider = QSlider(Qt.Orientation.Horizontal)
        duck_slider.setRange(0, 100)
        duck_slider.setValue(int(raw_duck * 100))
        duck_slider.setFixedWidth(200)
        duck_slider.setStyleSheet(
            "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.12);border-radius:2px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-6px 0;border-radius:8px;background:#5EEAD4;}"
            "QSlider::sub-page:horizontal{background:#5EEAD4;border-radius:2px;}"
        )
        duck_pct = QLabel(f"{int(raw_duck * 100)}%")
        duck_pct.setStyleSheet("color: #E8E8EA; font-size: 13px;")
        duck_pct.setFixedWidth(40)
        duck_slider.valueChanged.connect(lambda v: duck_pct.setText(f"{v}%"))
        self._widgets['tts_duck_slider'] = duck_slider

        duck_container = QWidget()
        duck_h = QHBoxLayout(duck_container)
        duck_h.setContentsMargins(0, 0, 0, 0)
        duck_h.setSpacing(8)
        duck_h.addWidget(duck_slider)
        duck_h.addWidget(duck_pct)
        layout.addLayout(self._setting_row(
            "Duck level",
            "How much to reduce other audio (100% = silent others, 0% = no reduction)",
            duck_container,
        ))
        layout.addSpacing(16)

        # ---- Test -----------------------------------------------------------
        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        test_btn = QPushButton("Test Voice")
        test_btn.setFixedWidth(120)
        test_btn.clicked.connect(self._test_tts)
        test_status = QLabel("")
        test_status.setStyleSheet("color: #8A8A92; font-size: 12px;")
        self._widgets['tts_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        layout.addSpacing(16)

        # ---- When to speak (collapsible) ------------------------------------
        when_toggle = QPushButton("When should Samsara speak?  ▶")
        when_toggle.setStyleSheet(
            "QPushButton{background:transparent;color:#8A8A92;border:none;"
            "font-size:13px;text-align:left;padding:0;}"
            "QPushButton:hover{color:#E8E8EA;}"
        )
        layout.addWidget(when_toggle)

        when_widget = QWidget()
        when_widget.setVisible(False)
        when_layout = QVBoxLayout(when_widget)
        when_layout.setContentsMargins(0, 8, 0, 0)
        when_layout.setSpacing(6)

        phase_note = QLabel(
            "These settings are saved to config but Phase 2 category-driven "
            "behavior is required for them to take full effect."
        )
        phase_note.setWordWrap(True)
        phase_note.setStyleSheet("color: #8A8A92; font-size: 12px;")
        when_layout.addWidget(phase_note)

        _WHEN_TOGGLES = [
            ('tts_use_agent',    "Speak agent responses",           'use_for_agent_responses',    True),
            ('tts_use_confirm',  "Speak confirmations",             'use_for_confirmations',      True),
            ('tts_use_warnings', "Speak warnings",                  'use_for_warnings',           True),
            ('tts_use_status',   "Speak status updates (Thinking)", 'use_for_status_updates',     True),
            ('tts_use_readback', "Speak dictation readback",        'use_for_dictation_readback', False),
            ('tts_use_errors',   "Speak errors",                    'use_for_errors',             True),
        ]
        for wkey, label_text, cfg_key, default in _WHEN_TOGGLES:
            cb = QCheckBox(label_text)
            cb.setChecked(bool(cfg.get(cfg_key, default)))
            self._widgets[wkey] = cb
            when_layout.addWidget(cb)

        layout.addWidget(when_widget)

        def _toggle_when():
            vis = not when_widget.isVisible()
            when_widget.setVisible(vis)
            when_toggle.setText(
                "When should Samsara speak?  " + ("▼" if vis else "▶")
            )
        when_toggle.clicked.connect(_toggle_when)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _test_tts(self) -> None:
        coordinator = getattr(self.app, 'audio_coordinator', None)
        status = self._widgets.get('tts_test_status')
        if coordinator is None:
            if status:
                status.setText("TTS not initialized — restart with TTS enabled.")
            return
        voice_combo = self._widgets.get('tts_voice_combo')
        voice_label = voice_combo.currentText() if voice_combo else None
        voice_id = self._widgets.get('tts_voice_label_to_id', {}).get(voice_label)
        speed  = self._widgets['tts_speed'].value() if 'tts_speed' in self._widgets else 1.0
        volume = (self._widgets['tts_volume_slider'].value() / 100.0
                  if 'tts_volume_slider' in self._widgets else 0.8)
        _PHRASE = "Note saved. Your reminder will be in the brain dump."
        try:
            coordinator.speak(
                _PHRASE, voice_id=voice_id, speed=speed, volume=volume, category="general"
            )
            if status:
                status.setText(f'Speaking: "{_PHRASE}"')
        except Exception as exc:
            if status:
                status.setText(f"Error: {exc}")

    # ------------------------------------------------------------------
    # Alarms tab
    # ------------------------------------------------------------------

    def _build_alarms_tab(self):
        from samsara.alarms import get_default_alarm_config

        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 12)
        layout.setSpacing(8)

        alarm_cfg = self.app.config.get('alarms', get_default_alarm_config()) or {}

        # ---- Section: Global Settings ---------------------------------------
        layout.addWidget(self._section_title("Alarm Settings"))
        layout.addSpacing(4)

        alarms_enabled = QCheckBox("Enable alarm reminders")
        alarms_enabled.setChecked(bool(alarm_cfg.get('enabled', True)))
        self._widgets['alarms_enabled'] = alarms_enabled
        layout.addWidget(alarms_enabled)
        layout.addSpacing(8)

        complete_key = _HotkeyButton(alarm_cfg.get('complete_hotkey', 'f7'))
        self._widgets['alarms_complete_key'] = complete_key
        layout.addLayout(self._setting_row(
            "Complete hotkey",
            "Press to mark the alarm complete — counts toward your streak",
            complete_key,
        ))
        layout.addSpacing(6)

        dismiss_key = _HotkeyButton(alarm_cfg.get('dismiss_hotkey', 'f8'))
        self._widgets['alarms_dismiss_key'] = dismiss_key
        layout.addLayout(self._setting_row(
            "Dismiss hotkey",
            "Press to silence without streak credit",
            dismiss_key,
        ))
        layout.addSpacing(6)

        nag_spin = QSpinBox()
        nag_spin.setRange(15, 300)
        nag_spin.setSingleStep(15)
        nag_spin.setSuffix(" s")
        nag_spin.setValue(int(alarm_cfg.get('nag_interval_seconds', 60)))
        self._widgets['alarms_nag'] = nag_spin
        layout.addLayout(self._setting_row(
            "Repeat interval",
            "How often to replay the alarm sound until completed or dismissed",
            nag_spin,
        ))
        layout.addSpacing(12)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: rgba(255,255,255,0.06); max-height: 1px;")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # ---- Section: Alarm List --------------------------------------------
        layout.addWidget(self._section_title("Your Alarms"))
        layout.addSpacing(4)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(['On', 'Name', 'Interval', 'Streak'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setMinimumHeight(200)
        self._widgets['alarms_table'] = table
        self._populate_alarms_table(table)
        layout.addWidget(table, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        _SEC = (
            "QPushButton{background-color:transparent;color:#8A8A92;"
            "border:1px solid rgba(255,255,255,0.14);border-radius:6px;padding:7px 12px;}"
            "QPushButton:hover{background-color:rgba(255,255,255,0.05);color:#E8E8EA;}"
        )

        add_btn = QPushButton("Add Alarm")
        add_btn.setFixedWidth(100)
        add_btn.clicked.connect(lambda: self._open_alarm_dialog(None, table))
        btn_row.addWidget(add_btn)

        for label, width, handler in [
            ("Edit",      65, lambda: self._edit_selected_alarm(table)),
            ("Toggle",    65, lambda: self._toggle_selected_alarm(table)),
            ("Test",      55, lambda: self._test_selected_alarm(table)),
        ]:
            b = QPushButton(label)
            b.setFixedWidth(width)
            b.setStyleSheet(_SEC)
            b.clicked.connect(handler)
            btn_row.addWidget(b)

        del_btn = QPushButton("Delete")
        del_btn.setFixedWidth(65)
        del_btn.setStyleSheet(
            "QPushButton{background-color:rgba(200,60,60,0.15);color:#FF8888;"
            "border:1px solid rgba(200,60,60,0.3);border-radius:6px;padding:7px 12px;}"
            "QPushButton:hover{background-color:rgba(200,60,60,0.25);}"
        )
        del_btn.clicked.connect(lambda: self._delete_selected_alarm(table))
        btn_row.addWidget(del_btn)

        btn_row.addStretch()

        reset_btn = QPushButton("Reset Stats")
        reset_btn.setFixedWidth(90)
        reset_btn.setStyleSheet(_SEC)
        reset_btn.clicked.connect(lambda: self._reset_alarm_stats(table))
        btn_row.addWidget(reset_btn)

        layout.addLayout(btn_row)
        return outer

    # Alarms tab helpers

    def _populate_alarms_table(self, table: QTableWidget) -> None:
        from samsara.alarms import get_default_alarm_config
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        try:
            alarm_cfg = self.app.config.get('alarms', get_default_alarm_config()) or {}
            am = getattr(self.app, 'alarm_manager', None)
            for alarm in alarm_cfg.get('items', []):
                alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
                row = table.rowCount()
                table.insertRow(row)
                enabled_item = QTableWidgetItem(
                    "✓" if alarm.get('enabled', False) else "—"
                )
                enabled_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                enabled_item.setData(Qt.ItemDataRole.UserRole, alarm_id)
                table.setItem(row, 0, enabled_item)
                table.setItem(row, 1, QTableWidgetItem(alarm.get('name', 'Unnamed')))
                table.setItem(row, 2, QTableWidgetItem(
                    f"{alarm.get('interval_minutes', 60)} min"
                ))
                if am:
                    stats   = am.get_stats(alarm_id)
                    cur     = stats.get('current_streak', 0)
                    best    = stats.get('best_streak', 0)
                    streak  = f"{cur} / {best}" if (cur or best) else "—"
                else:
                    streak = "—"
                table.setItem(row, 3, QTableWidgetItem(streak))
        finally:
            table.setUpdatesEnabled(True)

    def _selected_alarm_id(self, table: QTableWidget):
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _edit_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to edit.")
            return
        self._open_alarm_dialog(alarm_id, table)

    def _delete_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to delete.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        alarm = am.get_alarm(alarm_id) if am else None
        name  = alarm.get('name', alarm_id) if alarm else alarm_id
        reply = QMessageBox.question(
            self, "Confirm Delete", f"Delete the alarm '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and am:
            am.remove_alarm(alarm_id)
            self._populate_alarms_table(table)

    def _toggle_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to toggle.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        if am:
            am.toggle_alarm(alarm_id)
            self._populate_alarms_table(table)

    def _test_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to test.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        if am:
            alarm = am.get_alarm(alarm_id)
            if alarm:
                threading.Thread(
                    target=lambda: am.play_sound(alarm), daemon=True
                ).start()

    def _reset_alarm_stats(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to reset.")
            return
        am    = getattr(self.app, 'alarm_manager', None)
        alarm = am.get_alarm(alarm_id) if am else None
        name  = alarm.get('name', alarm_id) if alarm else alarm_id
        reply = QMessageBox.question(
            self, "Reset Stats", f"Reset all streak stats for '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and am:
            am.reset_stats(alarm_id)
            self._populate_alarms_table(table)

    def _open_alarm_dialog(self, edit_id, table: QTableWidget) -> None:
        from samsara.alarms import get_default_alarm_config
        am       = getattr(self.app, 'alarm_manager', None)
        existing = (am.get_alarm(edit_id) or {}) if (edit_id and am) else {}

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Alarm" if edit_id else "Add Alarm")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(self.styleSheet())

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(20, 20, 20, 16)
        dlg_layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        name_edit = QLineEdit(existing.get('name', ''))
        name_edit.setPlaceholderText("e.g. Hydration")
        form.addRow("Name:", name_edit)

        interval_spin = QSpinBox()
        interval_spin.setRange(1, 480)
        interval_spin.setSuffix(" min")
        interval_spin.setValue(int(existing.get('interval_minutes', 60)))
        form.addRow("Interval:", interval_spin)

        sound_opts = ['alarm', 'chime', 'bell', 'gentle']
        if am:
            try:
                sound_opts = [s['value'] for s in am.get_available_sounds()]
            except Exception:
                pass
        sound_combo = QComboBox()
        sound_combo.addItems(sound_opts)
        current_snd = existing.get('sound', 'alarm')
        if current_snd in sound_opts:
            sound_combo.setCurrentText(current_snd)
        form.addRow("Sound:", sound_combo)

        enabled_cb = QCheckBox("Enabled")
        enabled_cb.setChecked(bool(existing.get('enabled', True)))
        form.addRow("", enabled_cb)

        dlg_layout.addLayout(form)

        btn_row2 = QHBoxLayout()
        btn_row2.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(80)
        cancel_btn.setStyleSheet(
            "QPushButton{background-color:transparent;color:#8A8A92;"
            "border:1px solid rgba(255,255,255,0.14);border-radius:6px;padding:8px 14px;}"
        )
        cancel_btn.clicked.connect(dlg.reject)
        btn_row2.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setFixedWidth(80)
        save_btn.clicked.connect(lambda: self._alarm_dialog_save(
            dlg, edit_id, am, table,
            name_edit, interval_spin, sound_combo, enabled_cb,
        ))
        btn_row2.addWidget(save_btn)
        dlg_layout.addLayout(btn_row2)
        dlg.exec()

    def _alarm_dialog_save(
        self, dlg, edit_id, am, table,
        name_edit, interval_spin, sound_combo, enabled_cb,
    ) -> None:
        from samsara.alarms import get_default_alarm_config
        name = name_edit.text().strip()
        if not name:
            QMessageBox.warning(dlg, "Error", "Alarm name is required.")
            return
        interval = interval_spin.value()
        sound    = sound_combo.currentText()
        enabled  = enabled_cb.isChecked()

        if am:
            if edit_id:
                am.update_alarm(edit_id, name=name, interval_minutes=interval,
                                sound=sound, enabled=enabled)
            else:
                am.add_alarm(name=name, interval_minutes=interval,
                             sound=sound, enabled=enabled)
        else:
            # Fallback: write directly to config when alarm_manager not running
            alarms_cfg = self.app.config.setdefault(
                'alarms', get_default_alarm_config()
            )
            items = alarms_cfg.setdefault('items', [])
            if edit_id:
                for item in items:
                    if item.get('id') == edit_id or item.get('name') == edit_id:
                        item.update({'name': name, 'interval_minutes': interval,
                                     'sound': sound, 'enabled': enabled})
                        break
            else:
                items.append({
                    'id':               name.lower().replace(' ', '_'),
                    'name':             name,
                    'interval_minutes': interval,
                    'sound':            sound,
                    'enabled':          enabled,
                })

        self._populate_alarms_table(table)
        dlg.accept()
        QMessageBox.information(self, "Saved", f"Alarm '{name}' saved.")

    def _build_health_tab(self):
        from datetime import datetime, timezone

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        def _fmt_time(ts):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local = dt.astimezone()
                h = local.hour % 12 or 12
                ampm = "AM" if local.hour < 12 else "PM"
                return f"{h}:{local.minute:02d} {ampm}"
            except Exception:
                return ts

        # ---- Section: Voice Command Reference ----
        layout.addWidget(self._section_title("Voice Commands"))
        layout.addSpacing(4)

        commands_info = [
            ("Pain tracking",    [
                ('"pain level 6"', "Log pain 1-10 with timestamp"),
                ('"pain level 4 knees"', "Log pain + body location"),
            ]),
            ("Medication",       [
                ('"took ibuprofen 400mg"', "Log medication + dose"),
                ('"took paracetamol"', "Log medication name only"),
            ]),
            ("Symptoms",         [
                ('"symptom hands are stiff"', "Freeform symptom note"),
                ('"I feel nauseous"', "Also triggers symptom log"),
            ]),
            ("Summaries",        [
                ('"health summary"', "Last 24h pain avg, meds, symptoms"),
                ('"how was my week"', "7-day summary via TTS"),
                ('"read health log"', "Read today's entries aloud"),
            ]),
            ("Management",       [
                ('"export health log"', "Save to CSV file"),
                ('"undo health log"', "Remove last entry"),
            ]),
        ]

        for group_name, cmds in commands_info:
            group_lbl = QLabel(group_name)
            group_lbl.setStyleSheet(
                "color: #E8E8EA; font-size: 13px; font-weight: 600; "
                "margin-top: 6px;"
            )
            layout.addWidget(group_lbl)
            for phrase, desc in cmds:
                row = QHBoxLayout()
                row.setContentsMargins(12, 1, 0, 1)
                p = QLabel(phrase)
                p.setStyleSheet(
                    "color: #5EEAD4; font-size: 12px; "
                    "font-family: 'Consolas', 'Courier New', monospace;"
                )
                p.setMinimumWidth(240)
                d = QLabel(desc)
                d.setStyleSheet("color: #8A8A92; font-size: 12px;")
                row.addWidget(p)
                row.addWidget(d, stretch=1)
                layout.addLayout(row)

        layout.addSpacing(20)

        # ---- Section: Today's Log ----
        layout.addWidget(self._section_title("Today's Log"))
        layout.addSpacing(4)

        self._health_count_label = QLabel("Loading...")
        self._health_count_label.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(self._health_count_label)
        layout.addSpacing(4)

        self._health_log_table = QTableWidget()
        self._health_log_table.setColumnCount(3)
        self._health_log_table.setHorizontalHeaderLabels(["Time", "Type", "Detail"])
        self._health_log_table.horizontalHeader().setStretchLastSection(True)
        self._health_log_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._health_log_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self._health_log_table.verticalHeader().setVisible(False)
        self._health_log_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._health_log_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._health_log_table.setMinimumHeight(160)
        self._health_log_table.setMaximumHeight(260)
        layout.addWidget(self._health_log_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(100)
        refresh_btn.clicked.connect(self._refresh_health_log)
        layout.addWidget(refresh_btn)
        layout.addSpacing(20)

        # ---- Section: Summary ----
        layout.addWidget(self._section_title("Summary"))
        layout.addSpacing(4)

        self._health_summary_label = QLabel("")
        self._health_summary_label.setWordWrap(True)
        self._health_summary_label.setStyleSheet(
            "color: #E8E8EA; font-size: 13px; line-height: 1.5;"
        )
        layout.addWidget(self._health_summary_label)
        layout.addSpacing(20)

        # ---- Section: Export ----
        layout.addWidget(self._section_title("Export"))
        layout.addSpacing(4)

        export_row = QHBoxLayout()
        export_btn = QPushButton("Export to CSV")
        export_btn.setFixedWidth(140)
        export_btn.clicked.connect(self._export_health_csv)
        export_row.addWidget(export_btn)

        open_folder_btn = QPushButton("Open folder")
        open_folder_btn.setFixedWidth(120)
        open_folder_btn.clicked.connect(self._open_health_folder)
        export_row.addWidget(open_folder_btn)
        export_row.addStretch()
        layout.addLayout(export_row)

        self._health_export_label = QLabel("")
        self._health_export_label.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(self._health_export_label)
        layout.addSpacing(20)

        # ---- Section: Medication Dictionary ----
        layout.addWidget(self._section_title("Medication Dictionary"))
        layout.addSpacing(4)

        dict_desc = QLabel(
            "Import common medication names into Whisper's vocabulary so it "
            "recognizes them when you say \"took ibuprofen\" or \"took pregabalin\". "
            "This adds ~100 medication names to the speech recognition prompt."
        )
        dict_desc.setWordWrap(True)
        dict_desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(dict_desc)
        layout.addSpacing(6)

        dict_row = QHBoxLayout()
        load_dict_btn = QPushButton("Load Medication Dictionary")
        load_dict_btn.setFixedWidth(220)
        load_dict_btn.clicked.connect(self._load_medication_dictionary)
        dict_row.addWidget(load_dict_btn)
        dict_row.addStretch()
        layout.addLayout(dict_row)

        self._med_dict_label = QLabel("")
        self._med_dict_label.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(self._med_dict_label)

        layout.addStretch()
        scroll.setWidget(container)

        # Populate on build
        self._refresh_health_log()

        return scroll

    def _refresh_health_log(self):
        from datetime import datetime, timezone
        try:
            from samsara import health_store
            entries = health_store.get_today()
        except Exception:
            entries = []

        def _fmt_time(ts):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local = dt.astimezone()
                h = local.hour % 12 or 12
                ampm = "AM" if local.hour < 12 else "PM"
                return f"{h}:{local.minute:02d} {ampm}"
            except Exception:
                return ts

        # Table
        self._health_log_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            t = e["type"]
            d = e["data"]
            time_str = _fmt_time(e["timestamp"])
            type_str = t.capitalize()
            if t == "pain":
                detail = f"Level {d.get('level', '?')}"
                if d.get("location"):
                    detail += f" - {d['location']}"
                if d.get("note"):
                    detail += f" ({d['note']})"
            elif t == "medication":
                detail = d.get("name", "unknown")
                if d.get("dose"):
                    detail += f" {d['dose']}"
            elif t == "symptom":
                detail = d.get("text", "")
            else:
                detail = str(d)

            self._health_log_table.setItem(i, 0, QTableWidgetItem(time_str))
            self._health_log_table.setItem(i, 1, QTableWidgetItem(type_str))
            self._health_log_table.setItem(i, 2, QTableWidgetItem(detail))

        n = len(entries)
        self._health_count_label.setText(
            f"{n} entr{'ies' if n != 1 else 'y'} today" if n else "No entries today"
        )

        # Summary
        try:
            from samsara import health_store
            pain_avg = health_store.get_pain_average(hours=24)
            week_avg = health_store.get_pain_average(hours=168)
            today_meds = health_store.get_by_type("medication", hours=24)
            today_symptoms = health_store.get_by_type("symptom", hours=24)
            today_pain = health_store.get_by_type("pain", hours=24)

            parts = []
            if pain_avg is not None:
                levels = [e["data"]["level"] for e in today_pain
                          if "level" in e["data"]]
                lo, hi = min(levels), max(levels)
                parts.append(
                    f"Today's pain: avg {pain_avg}, range {lo}-{hi} "
                    f"({len(levels)} readings)"
                )
            else:
                parts.append("No pain logged today.")

            if today_meds:
                names = {}
                for e in today_meds:
                    name = e["data"].get("name", "unknown")
                    names[name] = names.get(name, 0) + 1
                med_str = ", ".join(
                    f"{n} x{c}" if c > 1 else n for n, c in names.items()
                )
                parts.append(f"Medications: {med_str}")

            if today_symptoms:
                parts.append(f"Symptoms: {len(today_symptoms)} logged")

            if week_avg is not None:
                parts.append(f"Weekly avg pain: {week_avg}")

            self._health_summary_label.setText("\n".join(parts))
        except Exception as ex:
            self._health_summary_label.setText(f"Could not load summary: {ex}")

    def _export_health_csv(self):
        try:
            from samsara import health_store
            path = health_store.export_csv()
            self._health_export_label.setText(f"Exported to {path}")
        except Exception as ex:
            self._health_export_label.setText(f"Export failed: {ex}")

    def _open_health_folder(self):
        import os, subprocess
        folder = os.path.join(os.path.expanduser("~"), ".samsara")
        os.makedirs(folder, exist_ok=True)
        try:
            subprocess.Popen(["explorer", folder])
        except Exception:
            pass

    def _load_medication_dictionary(self):
        """Load the bundled medication dictionary into the vocabulary."""
        import json
        from pathlib import Path
        try:
            dict_path = Path(__file__).parent.parent.parent / "dictionaries" / "medications.json"
            if not dict_path.exists():
                self._med_dict_label.setText("Dictionary file not found.")
                self._med_dict_label.setStyleSheet("color: #c0392b; font-size: 12px;")
                return

            with open(dict_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            words = data.get("words", [])
            if not words:
                self._med_dict_label.setText("Dictionary is empty.")
                return

            vt = getattr(self.app, 'voice_training_window', None)
            if vt is None:
                self._med_dict_label.setText("Voice training not available.")
                return

            added = 0
            for word in words:
                w = word.strip()
                if w and w not in vt.custom_vocab:
                    vt.custom_vocab.append(w)
                    added += 1

            if added > 0:
                vt.save_training_data()

            total = len([w for w in words if w.strip() in vt.custom_vocab])
            self._med_dict_label.setText(
                f"Added {added} new terms ({total} total medication words in vocabulary)."
            )
            self._med_dict_label.setStyleSheet("color: #5EEAD4; font-size: 12px;")
        except Exception as ex:
            self._med_dict_label.setText(f"Error: {ex}")
            self._med_dict_label.setStyleSheet("color: #c0392b; font-size: 12px;")

    def _build_advanced_tab(self):
        try:
            from samsara.cuda_detect import is_cuda_available, cuda_status_message
            _cuda_ok  = is_cuda_available()
            _cuda_msg = cuda_status_message()
        except Exception:
            _cuda_ok  = False
            _cuda_msg = "CUDA detection unavailable."

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        cfg = self.app.config
        aec_cfg = cfg.get('echo_cancellation', {}) or {}

        # ---- Section: Hardware Acceleration --------------------------------
        layout.addWidget(self._section_title("Hardware Acceleration"))
        layout.addSpacing(4)

        device_display_to_value = {'CPU': 'cpu'}
        if _cuda_ok:
            device_display_to_value['CUDA (NVIDIA GPU)'] = 'cuda'
        device_value_to_display = {v: k for k, v in device_display_to_value.items()}

        current_device = cfg.get('device', 'cpu')
        if current_device == 'cuda' and not _cuda_ok:
            current_device = 'cpu'
        current_device_display = device_value_to_display.get(current_device, 'CPU')

        device_combo = QComboBox()
        device_combo.addItems(list(device_display_to_value.keys()))
        device_combo.setCurrentText(current_device_display)
        self._widgets['adv_device'] = device_combo
        self._widgets['adv_device_map'] = device_display_to_value
        layout.addLayout(self._setting_row(
            "Compute device",
            _cuda_msg + "  Device changes require restart.",
            device_combo,
        ))
        layout.addSpacing(8)

        compute_options = ['float16', 'int8', 'float32']
        current_compute = cfg.get('compute_type', 'float16')
        compute_combo = QComboBox()
        compute_combo.addItems(compute_options)
        if current_compute in compute_options:
            compute_combo.setCurrentText(current_compute)
        self._widgets['adv_compute_type'] = compute_combo
        layout.addLayout(self._setting_row(
            "Compute type",
            "float16: fastest on GPU.  int8: low-memory CPUs.  float32: fallback.",
            compute_combo,
        ))
        layout.addSpacing(20)

        # ---- Section: Performance ------------------------------------------
        layout.addWidget(self._section_title("Performance"))
        layout.addSpacing(4)

        perf_options = ['fast', 'balanced', 'accurate']
        current_perf = cfg.get('performance_mode', 'balanced')
        perf_combo = QComboBox()
        perf_combo.addItems(perf_options)
        if current_perf in perf_options:
            perf_combo.setCurrentText(current_perf)
        self._widgets['adv_perf_mode'] = perf_combo
        layout.addLayout(self._setting_row(
            "Performance mode",
            "fast: lowest latency.  balanced: good tradeoff.  accurate: best quality.",
            perf_combo,
        ))
        layout.addSpacing(20)

        # ---- Section: Continuous Mode --------------------------------------
        layout.addWidget(self._section_title("Continuous Mode"))
        layout.addSpacing(4)

        silence_spin = QDoubleSpinBox()
        silence_spin.setRange(0.5, 10.0)
        silence_spin.setSingleStep(0.5)
        silence_spin.setDecimals(1)
        silence_spin.setSuffix(" s")
        silence_spin.setValue(float(cfg.get('silence_threshold', 2.0)))
        self._widgets['adv_silence'] = silence_spin
        layout.addLayout(self._setting_row(
            "Silence threshold",
            "Seconds of silence before continuous mode auto-transcribes",
            silence_spin,
        ))
        layout.addSpacing(8)

        min_speech_spin = QDoubleSpinBox()
        min_speech_spin.setRange(0.1, 2.0)
        min_speech_spin.setSingleStep(0.1)
        min_speech_spin.setDecimals(1)
        min_speech_spin.setSuffix(" s")
        min_speech_spin.setValue(float(cfg.get('min_speech_duration', 0.3)))
        self._widgets['adv_min_speech'] = min_speech_spin
        layout.addLayout(self._setting_row(
            "Min speech duration",
            "Recordings shorter than this are discarded as noise",
            min_speech_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Speech Threshold -------------------------------------
        layout.addWidget(self._section_title("Speech Threshold"))
        layout.addSpacing(4)

        thresh_mode_combo = QComboBox()
        thresh_mode_combo.addItems(['auto', 'manual'])
        thresh_mode_combo.setCurrentText(cfg.get('threshold_mode', 'auto'))
        self._widgets['adv_threshold_mode'] = thresh_mode_combo
        layout.addLayout(self._setting_row(
            "Calibration mode",
            "auto: calibrate on startup (recommended).  manual: use a fixed threshold below.",
            thresh_mode_combo,
        ))
        layout.addSpacing(8)

        cal_spin = QDoubleSpinBox()
        cal_spin.setRange(1.0, 10.0)
        cal_spin.setSingleStep(0.1)
        cal_spin.setDecimals(1)
        cal_spin.setValue(float(cfg.get('cal_multiplier', 3.0)))
        self._widgets['adv_cal_multiplier'] = cal_spin
        layout.addLayout(self._setting_row(
            "Calibration multiplier",
            "Auto mode: signal must be this many times louder than ambient to count as speech",
            cal_spin,
        ))
        layout.addSpacing(8)

        # Manual threshold row — visible only when mode is 'manual'
        manual_row_widget = QWidget()
        manual_row_widget.setVisible(thresh_mode_combo.currentText() == 'manual')
        manual_row_layout = QVBoxLayout(manual_row_widget)
        manual_row_layout.setContentsMargins(0, 0, 0, 0)

        current_thresh = (
            cfg.get('wake_word_config', {}).get('audio', {}).get('speech_threshold', 0.03)
        )
        manual_spin = QDoubleSpinBox()
        manual_spin.setRange(0.005, 0.20)
        manual_spin.setSingleStep(0.005)
        manual_spin.setDecimals(4)
        manual_spin.setValue(float(current_thresh))
        self._widgets['adv_manual_threshold'] = manual_spin
        manual_row_layout.addLayout(self._setting_row(
            "Manual threshold",
            "Raw RMS amplitude level required to count as speech (0.005 – 0.20)",
            manual_spin,
        ))
        layout.addWidget(manual_row_widget)

        thresh_mode_combo.currentTextChanged.connect(
            lambda t: manual_row_widget.setVisible(t == 'manual')
        )
        layout.addSpacing(20)

        # ---- Section: Echo Cancellation ------------------------------------
        layout.addWidget(self._section_title("Echo Cancellation"))
        layout.addSpacing(4)

        aec_cb = QCheckBox("Enable echo cancellation (removes system audio from mic)")
        aec_cb.setChecked(bool(aec_cfg.get('enabled', False)))
        self._widgets['adv_aec_enabled'] = aec_cb
        layout.addWidget(aec_cb)

        aec_note = QLabel(
            "Filters out music/video audio so only your voice is transcribed. "
            "Requires restart. Windows only (WASAPI loopback)."
        )
        aec_note.setWordWrap(True)
        aec_note.setStyleSheet("color: #8A8A92; font-size: 12px; margin-left: 26px;")
        layout.addWidget(aec_note)
        layout.addSpacing(8)

        aec_latency_spin = QDoubleSpinBox()
        aec_latency_spin.setRange(0.0, 500.0)
        aec_latency_spin.setSingleStep(5.0)
        aec_latency_spin.setDecimals(0)
        aec_latency_spin.setSuffix(" ms")
        aec_latency_spin.setValue(float(aec_cfg.get('latency_ms', 30.0)))
        self._widgets['adv_aec_latency'] = aec_latency_spin
        layout.addLayout(self._setting_row(
            "Latency compensation",
            "How far back to look for the system audio that matches what the mic captured",
            aec_latency_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Listening Indicator ----------------------------------
        layout.addWidget(self._section_title("Listening Indicator"))
        layout.addSpacing(4)

        indicator_desc = QLabel(
            "An always-on-top pill that shows your current mode and pulses while recording."
        )
        indicator_desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        layout.addWidget(indicator_desc)
        layout.addSpacing(6)

        ind_cb = QCheckBox("Show listening indicator overlay")
        ind_cb.setChecked(bool(cfg.get('listening_indicator_enabled', False)))
        self._widgets['adv_indicator_enabled'] = ind_cb
        layout.addWidget(ind_cb)
        layout.addSpacing(8)

        pos_options = [
            'top-left', 'top-center', 'top-right',
            'bottom-left', 'bottom-center', 'bottom-right',
        ]
        current_pos = cfg.get('listening_indicator_position', 'bottom-center')
        pos_combo = QComboBox()
        pos_combo.addItems(pos_options)
        if current_pos in pos_options:
            pos_combo.setCurrentText(current_pos)
        self._widgets['adv_indicator_pos'] = pos_combo
        layout.addLayout(self._setting_row(
            "Indicator position",
            "Screen edge where the indicator pill is anchored",
            pos_combo,
        ))

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_ava_cloud_tab(self):
        from samsara import premium

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        key = premium.get_license_key(self.app)
        has_license = premium.validate_key(key)

        # ---- License header ------------------------------------------------
        layout.addWidget(self._section_title("Cloud AI — Premium Feature"))
        layout.addSpacing(4)

        lic_frame = QFrame()
        lic_frame.setStyleSheet(
            "QFrame { background-color: #111114; border-radius: 8px; "
            "border: 1px solid rgba(255,255,255,0.06); }"
        )
        lic_frame_layout = QVBoxLayout(lic_frame)
        lic_frame_layout.setContentsMargins(16, 16, 16, 16)
        lic_frame_layout.setSpacing(0)

        # QStackedWidget: index 0 = unlicensed, index 1 = licensed
        lic_stack = QStackedWidget()
        lic_stack.setStyleSheet("background: transparent;")
        self._widgets['cloud_license_stack'] = lic_stack

        # -- Page 0: unlicensed ---
        unlicensed_page = QWidget()
        unlicensed_page.setStyleSheet("background: transparent;")
        up_layout = QVBoxLayout(unlicensed_page)
        up_layout.setContentsMargins(0, 0, 0, 0)
        up_layout.setSpacing(10)

        exp_text = (
            "Samsara is free. Every voice command, dictation feature, plugin, and the "
            "local AI assistant are yours with no restrictions, no trial period, no nag screens.\n\n"
            "Cloud AI connects Ava to larger language models for more capable conversations "
            "and smarter command interpretation. Revenue from Cloud AI licenses helps fund "
            "Samsara's continued development as a free accessibility tool.\n\n"
            "If you need Samsara for accessibility and genuinely cannot afford a license, "
            "get in touch at morneis.com/samsara/business — we'll work something out."
        )
        exp_label = QLabel(exp_text)
        exp_label.setWordWrap(True)
        exp_label.setStyleSheet("color: #8A8A92; font-size: 12px; background: transparent;")
        up_layout.addWidget(exp_label)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row_lbl = QLabel("License key:")
        key_row_lbl.setStyleSheet("color: #E8E8EA; font-size: 13px; background: transparent;")
        key_row_lbl.setFixedWidth(95)
        license_entry = QLineEdit()
        license_entry.setPlaceholderText("SAMSARA-XXXX-XXXX-XXXX")
        self._widgets['cloud_license_entry'] = license_entry
        activate_btn = QPushButton("Activate")
        activate_btn.setFixedWidth(90)
        activate_btn.clicked.connect(self._activate_license)
        key_row.addWidget(key_row_lbl)
        key_row.addWidget(license_entry, stretch=1)
        key_row.addWidget(activate_btn)
        up_layout.addLayout(key_row)

        license_status = QLabel("")
        license_status.setStyleSheet("color: #FF6666; font-size: 12px; background: transparent;")
        self._widgets['cloud_license_status'] = license_status
        up_layout.addWidget(license_status)

        link_lbl = QLabel("Get a license at morneis.com/samsara/premium")
        link_lbl.setStyleSheet("color: #5EEAD4; font-size: 12px; background: transparent;")
        up_layout.addWidget(link_lbl)

        lic_stack.addWidget(unlicensed_page)  # index 0

        # -- Page 1: licensed ---
        licensed_page = QWidget()
        licensed_page.setStyleSheet("background: transparent;")
        lp_layout = QVBoxLayout(licensed_page)
        lp_layout.setContentsMargins(0, 0, 0, 0)
        lp_layout.setSpacing(6)

        active_lbl = QLabel("Premium license active")
        active_lbl.setStyleSheet(
            "color: #5EEAD4; font-size: 13px; font-weight: bold; background: transparent;"
        )
        lp_layout.addWidget(active_lbl)

        masked_lbl = QLabel(premium.masked_key(key) if has_license else "")
        masked_lbl.setStyleSheet(
            "color: #8A8A92; font-size: 11px; "
            "font-family: 'Consolas', 'Courier New', monospace; background: transparent;"
        )
        self._widgets['cloud_masked_key'] = masked_lbl
        lp_layout.addWidget(masked_lbl)

        remove_btn = QPushButton("Remove License")
        remove_btn.setFixedWidth(140)
        remove_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 7px 14px; font-size: 13px; }"
            "QPushButton:hover { background-color: rgba(255,255,255,0.04); color: #E8E8EA; }"
        )
        remove_btn.clicked.connect(self._remove_license)
        lp_layout.addWidget(remove_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        lic_stack.addWidget(licensed_page)  # index 1

        lic_stack.setCurrentIndex(1 if has_license else 0)
        lic_frame_layout.addWidget(lic_stack)
        layout.addWidget(lic_frame)

        # ---- Cloud settings (hidden when unlicensed) -----------------------
        cloud_settings = QWidget()
        cloud_settings.setVisible(has_license)
        self._widgets['cloud_settings_widget'] = cloud_settings
        cs_layout = QVBoxLayout(cloud_settings)
        cs_layout.setContentsMargins(0, 8, 0, 0)
        cs_layout.setSpacing(12)

        cfg = self.app.config.get('cloud_llm', {}) or {}

        # Enable
        cloud_enabled = QCheckBox("Enable Cloud LLM (Ava routes requests to the cloud provider)")
        cloud_enabled.setChecked(bool(cfg.get('enabled', False)))
        self._widgets['cloud_enabled'] = cloud_enabled
        cs_layout.addWidget(cloud_enabled)

        enable_note = QLabel(
            "When enabled, voice requests are sent to the selected provider. "
            "Falls back to local Ollama on error."
        )
        enable_note.setWordWrap(True)
        enable_note.setStyleSheet("color: #8A8A92; font-size: 12px; margin-left: 26px;")
        cs_layout.addWidget(enable_note)
        cs_layout.addSpacing(8)

        # Provider
        cs_layout.addWidget(self._section_title("Provider"))
        cs_layout.addSpacing(4)

        current_provider = cfg.get('provider', 'deepseek')
        current_display = _CODE_TO_DISPLAY.get(current_provider, _PROVIDER_DISPLAY[0])
        provider_combo = QComboBox()
        provider_combo.addItems(_PROVIDER_DISPLAY)
        provider_combo.setCurrentText(current_display)
        self._widgets['cloud_provider'] = provider_combo
        cs_layout.addLayout(self._setting_row(
            "Provider",
            "Cloud AI provider that processes your voice requests",
            provider_combo,
        ))

        # Info card
        info_card = QFrame()
        info_card.setStyleSheet(
            "QFrame { background-color: #111114; border-radius: 6px; "
            "border: 1px solid rgba(255,255,255,0.06); }"
        )
        info_card_layout = QVBoxLayout(info_card)
        info_card_layout.setContentsMargins(12, 10, 12, 10)
        info_label = QLabel(_PROVIDER_INFO.get(current_provider, ""))
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #8A8A92; font-size: 12px; background: transparent;")
        info_card_layout.addWidget(info_label)
        self._widgets['cloud_info_label'] = info_label
        cs_layout.addWidget(info_card)

        provider_combo.currentTextChanged.connect(self._on_cloud_provider_changed)
        cs_layout.addSpacing(8)

        # API Key (entry + show/hide button as a container widget)
        api_key_container = QWidget()
        ak_layout = QHBoxLayout(api_key_container)
        ak_layout.setContentsMargins(0, 0, 0, 0)
        ak_layout.setSpacing(6)
        api_key_entry = QLineEdit()
        api_key_entry.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_entry.setText(cfg.get('api_key', ''))
        api_key_entry.setPlaceholderText("Paste your API key here")
        api_key_entry.setMinimumWidth(260)
        self._widgets['cloud_api_key'] = api_key_entry
        show_btn = QPushButton("Show")
        show_btn.setCheckable(True)
        show_btn.setFixedWidth(60)
        show_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #8A8A92; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 6px 10px; font-size: 12px; }"
            "QPushButton:hover { color: #E8E8EA; }"
            "QPushButton:checked { border-color: rgba(94,234,212,0.4); color: #5EEAD4; }"
        )
        show_btn.toggled.connect(
            lambda checked: self._toggle_api_key_show(checked, api_key_entry, show_btn)
        )
        ak_layout.addWidget(api_key_entry, stretch=1)
        ak_layout.addWidget(show_btn)
        cs_layout.addLayout(self._setting_row(
            "API Key",
            "Stored locally in config.json only — never transmitted to Samsara servers.",
            api_key_container,
        ))
        cs_layout.addSpacing(8)

        # Model override
        current_model = cfg.get('model', '')
        model_entry = QLineEdit()
        model_entry.setText(current_model)
        model_entry.setPlaceholderText(
            f"Default: {_DEFAULT_MODELS.get(current_provider, '')}"
        )
        model_entry.setMinimumWidth(220)
        self._widgets['cloud_model'] = model_entry
        cs_layout.addLayout(self._setting_row(
            "Model override",
            "Leave blank to use the provider's default model shown in the placeholder",
            model_entry,
        ))
        cs_layout.addSpacing(8)

        # Timeout
        timeout_spin = QSpinBox()
        timeout_spin.setRange(5, 120)
        timeout_spin.setSingleStep(5)
        timeout_spin.setSuffix(" s")
        timeout_spin.setValue(int(cfg.get('timeout_seconds', 30)))
        self._widgets['cloud_timeout'] = timeout_spin
        cs_layout.addLayout(self._setting_row(
            "Timeout",
            "Seconds to wait for the cloud provider before showing an error",
            timeout_spin,
        ))
        cs_layout.addSpacing(12)

        # Test connection
        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        test_btn = QPushButton("Test Connection")
        test_btn.setFixedWidth(150)
        test_btn.clicked.connect(self._run_test_connection)
        test_status = QLabel("")
        test_status.setStyleSheet("color: #8A8A92; font-size: 12px;")
        self._widgets['cloud_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        cs_layout.addLayout(test_row)
        cs_layout.addSpacing(8)

        # Privacy notice
        privacy = QLabel(
            "Your API key is stored locally in config.json and is never logged, "
            "printed, or transmitted to Samsara servers. It is only sent directly "
            "to your chosen cloud provider when you make a request."
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet(
            "color: #E89020; font-size: 12px; "
            "background-color: rgba(232,144,32,0.07); "
            "border: 1px solid rgba(232,144,32,0.2); "
            "border-radius: 6px; padding: 10px 12px;"
        )
        cs_layout.addWidget(privacy)

        layout.addWidget(cloud_settings)
        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Ava / Cloud tab slots and helpers
    # ------------------------------------------------------------------

    def _provider_info(self, display_name: str) -> str:
        code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
        return _PROVIDER_INFO.get(code, "")

    def _on_cloud_provider_changed(self, display_name: str):
        info_label = self._widgets.get('cloud_info_label')
        if info_label:
            info_label.setText(self._provider_info(display_name))
        model_entry = self._widgets.get('cloud_model')
        if model_entry:
            code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
            model_entry.setPlaceholderText(f"Default: {_DEFAULT_MODELS.get(code, '')}")

    def _activate_license(self):
        from samsara import premium
        entry = self._widgets.get('cloud_license_entry')
        if not entry:
            return
        key = entry.text().strip()
        status_lbl = self._widgets.get('cloud_license_status')
        if not premium.validate_key(key):
            if status_lbl:
                status_lbl.setText("Invalid key format. Expected: SAMSARA-XXXX-XXXX-XXXX")
            return
        premium.set_license_key(self.app, key)
        with self.app._config_lock:
            self.app.config['premium_license'] = key
            self.app.save_config()
        # Switch license panel to licensed state
        stack = self._widgets.get('cloud_license_stack')
        if stack:
            stack.setCurrentIndex(1)
        masked_lbl = self._widgets.get('cloud_masked_key')
        if masked_lbl:
            masked_lbl.setText(premium.masked_key(key))
        cloud_widget = self._widgets.get('cloud_settings_widget')
        if cloud_widget:
            cloud_widget.setVisible(True)
        if status_lbl:
            status_lbl.setText("")

    def _remove_license(self):
        from samsara import premium
        premium.set_license_key(self.app, "")
        cfg = dict(self.app.config.get('cloud_llm', {}) or {})
        cfg['enabled'] = False
        with self.app._config_lock:
            self.app.config['premium_license'] = ""
            self.app.config['cloud_llm'] = cfg
            self.app.save_config()
        stack = self._widgets.get('cloud_license_stack')
        if stack:
            stack.setCurrentIndex(0)
        cloud_widget = self._widgets.get('cloud_settings_widget')
        if cloud_widget:
            cloud_widget.setVisible(False)
        entry = self._widgets.get('cloud_license_entry')
        if entry:
            entry.clear()
        status_lbl = self._widgets.get('cloud_license_status')
        if status_lbl:
            status_lbl.setText("")

    @staticmethod
    def _toggle_api_key_show(checked: bool, entry: QLineEdit, btn: QPushButton):
        entry.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        btn.setText("Hide" if checked else "Show")

    def _run_test_connection(self):
        from samsara import premium
        if not premium.is_premium(self.app):
            return
        api_key_entry = self._widgets.get('cloud_api_key')
        api_key = api_key_entry.text().strip() if api_key_entry else ""
        if not api_key:
            self._test_result.emit("No API key entered.", "#E89020")
            return
        provider_combo = self._widgets.get('cloud_provider')
        provider_display = provider_combo.currentText() if provider_combo else _PROVIDER_DISPLAY[0]
        provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
        self._test_result.emit("Testing...", "#8A8A92")

        class _FakeApp:
            config = {"cloud_llm": {
                "enabled": True, "api_key": api_key,
                "provider": provider, "timeout_seconds": 5,
            }}

        fake = _FakeApp()

        def _do():
            try:
                from samsara import cloud_llm
                ok, info = cloud_llm.check_available(fake)
                msg = f"Connected to {provider}." if ok else f"Failed: {info}"
                color = "#5EEAD4" if ok else "#FF6666"
            except Exception as exc:
                msg = f"Error: {exc}"
                color = "#FF6666"
            self._test_result.emit(msg, color)

        threading.Thread(target=_do, daemon=True).start()

    def _on_test_result(self, msg: str, color: str):
        label = self._widgets.get('cloud_test_status')
        if label:
            label.setText(msg)
            label.setStyleSheet(f"color: {color}; font-size: 12px;")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _section_title(self, text):
        label = QLabel(text)
        label.setStyleSheet("color: #5EEAD4; font-size: 16px; font-weight: bold;")
        return label

    def _setting_row(self, label, description, widget):
        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(2)

        lbl = QLabel(label)
        lbl.setStyleSheet("font-weight: 600; font-size: 14px; color: #E8E8EA;")
        left.addWidget(lbl)

        desc = QLabel(description)
        desc.setStyleSheet("color: #8A8A92; font-size: 12px;")
        desc.setWordWrap(True)
        left.addWidget(desc)

        row.addLayout(left, stretch=1)
        row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)
        return row

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _apply_and_close(self):
        updates = {}

        # General tab
        updates.update({
            'auto_paste':         self._widgets['auto_paste'].isChecked(),
            'add_trailing_space': self._widgets['trailing_space'].isChecked(),
            'auto_capitalize':    self._widgets['auto_capitalize'].isChecked(),
            'format_numbers':     self._widgets['format_numbers'].isChecked(),
            'cleanup_mode':       self._widgets['cleanup_mode'].currentText(),
            'model_size':         self._widgets['model_combo'].currentText(),
            'language':           self._widgets['lang_name_to_code'].get(
                                      self._widgets['lang_combo'].currentText(), 'en'),
            'hints_enabled':      self._widgets['hints_enabled'].isChecked(),
        })

        # Sync hints enabled/disabled state on the live HintManager
        hints = getattr(self.app, 'hints', None)
        if hints is not None:
            if self._widgets['hints_enabled'].isChecked():
                hints._enabled = True
            else:
                hints._enabled = False

        mic_name = self._widgets['mic_combo'].currentText()
        mic_id = self._widgets['mic_names_to_id'].get(mic_name)
        if mic_id is not None:
            updates['microphone'] = mic_id
            updates['microphone_name'] = mic_name

        # Hotkeys tab — flat hotkey keys
        for key in ('hotkey', 'continuous_hotkey', 'wake_word_hotkey',
                    'command_hotkey', 'streaming_hotkey', 'cancel_hotkey',
                    'undo_hotkey', 'ava_mode_key'):
            btn = self._widgets.get(key)
            if isinstance(btn, _HotkeyButton):
                updates[key] = btn.combo

        # Recording mode + wake word enable
        if 'mode' in self._widgets:
            updates['mode'] = self._widgets['mode'].currentText()
        if 'wake_word_enabled' in self._widgets:
            updates['wake_word_enabled'] = self._widgets['wake_word_enabled'].isChecked()

        # Wake word nested config
        if 'threshold_mode' in self._widgets:
            updates['threshold_mode'] = self._widgets['threshold_mode'].currentText()
            updates['cal_multiplier'] = self._widgets['cal_multiplier'].value()
            ww_cfg = dict(self.app.config.get('wake_word_config', {}) or {})
            ww_audio = dict(ww_cfg.get('audio', {}) or {})
            ww_audio['wake_command_timeout'] = self._widgets['wake_cmd_timeout'].value()
            ww_cfg['audio'] = ww_audio
            ww_cfg['quick_silence_timeout'] = self._widgets['quick_silence'].value()
            ww_cfg['oww_threshold'] = max(0.01, min(1.0, self._widgets['oww_threshold'].value()))
            updates['wake_word_config'] = ww_cfg

        # Command mode nested config
        if 'cmd_mode' in self._widgets:
            cmd_cfg = dict(self.app.config.get('command_mode', {}) or {})
            cmd_cfg['mode'] = self._widgets['cmd_mode'].currentText()
            cmd_cfg['button'] = self._widgets['cmd_button'].currentText()
            cmd_cfg['enter_debounce_ms'] = self._widgets['cmd_debounce'].value()
            cmd_cfg['inactivity_timeout_s'] = self._widgets['cmd_timeout'].value()
            cmd_cfg['miss_limit'] = self._widgets['cmd_miss_limit'].value()
            updates['command_mode'] = cmd_cfg

        # Commands tab — button + suppress merge into command_mode (after hotkeys tab write)
        if 'cmd_tab_button' in self._widgets:
            cmd_cfg = dict(updates.get('command_mode',
                           self.app.config.get('command_mode', {})) or {})
            btn_label = self._widgets['cmd_tab_button'].currentText()
            cmd_cfg['button'] = _CMD_BUTTON_OPTIONS.get(btn_label, 'mouse4')
            cmd_cfg['suppress_button'] = self._widgets['cmd_tab_suppress'].isChecked()
            updates['command_mode'] = cmd_cfg

        # Commands tab — pack enables
        if '_pack_checkboxes' in self._widgets:
            from samsara.command_packs import PACKS
            new_packs = dict(self.app.config.get('command_packs', {}) or {})
            for pack_id, cb in self._widgets['_pack_checkboxes'].items():
                meta = PACKS.get(pack_id, {})
                if not meta.get('always_on'):
                    new_packs[pack_id] = cb.isChecked()
            updates['command_packs'] = new_packs

        # TTS tab
        if 'tts_enabled' in self._widgets:
            tts_cfg = dict(self.app.config.get('tts', {}) or {})
            tts_cfg['enabled'] = self._widgets['tts_enabled'].isChecked()
            tts_cfg['engine']  = self._widgets['tts_engine'].currentText()
            voice_label = self._widgets['tts_voice_combo'].currentText()
            voice_id    = self._widgets.get('tts_voice_label_to_id', {}).get(voice_label)
            if voice_id:
                tts_cfg['voice_id'] = voice_id
            tts_cfg['speed']  = self._widgets['tts_speed'].value()
            tts_cfg['pitch']  = self._widgets['tts_pitch'].value()
            tts_cfg['volume'] = self._widgets['tts_volume_slider'].value() / 100.0
            for wkey, cfg_key in [
                ('tts_use_agent',    'use_for_agent_responses'),
                ('tts_use_confirm',  'use_for_confirmations'),
                ('tts_use_warnings', 'use_for_warnings'),
                ('tts_use_status',   'use_for_status_updates'),
                ('tts_use_readback', 'use_for_dictation_readback'),
                ('tts_use_errors',   'use_for_errors'),
            ]:
                if wkey in self._widgets:
                    tts_cfg[cfg_key] = self._widgets[wkey].isChecked()
            updates['tts'] = tts_cfg
            # Audio coordinator duck settings
            ac_cfg = dict(self.app.config.get('audio_coordinator', {}) or {})
            ac_cfg['enabled']     = self._widgets['tts_duck_enabled'].isChecked()
            ac_cfg['duck_factor'] = self._widgets['tts_duck_slider'].value() / 100.0
            updates['audio_coordinator'] = ac_cfg

        # Alarms tab
        if 'alarms_enabled' in self._widgets:
            from samsara.alarms import get_default_alarm_config
            alarms_cfg = dict(
                self.app.config.get('alarms', get_default_alarm_config()) or {}
            )
            alarms_cfg['enabled'] = self._widgets['alarms_enabled'].isChecked()
            for wkey, cfg_key in [
                ('alarms_complete_key', 'complete_hotkey'),
                ('alarms_dismiss_key',  'dismiss_hotkey'),
            ]:
                btn = self._widgets.get(wkey)
                if isinstance(btn, _HotkeyButton):
                    alarms_cfg[cfg_key] = btn.combo
            alarms_cfg['nag_interval_seconds'] = self._widgets['alarms_nag'].value()
            updates['alarms'] = alarms_cfg
            am = getattr(self.app, 'alarm_manager', None)
            if am:
                if alarms_cfg['enabled'] and not getattr(am, 'running', False):
                    am.start()
                elif not alarms_cfg['enabled'] and getattr(am, 'running', False):
                    am.stop()

        # Sounds tab
        if 'sound_feedback' in self._widgets:
            updates['audio_feedback'] = self._widgets['sound_feedback'].isChecked()
            updates['sound_volume'] = self._widgets['sound_volume_slider'].value() / 100.0
            updates['sound_theme'] = self._widgets['sound_theme_combo'].currentText()

        # Cloud LLM (only when licensed and tab was built)
        if 'cloud_enabled' in self._widgets:
            from samsara import premium
            if premium.is_premium(self.app):
                provider_display = self._widgets['cloud_provider'].currentText()
                provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
                api_key = self._widgets['cloud_api_key'].text().strip()
                model_override = self._widgets['cloud_model'].text().strip()
                cfg = dict(self.app.config.get('cloud_llm', {}) or {})
                cfg['enabled'] = self._widgets['cloud_enabled'].isChecked()
                cfg['provider'] = provider
                cfg['api_key'] = api_key
                cfg['timeout_seconds'] = self._widgets['cloud_timeout'].value()
                if model_override:
                    cfg['model'] = model_override
                elif 'model' in cfg:
                    del cfg['model']
                updates['cloud_llm'] = cfg

        # Advanced tab
        if 'adv_device' in self._widgets:
            device_map  = self._widgets['adv_device_map']
            device_disp = self._widgets['adv_device'].currentText()
            updates['device']            = device_map.get(device_disp, 'cpu')
            updates['compute_type']      = self._widgets['adv_compute_type'].currentText()
            updates['performance_mode']  = self._widgets['adv_perf_mode'].currentText()
            updates['silence_threshold'] = self._widgets['adv_silence'].value()
            updates['min_speech_duration'] = self._widgets['adv_min_speech'].value()
            updates['threshold_mode']    = self._widgets['adv_threshold_mode'].currentText()
            updates['cal_multiplier']    = self._widgets['adv_cal_multiplier'].value()
            updates['echo_cancellation'] = {
                'enabled':    self._widgets['adv_aec_enabled'].isChecked(),
                'latency_ms': self._widgets['adv_aec_latency'].value(),
            }
            updates['listening_indicator_enabled']  = (
                self._widgets['adv_indicator_enabled'].isChecked()
            )
            updates['listening_indicator_position'] = (
                self._widgets['adv_indicator_pos'].currentText()
            )
            # Apply manual threshold to wake_word_config if in manual mode
            if self._widgets['adv_threshold_mode'].currentText() == 'manual':
                ww_cfg = dict(self.app.config.get('wake_word_config', {}) or {})
                ww_audio = dict(ww_cfg.get('audio', {}) or {})
                val = self._widgets['adv_manual_threshold'].value()
                val = max(0.005, min(0.20, val))
                ww_audio['speech_threshold'] = val
                ww_cfg['audio'] = ww_audio
                # Merge with any existing wake_word_config update from Hotkeys tab
                existing_ww = updates.get('wake_word_config', ww_cfg)
                existing_ww_audio = dict(existing_ww.get('audio', {}) or {})
                existing_ww_audio['speech_threshold'] = val
                existing_ww['audio'] = existing_ww_audio
                updates['wake_word_config'] = existing_ww

        with self.app._config_lock:
            self.app.config.update(updates)
            self.app.save_config()

        self.close()
