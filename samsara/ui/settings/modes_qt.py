"""Modes page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
ModesPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from samsara import config_defaults, session_modes
from samsara.constants import (
    DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
    DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
    DEFAULT_WAKE_PHRASE,
    DEFAULT_WAKE_PHRASE_OPTIONS,
)
from samsara.ui import ava_consent_qt, theme

from samsara.ui.settings_qt import (
    _CMD_BUTTON_KEY_TO_LABEL,
    _CMD_BUTTON_OPTIONS,
    _CONTENT_MAX_WIDTH,
    _HotkeyButton,
)


_AI_CMD_KEY_OPTIONS: dict = {
    'Right Ctrl':   'right_ctrl',
    'Right Shift':  'right_shift',
    'Right Alt':    'right_alt',
    'Left Ctrl':    'left_ctrl',
    'Left Shift':   'left_shift',
    'Left Alt':     'left_alt',
    **{f'F{n}': f'f{n}' for n in range(1, 25)},
}
_AI_CMD_KEY_TO_LABEL: dict = {v: k for k, v in _AI_CMD_KEY_OPTIONS.items()}

# Canonical alias map: normalise rctrl/right_ctrl etc. before comparing
_KEY_NORMALIZE: dict = {
    'right_ctrl':  'right_ctrl', 'rctrl':  'right_ctrl',
    'left_ctrl':   'left_ctrl',  'lctrl':  'left_ctrl',
    'right_alt':   'right_alt',  'ralt':   'right_alt',
    'left_alt':    'left_alt',   'lalt':   'left_alt',
    'right_shift': 'right_shift', 'rshift': 'right_shift',
    'left_shift':  'left_shift',  'lshift': 'left_shift',
}
# dictation.py on_key_press: command_hotkey fires whenever pressed; only the
# button/session listener (_check_command_mode_key) reads command_mode.enabled.
_ENABLE_VOICE_CONTROL_DESC = (
    "Enables the voice-control button below. The command-only shortcut always "
    "works, even with this off."
)
# dictation.py _install_capslock_hook: keyboard.hook_key('caps lock'), installed
# only while streaming_mode is on; the tray's Streaming Mode toggle and the
# checkbox below share set_streaming_mode(). streaming_hotkey is read by nothing.
_STREAMING_KEY_LABEL = "CapsLock (fixed)"
_STREAMING_KEY_DESC = (
    "The streaming key is fixed in code and only works while Streaming preview is on."
)
_STREAMING_PREVIEW_DESC = (
    "Show live partials in the overlay while you hold CapsLock -- the same switch as "
    "the tray's Streaming Mode."
)
_PASTE_STAGED_DESC = "Toggle hands-free only; same as saying '{commit}'."
_CMD_DEBOUNCE_DESC = "Taps shorter than this are ignored (audio discarded)."
_CMD_TIMEOUT_DESC = "Exit after this much inactivity (max 30 min) (toggle mode only)."
_CMD_MISS_LIMIT_DESC = "Exit after this many unmatched command utterances (toggle mode only)."
_AVA_MODE_ENABLED_DESC = "Hold the key below to talk to Ava (conversation assistant mode)."
_AVA_MODE_KEY_DESC = (
    "A single named key -- the runtime cannot bind a combination "
    "(dictation.py _get_pynput_command_key)."
)
_AVA_KEY_UNSUPPORTED = "(unsupported: {combo}) -- pick a key"
_CMD_TTS_CHAR_LIMIT_DESC = (
    "In a hands-free command session, spoken command acknowledgements longer "
    "than this are skipped so speech doesn't talk over your next command. "
    "Ava's answers, Ava status messages and yes/no confirmations are always "
    "spoken. No limit (0) speaks every acknowledgement in full."
)
_CMD_CANCEL_WINDOW_DESC = (
    "When a command is recognised while you are dictating, the indicator shows "
    "\"running <command>... say no\" and waits this long before running it. "
    "Say \"no\", \"cancel\" or \"stop\" to cancel; start talking and it waits to "
    "hear you; keep dictating and it is cancelled. Reading commands run at once "
    "and destructive ones still ask yes or no. Off (0) runs commands at once."
)
_CMD_CANCEL_WINDOW_ALL_DESC = (
    "Also wait before the everyday words the dictation lane has always "
    "accepted (submit, enter, escape, next field, focus / switch to ...). "
    "Off by default: those are said on purpose, and a wait slows every send."
)
# Queue 62: every command_mode key the app reads now has a control below.
# A key added here gets listed on the tab as config-file only.
_MODES_CONFIG_ONLY_KEYS: tuple = ()
_MODES_CONFIG_ONLY_NOTE = (
    "Config-file only for now (command_mode in config.json): " + ", ".join(_MODES_CONFIG_ONLY_KEYS) + "."
)

# command_mode.command_matching_enabled: samsara/commands.py process_text
# gates on app.command_matching_enabled unless force_commands (hands-free
# session, wake word, Ava, cheat sheet all pass it).
_CMD_MATCHING_DESC = (
    "Recognise spoken commands in ordinary dictation. Off: everything you "
    "dictate is typed as text. Saying 'command mode on' or 'command mode off' "
    "flips this same switch. Hands-free sessions, the wake word and Ava "
    "always recognise commands, whatever this is set to."
)
# command_mode.exit_earcon: dictation.py exit_command_mode plays 'stop' when
# the session ends while not recording (stop_recording plays its own).
_CMD_EXIT_EARCON_DESC = (
    "Play the stop sound when a voice-control session ends. Off: a session "
    "that times out or ends by voice closes with no sound, so you only see "
    "it in the tray."
)
# command_mode.utterance_silence_s / dictate_utterance_silence_s:
# samsara/audio_engine/wake_consumer.py reads both on every audio frame
# (fallbacks 1.0 / 0.65), so a saved change applies to the next pause.
_SILENCE_MIN_S = 0.3
_SILENCE_MAX_S = 3.0
_CMD_SILENCE_DESC = (
    "Hands-free COMMAND lane: how long you must pause before what you said "
    "is treated as one finished command. Shorter runs commands sooner but can "
    "cut a command in two at a natural pause; longer waits after every command. "
    f"Limited to {_SILENCE_MIN_S:g}-{_SILENCE_MAX_S:g} s: below that every "
    "short gap between words would end the command."
)
_DICTATE_SILENCE_DESC = (
    "Hands-free DICTATE lane: how long a pause closes a chunk of dictation. "
    "Nothing is pasted on this pause (you still say 'end'), but a longer gap "
    "makes 'end' slower to paste and a shorter one breaks sentences into more "
    f"pieces. Limited to {_SILENCE_MIN_S:g}-{_SILENCE_MAX_S:g} s."
)
# command_mode.preview_idle_delay_s / preview_idle_opacity (queue 75):
# samsara/streaming.py DictatePreviewSession reads both each time the
# hands-free session enters DICTATE.
_PREVIEW_IDLE_DELAY_DESC = (
    "Hands-free dictation preview: how long after you stop talking (and no new "
    "text arrives) before the preview box fades. While faded, clicks go "
    "straight through it to the window underneath. It comes back at once "
    "when you speak. Applies the next time the session enters dictation."
)
_PREVIEW_IDLE_OPACITY_DESC = (
    "How visible the faded preview stays. The faint box is Samsara's sign that "
    "it is still listening, so it stays on screen by default. 'Fully hidden' "
    "removes it while idle; the listening indicator still shows the session."
)
_PREVIEW_HIDDEN_WARNING = (
    "Fully hidden: while you are not talking there is no preview on screen. "
    "Watch the listening indicator to see that the session is still running."
)


# command_mode.abort_phrases: dictation.py passes it to SessionModeManager as
# extra_sleep_phrases, which is built once per app run.
# command_mode.stop_phrases: dictation.py passes it to SessionModeManager as
# stop_phrases, which REPLACES the built-in list and is read once per app run.
_STOP_PHRASES_DESC = (
    "Words, one per line, that stop what Samsara is doing when said on their "
    "own: the answer being spoken, a question waiting for you, anything "
    "queued. Your draft, your lane and the microphone are untouched -- it is "
    "not an exit. Replaces the built-in '{stop}'; leave it empty to keep "
    "those. Changes apply the next time Samsara starts."
)

_ABORT_PHRASES_DESC = (
    "Extra phrases, one per line, that end the hands-free session when said "
    "on their own, like the built-in '{sleep}'. Your staged draft is kept. "
    "Empty means only the built-in phrases work; they cannot be removed. "
    "Changes apply the next time Samsara starts."
)


def _silence_warning(value: float, lane: str, stored=None) -> str:
    """Inline warning for a per-utterance silence gap ('' when none).

    stored is the raw config value, reported when the spin box had to clamp it.
    """
    parts = []
    if stored is not None:
        try:
            raw = float(stored)
        except (TypeError, ValueError):
            raw = None
        if raw is None or not (_SILENCE_MIN_S <= raw <= _SILENCE_MAX_S):
            parts.append(f"config.json has {stored!r}, outside {_SILENCE_MIN_S:g}-{_SILENCE_MAX_S:g} s; "
                         f"saving stores {value:g} s.")
    if lane == 'command':
        if value < 0.5:
            parts.append("Very short: a normal pause mid-command can split it into two pieces that both miss.")
        elif value > 2.0:
            parts.append("Long: every command waits this long after you stop talking before it runs.")
    else:
        if value < 0.4:
            parts.append("Very short: dictation is cut into many small pieces, which gives recognition less context.")
        elif value > 2.0:
            parts.append("Long: saying 'end' waits this long before it pastes.")
    return " ".join(parts)


def _abort_phrase_lines(text: str) -> list:
    """Editor text -> saved list: stripped, non-empty, first occurrence kept."""
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def _abort_phrase_warnings(phrases) -> list:
    """Why each phrase would misbehave, checked in SessionModeManager.dispatch
    order: stop is matched first, then sleep (these phrases), then lane
    switches, commit and scratch-that, which a clashing phrase would swallow."""
    from samsara import session_modes as sm  # noqa: PLC0415
    commit = {sm.normalize_utterance(p) for p in sm._DICTATE_COMMIT_HOMOPHONES}
    switches = {sm.normalize_utterance(p) for p in sm._WHOLE_UTTERANCE_SWITCHES}
    stops = {sm.normalize_utterance(p) for p in sm.SESSION_STOP_PHRASES}
    sleeps = {sm.normalize_utterance(p) for p in sm.SESSION_SLEEP_PHRASES}
    scratch = sm.normalize_utterance(sm.SCRATCH_THAT_PHRASE)
    out = []
    for phrase in phrases:
        norm = sm.normalize_utterance(phrase)
        if not norm:
            out.append(f"'{phrase}' is ignored: nothing is left after dropping punctuation and filler words.")
        elif norm in stops:
            out.append(f"'{phrase}' is already the stop phrase and is checked first, so it never ends the session.")
        elif norm in commit:
            out.append(f"'{phrase}' is the paste word: it would end the session instead of pasting your draft.")
        elif norm in switches:
            out.append(f"'{phrase}' switches lanes: it would end the session instead.")
        elif norm == scratch:
            out.append(f"'{phrase}' would end the session instead of scratching your last dictation.")
        elif norm in sleeps:
            out.append(f"'{phrase}' is already built in.")
    return out
def _stop_phrase_warnings(phrases) -> list:
    """Queue 116. The stop is checked FIRST in dispatch order, so anything
    listed here wins over every other whole-utterance phrase -- which is the
    point for a panic control and a trap for everything else."""
    from samsara import session_modes as sm  # noqa: PLC0415
    if not [p for p in phrases if sm.normalize_utterance(p)]:
        built_in = " / ".join(f'"{p}"' for p in sm.SESSION_STOP_PHRASES)
        return [f"Empty: the built-in {built_in} stays in use. The emergency stop "
                f"cannot be switched off from here."]
    commit = {sm.normalize_utterance(p) for p in sm._DICTATE_COMMIT_HOMOPHONES}
    switches = {sm.normalize_utterance(p) for p in sm._WHOLE_UTTERANCE_SWITCHES}
    sleeps = {sm.normalize_utterance(p) for p in sm.SESSION_SLEEP_PHRASES}
    scratch = sm.normalize_utterance(sm.SCRATCH_THAT_PHRASE)
    out = []
    for phrase in phrases:
        norm = sm.normalize_utterance(phrase)
        if not norm:
            out.append(f"'{phrase}' is ignored: nothing is left after dropping punctuation and filler words.")
        elif len(norm.split()) == 1 and len(norm) <= 2:
            out.append(f"'{phrase}' is very short: a mishearing would halt what is running.")
        elif norm in commit:
            out.append(f"'{phrase}' is the paste word: it would halt instead of pasting your draft.")
        elif norm in switches:
            out.append(f"'{phrase}' switches lanes: it would halt instead.")
        elif norm == scratch:
            out.append(f"'{phrase}' would halt instead of scratching your last dictation.")
        elif norm in sleeps:
            out.append(f"'{phrase}' ends the session: as a stop phrase it would halt without sleeping.")
    return out


# Keys that share a default on purpose: each is live in a different mode
# (hands-free toggle vs continuous mode), so the same combo is not a collision.
_MODES_COLLISION_EXEMPT_PAIRS = frozenset({
    frozenset({'dictate_commit_hotkey', 'continuous_commit_hotkey'}),
})


def _button_behavior_note() -> str:
    """What the button actually does, read from samsara.session_modes."""
    from samsara import session_modes  # noqa: PLC0415
    switches = session_modes._WHOLE_UTTERANCE_SWITCHES
    by_mode = {}
    for phrase, mode in switches.items():
        by_mode.setdefault(mode.value, []).append(phrase)
    lanes = "; ".join(
        f"{mode.upper()}: " + " / ".join(f'"{p}"' for p in sorted(by_mode[mode]))
        for mode in ("command", "dictate", "ava") if mode in by_mode
    )
    stop = " / ".join(f'"{p}"' for p in session_modes.SESSION_STOP_PHRASES)
    sleep = " / ".join(f'"{p}"' for p in session_modes.SESSION_SLEEP_PHRASES)
    return (
        f"Toggle opens in the {session_modes.SessionMode.DICTATE.value.upper()} lane. "
        f"Switch lanes with {lanes}. "
        f'Dictation is buffered until you say "{session_modes.DICTATE_COMMIT_PHRASE}" '
        f"or press the Paste staged thought key. {stop} cancels what is running "
        f"(your draft is kept); {sleep} exits and keeps the draft."
    )


class ModesPage:
    """Methods of the Modes settings page (moved from _SettingsWindow)."""

    def _reset_preview_position(self):
        from samsara.streaming import reset_preview_placement  # noqa: PLC0415
        return reset_preview_placement(self.app)

    def _reset_indicator_position(self):
        from samsara.ui.listening_indicator import reset_indicator_placement  # noqa: PLC0415
        return reset_indicator_placement(self.app)

    def _reset_floating_window_positions(self):
        self._reset_preview_position()
        self._reset_indicator_position()

    def _mouse_hotkey_control(self, btn):
        """The primary-key button, plus an inline notice when the configured
        mouse hotkey is not actually active (35). The saved choice is shown
        and kept as it is; the notice retries the hook when clicked."""
        status_fn = getattr(self.app, 'mouse_hotkey_status', None)
        try:
            status = status_fn() if callable(status_fn) else {'state': 'n/a'}
        except Exception:
            status = {'state': 'n/a'}
        self._mouse_hotkey_status_btn = None
        if status.get('state') != 'disabled':
            return btn
        wrap = QWidget()
        column = QVBoxLayout(wrap)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(btn)
        notice = QPushButton("not active - hook disabled, click to retry")
        notice.setObjectName("mouseHotkeyStatus")
        notice.setFlat(True)
        notice.setCursor(Qt.CursorShape.PointingHandCursor)
        notice.setToolTip(f"{status.get('reason', '')}. "
                          f"{status.get('fallback') or 'The keyboard hotkey'} records meanwhile.")
        notice.setStyleSheet(f"color: {theme.WARNING}; text-align: left; border: none; padding: 0;")

        def _retry():
            reenable = getattr(self.app, 'reenable_mouse_hotkey', None)
            state = reenable() if callable(reenable) else 'disabled'
            if state == 'active':
                notice.setText("active")
                notice.setEnabled(False)
            else:
                notice.setText("still not active - click to retry")

        notice.clicked.connect(_retry)
        column.addWidget(notice)
        self._mouse_hotkey_status_btn = notice
        return wrap

    def _build_modes_tab(self):
        from samsara.ava_command_session import _DEFAULTS as _AIMD  # noqa: PLC0415

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        cfg = self.app.config
        ww_cfg = cfg.get('wake_word_config', {}) or {}
        ww_audio = ww_cfg.get('audio', {}) or {}
        cmd_cfg = cfg.get('command_mode', {}) or {}
        ai_cfg = cfg.get('ava_command_session', {}) or {}

        def _add_row(card_layout, label, description, widget, width=280):
            card_layout.addLayout(
                self._setting_row(
                    label,
                    description,
                    widget,
                    control_width=width,
                )
            )

        def _disclosure_button(label: str) -> QPushButton:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("class", "secondary")
            button.setFixedWidth(230)
            button.setStyleSheet(
                "QPushButton {"
                " background-color: transparent;"
                f" color: {theme.TEXT_SECONDARY};"
                f" border: 1px solid {theme.wash(0.16)};"
                " border-radius: 6px;"
                " padding: 8px 12px;"
                " font-size: 13px;"
                "}"
                "QPushButton:hover {"
                f" color: {theme.TEXT_PRIMARY};"
                f" border-color: {theme.wash(0.28)};"
                "}"
            )
            return button

        # One tab-wide collision banner, shown above every section.
        collision_warn = QLabel("")
        collision_warn.setWordWrap(True)
        collision_warn.setStyleSheet(self._collision_warn_style())
        collision_warn.setVisible(False)
        self._widgets['modes_collision_warn'] = collision_warn
        layout.addWidget(collision_warn)
        layout.addSpacing(8)

        modes_intro = QLabel(
            "Choose how voice control starts. Hold for a temporary command-only "
            "session, or toggle it for persistent Hands-Free commands and dictation."
        )
        modes_intro.setWordWrap(True)
        modes_intro.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 13px;")
        layout.addWidget(modes_intro)

        # ---- Card 1: Hands-Free / Voice Control -------------------------
        hands_free_card, hands_free_layout = self._section_card(
            "Hands-Free / Voice Control",
        )
        layout.addWidget(hands_free_card)

        cmd_enabled_cb = QCheckBox()
        cmd_enabled_cb.setChecked(bool(cmd_cfg.get('enabled', False)))
        self._widgets['cmd_tab_enabled'] = cmd_enabled_cb
        _add_row(
            hands_free_layout,
            "Enable voice control",
            _ENABLE_VOICE_CONTROL_DESC,
            cmd_enabled_cb,
            width=220,
        )

        current_btn_key = cmd_cfg.get('button', 'rctrl')
        current_btn_label = _CMD_BUTTON_KEY_TO_LABEL.get(current_btn_key, 'Right Ctrl (default)')
        cmd_button_combo = QComboBox()
        cmd_button_combo.addItems(list(_CMD_BUTTON_OPTIONS.keys()))
        cmd_button_combo.setCurrentText(current_btn_label)
        self._widgets['cmd_tab_button'] = cmd_button_combo
        _add_row(
            hands_free_layout,
            "Voice control button",
            "Use one button for temporary commands or persistent Hands-Free.",
            cmd_button_combo,
            width=260,
        )
        cmd_button_combo.currentIndexChanged.connect(lambda _idx: self._check_modes_collisions())

        cmd_mode_combo = QComboBox()
        cmd_mode_combo.addItems(['hold', 'toggle'])
        cmd_mode_combo.setCurrentText(cmd_cfg.get('mode', 'hold'))
        self._widgets['cmd_mode'] = cmd_mode_combo
        _add_row(
            hands_free_layout,
            "Button behavior",
            "Hold: commands only while held. Toggle: persistent Hands-Free commands and dictation.",
            cmd_mode_combo,
            width=220,
        )
        behavior_note = QLabel(_button_behavior_note())
        behavior_note.setWordWrap(True)
        behavior_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        self._widgets['button_behavior_note'] = behavior_note
        hands_free_layout.addWidget(behavior_note)

        hold_heading = QLabel("Command-only activation")
        hold_heading.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; margin-top: 4px;"
        )
        hands_free_layout.addWidget(hold_heading)

        command_hotkey_btn = _HotkeyButton(
            cfg.get('command_hotkey', 'ctrl+alt+c'), on_change=self._check_modes_collisions
        )
        self._widgets['command_hotkey'] = command_hotkey_btn
        _add_row(
            hands_free_layout,
            "Command-only key",
            "Optional shortcut for one command capture without dictation output.",
            command_hotkey_btn,
            width=260,
        )

        suppress_cb = QCheckBox("Suppress browser-back when using Mouse 4/5")
        suppress_cb.setChecked(bool(cmd_cfg.get('suppress_button', True)))
        self._widgets['cmd_tab_suppress'] = suppress_cb
        _add_row(
            hands_free_layout,
            "Mouse button suppression",
            "",
            suppress_cb,
            width=260,
        )

        wake_heading = QLabel("Wake activation")
        wake_heading.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; margin-top: 4px;"
        )
        hands_free_layout.addWidget(wake_heading)

        wake_enabled = QCheckBox()
        wake_enabled.setChecked(bool(cfg.get('wake_word_enabled', False)))
        self._widgets['wake_word_enabled'] = wake_enabled
        _add_row(
            hands_free_layout,
            "Enable wake activation",
            "Listen for the configured wake phrase.",
            wake_enabled,
            width=220,
        )

        wake_word_btn = _HotkeyButton(
            cfg.get('wake_word_hotkey', 'ctrl+alt+w'), on_change=self._check_modes_collisions
        )
        self._widgets['wake_word_hotkey'] = wake_word_btn
        _add_row(
            hands_free_layout,
            "Wake activation key",
            "Tap to start or stop wake-phrase listening.",
            wake_word_btn,
            width=240,
        )

        phrases = ww_cfg.get('phrase_options', DEFAULT_WAKE_PHRASE_OPTIONS)
        primary_phrase = phrases[0] if phrases else DEFAULT_WAKE_PHRASE
        wake_phrase_display = QLineEdit(primary_phrase)
        wake_phrase_display.setReadOnly(True)
        wake_phrase_display.setObjectName("wakePhraseDisplay")
        wake_phrase_display.setPlaceholderText("wake phrase")
        self._widgets['wake_word_phrase_display'] = wake_phrase_display
        _add_row(
            hands_free_layout,
            "Wake phrase",
            "The word or phrase that activates voice control.",
            wake_phrase_display,
            width=340,
        )

        wake_timeout_spin = QDoubleSpinBox()
        wake_timeout_spin.setRange(1.0, 30.0)
        wake_timeout_spin.setSingleStep(0.5)
        wake_timeout_spin.setDecimals(1)
        wake_timeout_spin.setSuffix(" s")
        wake_timeout_spin.setValue(float(ww_audio.get('wake_command_timeout', 5.0)))
        self._widgets['wake_cmd_timeout'] = wake_timeout_spin
        _add_row(
            hands_free_layout,
            "Timeout after wake",
            "Seconds to wait for speech after wake activation.",
            wake_timeout_spin,
            width=170,
        )

        wake_note = QLabel("More wake phrases can be added in the config file.")
        wake_note.setWordWrap(True)
        wake_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        hands_free_layout.addWidget(wake_note)

        # ---- Hands-free session tuning (queue 62) ---------------------
        session_heading = QLabel("Hands-free session")
        session_heading.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; margin-top: 4px;"
        )
        hands_free_layout.addWidget(session_heading)

        def _warning_label(name: str) -> QLabel:
            label = QLabel("")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {theme.WARNING}; font-size: 12px;")
            label.setVisible(False)
            self._widgets[name] = label
            return label

        def _show_warning(label: QLabel, text: str) -> None:
            label.setText(text)
            label.setVisible(bool(text))

        silence_rows = (
            ('cmd_utterance_silence', 'utterance_silence_s', 1.0, 'command',
             "Command pause length", _CMD_SILENCE_DESC),
            ('cmd_dictate_utterance_silence', 'dictate_utterance_silence_s',
             config_defaults.DEFAULTS['command_mode.dictate_utterance_silence_s'], 'dictate',
             "Dictation pause length", _DICTATE_SILENCE_DESC),
        )
        for widget_key, cfg_key, fallback, lane, label, desc in silence_rows:
            stored = cmd_cfg.get(cfg_key, fallback)
            spin = QDoubleSpinBox()
            spin.setRange(_SILENCE_MIN_S, _SILENCE_MAX_S)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setSuffix(" s")
            try:
                spin.setValue(float(stored))      # Qt clamps out-of-range values
                clamped_from = None if _SILENCE_MIN_S <= float(stored) <= _SILENCE_MAX_S else stored
            except (TypeError, ValueError):
                spin.setValue(float(fallback))
                clamped_from = stored
            self._widgets[widget_key] = spin
            _add_row(hands_free_layout, label, desc, spin, width=170)
            warn_label = _warning_label(widget_key + '_warn')
            hands_free_layout.addWidget(warn_label)
            _show_warning(warn_label, _silence_warning(spin.value(), lane, clamped_from))
            spin.valueChanged.connect(
                lambda v, w=warn_label, ln=lane, c=clamped_from: _show_warning(w, _silence_warning(v, ln, c)))

        exit_earcon_cb = QCheckBox()
        exit_earcon_cb.setChecked(bool(cmd_cfg.get('exit_earcon', True)))
        self._widgets['cmd_exit_earcon'] = exit_earcon_cb
        _add_row(hands_free_layout, "Sound when a session ends", _CMD_EXIT_EARCON_DESC,
                 exit_earcon_cb, width=220)

        # Queue 75: dictation preview idle fade.
        from samsara.config_schema import SETTINGS_SCHEMA
        delay_schema = SETTINGS_SCHEMA['command_mode.preview_idle_delay_s']
        opacity_schema = SETTINGS_SCHEMA['command_mode.preview_idle_opacity']
        idle_delay_spin = QDoubleSpinBox()
        idle_delay_spin.setRange(delay_schema['min'], delay_schema['max'])
        idle_delay_spin.setSingleStep(delay_schema['step'])
        idle_delay_spin.setDecimals(1)
        idle_delay_spin.setSuffix(" s")
        try:
            idle_delay_spin.setValue(float(cmd_cfg.get('preview_idle_delay_s', delay_schema['default'])))
        except (TypeError, ValueError):
            idle_delay_spin.setValue(delay_schema['default'])
        self._widgets['cmd_preview_idle_delay'] = idle_delay_spin
        _add_row(hands_free_layout, "Preview fades after", _PREVIEW_IDLE_DELAY_DESC,
                 idle_delay_spin, width=170)

        idle_opacity_spin = QSpinBox()
        idle_opacity_spin.setRange(0, round(opacity_schema['max'] * 100))
        idle_opacity_spin.setSingleStep(round(opacity_schema['step'] * 100))
        idle_opacity_spin.setSuffix(" %")
        idle_opacity_spin.setSpecialValueText("Fully hidden")
        try:
            stored_opacity = float(cmd_cfg.get('preview_idle_opacity', opacity_schema['default']))
        except (TypeError, ValueError):
            stored_opacity = opacity_schema['default']
        idle_opacity_spin.setValue(round(stored_opacity * 100))
        self._widgets['cmd_preview_idle_opacity'] = idle_opacity_spin
        _add_row(hands_free_layout, "Faded preview visibility", _PREVIEW_IDLE_OPACITY_DESC,
                 idle_opacity_spin, width=170)
        idle_hidden_warn = _warning_label('cmd_preview_idle_opacity_warn')
        hands_free_layout.addWidget(idle_hidden_warn)
        _show_warning(idle_hidden_warn, _PREVIEW_HIDDEN_WARNING if idle_opacity_spin.value() == 0 else "")
        idle_opacity_spin.valueChanged.connect(
            lambda v, w=idle_hidden_warn: _show_warning(w, _PREVIEW_HIDDEN_WARNING if v == 0 else ""))

        recovery_heading = QLabel("Floating window recovery")
        recovery_heading.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; margin-top: 4px;"
        )
        hands_free_layout.addWidget(recovery_heading)
        preview_reset = QPushButton("Reset preview position")
        preview_reset.clicked.connect(self._reset_preview_position)
        self._widgets['reset_preview_position'] = preview_reset
        _add_row(hands_free_layout, "Dictation preview", "Move the preview back to its default position.",
                 preview_reset, width=220)
        indicator_reset = QPushButton("Reset indicator position")
        indicator_reset.clicked.connect(self._reset_indicator_position)
        self._widgets['reset_indicator_position'] = indicator_reset
        _add_row(hands_free_layout, "Listening indicator", "Move the listening indicator back to its default position.",
                 indicator_reset, width=220)
        both_reset = QPushButton("Reset both floating windows")
        both_reset.clicked.connect(self._reset_floating_window_positions)
        self._widgets['reset_floating_window_positions'] = both_reset
        _add_row(hands_free_layout, "Reset both", "Restore both floating windows in one action.",
                 both_reset, width=250)

        stored_stop = cmd_cfg.get('stop_phrases', [])
        if isinstance(stored_stop, str):
            stored_stop = [stored_stop]
        stop_edit = QPlainTextEdit()
        stop_edit.setPlainText("\n".join(str(p) for p in (stored_stop or []) if isinstance(p, str)))
        stop_edit.setPlaceholderText("\n".join(session_modes.SESSION_STOP_PHRASES))
        stop_edit.setFixedHeight(84)
        self._widgets['cmd_stop_phrases'] = stop_edit
        _add_row(hands_free_layout, "Stop words",
                 _STOP_PHRASES_DESC.format(
                     stop="' / '".join(session_modes.SESSION_STOP_PHRASES)),
                 stop_edit, width=280)
        stop_warn = _warning_label('cmd_stop_phrases_warn')
        hands_free_layout.addWidget(stop_warn)

        def _refresh_stop_warn():
            _show_warning(stop_warn, " ".join(
                _stop_phrase_warnings(_abort_phrase_lines(stop_edit.toPlainText()))))
        stop_edit.textChanged.connect(_refresh_stop_warn)
        _refresh_stop_warn()

        stored_abort = cmd_cfg.get('abort_phrases', [])
        if isinstance(stored_abort, str):
            stored_abort = [stored_abort]
        abort_edit = QPlainTextEdit()
        abort_edit.setPlainText("\n".join(str(p) for p in (stored_abort or []) if isinstance(p, str)))
        abort_edit.setPlaceholderText("No extra phrases (one per line)")
        abort_edit.setFixedHeight(84)
        self._widgets['cmd_abort_phrases'] = abort_edit
        _add_row(hands_free_layout, "Extra exit phrases",
                 _ABORT_PHRASES_DESC.format(sleep=session_modes.SESSION_SLEEP_PHRASES[0]),
                 abort_edit, width=280)
        abort_warn = _warning_label('cmd_abort_phrases_warn')
        hands_free_layout.addWidget(abort_warn)

        def _refresh_abort_warn():
            _show_warning(abort_warn, " ".join(
                _abort_phrase_warnings(_abort_phrase_lines(abort_edit.toPlainText()))))
        abort_edit.textChanged.connect(_refresh_abort_warn)
        _refresh_abort_warn()

        if _MODES_CONFIG_ONLY_KEYS:
            config_only_note = QLabel(_MODES_CONFIG_ONLY_NOTE)
            config_only_note.setWordWrap(True)
            config_only_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
            self._widgets['modes_config_only_note'] = config_only_note
            hands_free_layout.addWidget(config_only_note)

        # ---- Card 2: Dictation bindings ---------------------------------
        dictation_card, dictation_layout = self._section_card(
            "Dictation bindings",
            "Recording behavior and keys for normal text dictation.",
        )
        layout.addWidget(dictation_card)

        matching_cb = QCheckBox()
        matching_cb.setChecked(bool(cmd_cfg.get(
            'command_matching_enabled', getattr(self.app, 'command_matching_enabled', False))))
        self._widgets['cmd_matching_enabled'] = matching_cb
        _add_row(dictation_layout, "Commands while dictating", _CMD_MATCHING_DESC,
                 matching_cb, width=220)

        mode_combo = QComboBox()
        mode_combo.addItems(['hold', 'toggle', 'continuous'])
        mode_combo.setCurrentText(cfg.get('mode', 'hold'))
        self._widgets['mode'] = mode_combo
        _add_row(
            dictation_layout,
            "Primary dictation key behavior",
            "Hold: record while held. Toggle: press to start/stop. Continuous: finish on silence.",
            mode_combo,
            width=240,
        )

        _dictation_hotkeys = [
            ('hotkey', cfg.get('hotkey', 'ctrl+shift'),
             "Primary dictation key", "Hold or toggle recording for text output."),
            ('continuous_hotkey', cfg.get('continuous_hotkey', 'ctrl+alt+d'),
             "Continuous mode key", "Enter/exit continuous dictation mode."),
            ('cancel_hotkey', cfg.get('cancel_hotkey', 'escape'),
             "Cancel", "Abort the current recording without transcribing."),
            ('undo_hotkey', cfg.get('undo_hotkey', 'ctrl+alt+z'),
             "Undo", "Undo the last transcription in the focused application."),
            ('dictate_commit_hotkey',
             cfg.get('dictate_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY),
             "Paste staged thought",
             _PASTE_STAGED_DESC.format(commit=session_modes.DICTATE_COMMIT_PHRASE)),
            # 08d: keys the app has always read but never exposed.
            ('memo_hotkey', cfg.get('memo_hotkey', 'ctrl+alt+m'),
             "Voice memo", "Divert the next recording to your memo note instead of typing it."),
            ('correction_hotkey', cfg.get('correction_hotkey', 'ctrl+alt+r'),
             "Correction report", "Report the last transcription as wrong (voice training)."),
            ('capture_correction_hotkey',
             (cfg.get('hotkeys', {}) or {}).get('capture_correction', 'ctrl+alt+x'),
             "Correction capture", "Open the fix-my-last-dictation window pre-filled with the last text."),
            ('continuous_commit_hotkey',
             cfg.get('continuous_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY),
             "Continuous commit", "Commits accumulated speech in continuous mode -- only when the trigger below is 'key'."),
        ]
        for config_key, default, label, desc in _dictation_hotkeys:
            btn = _HotkeyButton(
                default, on_change=self._check_modes_collisions,
                # Only the primary dictation key may be a side mouse button.
                allow_mouse=(config_key == 'hotkey'),
            )
            self._widgets[config_key] = btn
            dictation_layout.addLayout(
                self._setting_row(
                    label,
                    desc,
                    self._mouse_hotkey_control(btn) if config_key == 'hotkey' else btn,
                    control_width=260,
                )
            )

        commit_trigger_combo = QComboBox()
        commit_trigger_combo.addItems(['silence', 'key'])
        commit_trigger_combo.setCurrentText(
            str(cfg.get('continuous_commit_trigger', DEFAULT_CONTINUOUS_COMMIT_TRIGGER)))
        self._widgets['continuous_commit_trigger'] = commit_trigger_combo
        dictation_layout.addLayout(
            self._setting_row(
                "Continuous commit trigger",
                "silence: commit automatically after a pause. key: keep talking and tap the Continuous commit key.",
                commit_trigger_combo,
                control_width=200,
            )
        )

        # Streaming (08d): the key is not a setting -- the hook hardcodes CapsLock.
        streaming_key_display = QLineEdit(_STREAMING_KEY_LABEL)
        streaming_key_display.setReadOnly(True)
        streaming_key_display.setObjectName("streamingKeyDisplay")
        self._widgets['streaming_key_display'] = streaming_key_display
        dictation_layout.addLayout(
            self._setting_row("Streaming key", _STREAMING_KEY_DESC, streaming_key_display, control_width=260)
        )
        streaming_cb = QCheckBox()
        streaming_cb.setChecked(bool(cfg.get('streaming_mode', False)))
        self._widgets['streaming_mode'] = streaming_cb
        dictation_layout.addLayout(
            self._setting_row("Streaming preview", _STREAMING_PREVIEW_DESC, streaming_cb, control_width=220)
        )

        # ---- Card 4: Ava Command Session ---------------------------------
        ai_card, ai_layout = self._section_card(
            "Ava Command Session",
            "Command-first latched voice session: speak short commands, Ava resolves and confirms.",
        )
        layout.addWidget(ai_card)

        ai_intro = QLabel(
            "Latch into a hands-free session and speak commands directly. Falls back to "
            "AI resolution (local or cloud) only when no exact command matches."
        )
        ai_intro.setWordWrap(True)
        ai_intro.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        ai_layout.addWidget(ai_intro)

        ai_enabled = QCheckBox()
        ai_enabled.setChecked(bool(ai_cfg.get('enabled', _AIMD['enabled'])))
        self._widgets['ava_cmd_enabled'] = ai_enabled
        _add_row(
            ai_layout,
            "Enable Ava command session",
            "Master switch for the command-first latched session.",
            ai_enabled,
            width=220,
        )

        key_val = ai_cfg.get('key', _AIMD['key'])
        key_label = _AI_CMD_KEY_TO_LABEL.get(key_val, list(_AI_CMD_KEY_OPTIONS.keys())[0])
        ai_key_combo = QComboBox()
        ai_key_combo.addItems(list(_AI_CMD_KEY_OPTIONS.keys()))
        if key_label in _AI_CMD_KEY_OPTIONS:
            ai_key_combo.setCurrentText(key_label)
        self._widgets['ava_cmd_key'] = ai_key_combo
        _add_row(
            ai_layout,
            "Activation key",
            "Key that latches the Ava command session (default Left Alt).",
            ai_key_combo,
            width=260,
        )
        ai_key_combo.currentIndexChanged.connect(lambda _idx: self._check_modes_collisions())

        ai_adv_button = _disclosure_button("Show AI backend options")
        ai_adv = QWidget()
        # Same cascade cause as the QCheckBox/QLabel fixes: a bare QWidget
        # used purely as a disclosure-panel container has no background-
        # color of its own, so it otherwise paints the QMainWindow, QWidget
        # rule's BG0 across every row inside it when expanded, on top
        # of this card. Scoped by objectName rather than a bare/unqualified
        # "background: transparent;" -- confirmed (see mic_row_widget above)
        # that the unqualified form leaks into the ancestor stylesheet
        # cascade and strips a child QPushButton's app-level background-
        # color, leaving it rendered unstyled/grey; scoping by objectName
        # avoids that regardless of which control types end up in here.
        ai_adv.setObjectName("aiAdvancedPanel")
        ai_adv.setStyleSheet("QWidget#aiAdvancedPanel { background-color: transparent; }")
        ai_adv_layout = QVBoxLayout(ai_adv)
        ai_adv_layout.setContentsMargins(0, 0, 0, 0)
        ai_adv_layout.setSpacing(8)
        ai_adv.setVisible(False)

        def _toggle_ai_adv(checked: bool) -> None:
            ai_adv.setVisible(checked)
            ai_adv_button.setText(
                "Hide AI backend options" if checked else "Show AI backend options"
            )
        ai_adv_button.toggled.connect(_toggle_ai_adv)

        backend_val = ai_cfg.get('backend', _AIMD['backend'])
        backend_combo = QComboBox()
        backend_combo.addItems(['Local (Ollama)', 'Cloud'])
        backend_combo.setCurrentText('Cloud' if backend_val == 'cloud' else 'Local (Ollama)')
        self._widgets['ava_cmd_backend'] = backend_combo
        ai_adv_layout.addLayout(
            self._setting_row(
            "Backend",
            "Local uses Ollama; Cloud uses your configured Ava / Cloud provider.",
            backend_combo,
            control_width=260,
            )
        )

        model_edit = QLineEdit()
        model_edit.setText(ai_cfg.get('model', _AIMD['model']))
        model_edit.setPlaceholderText("e.g. llama3.2:3b")
        model_edit.setEnabled(backend_val != 'cloud')
        self._widgets['ava_cmd_model'] = model_edit
        ai_adv_layout.addLayout(
            self._setting_row(
            "Model",
            "Only used for Local backend.",
            model_edit,
            control_width=340,
            )
        )

        def _update_model_enabled():
            model_edit.setEnabled(backend_combo.currentText() == 'Local (Ollama)')
        backend_combo.currentIndexChanged.connect(lambda _: _update_model_enabled())

        keep_warm = QCheckBox()
        keep_warm.setChecked(bool(ai_cfg.get('keep_warm', _AIMD['keep_warm'])))
        self._widgets['ava_cmd_keep_warm'] = keep_warm
        ai_adv_layout.addLayout(
            self._setting_row(
            "Keep model warm",
            "Pre-load local model when the session latches to reduce first-utterance latency.",
            keep_warm,
            control_width=220,
            )
        )

        queue_spin = QSpinBox()
        queue_spin.setRange(1, 10)
        queue_spin.setValue(int(ai_cfg.get('queue_depth_cap', _AIMD['queue_depth_cap'])))
        self._widgets['ava_cmd_queue_depth'] = queue_spin
        ai_adv_layout.addLayout(
            self._setting_row(
            "Queue depth",
            "Max queued utterances before dropping oldest entries.",
            queue_spin,
            control_width=180,
            )
        )

        miss_limit_spin = QSpinBox()
        miss_limit_spin.setRange(1, 20)
        miss_limit_spin.setValue(int(ai_cfg.get('miss_limit', _AIMD['miss_limit'])))
        self._widgets['ava_cmd_miss_limit'] = miss_limit_spin
        ai_adv_layout.addLayout(
            self._setting_row(
            "Miss limit",
            "Consecutive unresolved utterances before the session exits with feedback.",
            miss_limit_spin,
            control_width=180,
            )
        )

        ai_layout.addWidget(ai_adv_button)
        ai_layout.addWidget(ai_adv)

        # ---- Card 5: Ava shortcut ---------------------------------------
        ava_card, ava_layout = self._section_card(
            "Ava Assistant",
            "Direct access to the conversational assistant path."
        )
        layout.addWidget(ava_card)

        ava_enabled_cb = QCheckBox()
        ava_enabled_cb.setChecked(bool(cfg.get('ava_mode_enabled', True)))
        self._widgets['ava_mode_enabled'] = ava_enabled_cb
        ava_layout.addLayout(
            self._setting_row("Enable Ava mode", _AVA_MODE_ENABLED_DESC, ava_enabled_cb, control_width=220)
        )

        def _confirm_ava_enable(checked, checkbox):
            if not checked:
                return
            effective = dict(self.app.config)
            ava_cfg = dict(effective.get("ava", {}) or {})
            staged = getattr(self, "_ava_consent_staged", None)
            if staged is not None:
                ava_cfg["consent"] = staged
            effective["ava"] = ava_cfg
            cloud_checkbox = self._widgets.get("cloud_enabled")
            cloud_enabled = bool(cloud_checkbox and cloud_checkbox.isChecked())
            cloud_enabled = cloud_enabled or bool(
                (self.app.config.get("cloud_llm", {}) or {}).get("enabled", False))
            if not ava_consent_qt.consent_required(effective, cloud_enabled=cloud_enabled):
                return
            was_blocked = checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(was_blocked)
            consent = ava_consent_qt.request_consent(
                self, effective, cloud_enabled=cloud_enabled)
            if consent is not None:
                self._ava_consent_staged = consent
                checkbox.setChecked(True)

        ava_enabled_cb.clicked.connect(
            lambda checked: _confirm_ava_enable(checked, ava_enabled_cb))
        ai_enabled.clicked.connect(
            lambda checked: _confirm_ava_enable(checked, ai_enabled))

        # 08d: the runtime resolves ava_mode_key with _get_pynput_command_key
        # (single named keys only) -- a captured combo used to disable Ava
        # silently. Same option list as the Ava Command Session key.
        ava_key_combo = QComboBox()
        ava_key_combo.addItems(list(_AI_CMD_KEY_OPTIONS.keys()))
        stored_ava_key = str(cfg.get('ava_mode_key', 'right_alt'))
        if stored_ava_key in _AI_CMD_KEY_TO_LABEL:
            ava_key_combo.setCurrentText(_AI_CMD_KEY_TO_LABEL[stored_ava_key])
        else:
            # Unsupported (a combo, or an unknown name): show it, keep it until a real key is picked.
            ava_key_combo.insertItem(0, _AVA_KEY_UNSUPPORTED.format(combo=stored_ava_key), userData=stored_ava_key)
            ava_key_combo.setCurrentIndex(0)
        self._widgets['ava_mode_key'] = ava_key_combo
        ava_layout.addLayout(
            self._setting_row("Ava mode key", _AVA_MODE_KEY_DESC, ava_key_combo, control_width=280)
        )
        ava_key_combo.currentIndexChanged.connect(lambda _idx: self._check_modes_collisions())

        # ---- Card 6: Advanced tuning ------------------------------------
        advanced_card, advanced_layout = self._section_card(
            "Advanced tuning",
            "Fine-grained timing and threshold controls for users who need behavior tuning."
        )
        layout.addWidget(advanced_card)

        adv_button = _disclosure_button("Show advanced tuning")
        adv_area = QWidget()
        # Same cascade cause as ai_adv above, same objectName-scoped fix.
        adv_area.setObjectName("advancedTuningPanel")
        adv_area.setStyleSheet("QWidget#advancedTuningPanel { background-color: transparent; }")
        adv_area_layout = QVBoxLayout(adv_area)
        adv_area_layout.setContentsMargins(0, 0, 0, 0)
        adv_area_layout.setSpacing(8)
        adv_area.setVisible(False)

        def _toggle_adv(checked: bool) -> None:
            adv_area.setVisible(checked)
            adv_button.setText(
                "Hide advanced tuning" if checked else "Show advanced tuning"
            )
        adv_button.toggled.connect(_toggle_adv)

        cmd_debounce_spin = QSpinBox()
        cmd_debounce_spin.setRange(0, 2000)
        cmd_debounce_spin.setSingleStep(50)
        cmd_debounce_spin.setSuffix(" ms")
        cmd_debounce_spin.setValue(int(cmd_cfg.get('enter_debounce_ms', 200)))
        self._widgets['cmd_debounce'] = cmd_debounce_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Command debounce",
            _CMD_DEBOUNCE_DESC,
            cmd_debounce_spin,
            control_width=180,
            )
        )

        cmd_timeout_spin = QSpinBox()
        cmd_timeout_spin.setRange(5, 1800)
        cmd_timeout_spin.setSingleStep(5)
        cmd_timeout_spin.setSuffix(" s")
        cmd_timeout_spin.setValue(int(cmd_cfg.get(
            'inactivity_timeout_s',
            config_defaults.DEFAULTS['command_mode.inactivity_timeout_s'])))
        self._widgets['cmd_timeout'] = cmd_timeout_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Command timeout",
            _CMD_TIMEOUT_DESC,
            cmd_timeout_spin,
            control_width=180,
            )
        )

        # command_mode.tts_char_limit (queue 57). 0 = no limit, shown as
        # "No limit"; the spin shows the EFFECTIVE value, so a negative or
        # garbage config value reads as the default the app actually uses.
        from samsara.tts.coordinator import command_mode_char_limit  # noqa: PLC0415
        tts_limit_spin = QSpinBox()
        tts_limit_spin.setRange(0, 1000)
        tts_limit_spin.setSingleStep(10)
        tts_limit_spin.setSuffix(" chars")
        tts_limit_spin.setSpecialValueText("No limit")
        tts_limit_spin.setValue(command_mode_char_limit(cfg) or 0)
        self._widgets['cmd_tts_char_limit'] = tts_limit_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Spoken acknowledgement limit",
            _CMD_TTS_CHAR_LIMIT_DESC,
            tts_limit_spin,
            control_width=180,
            )
        )

        # command_mode.cancel_window_s / cancel_window_all_commands (queue
        # 69). The spin shows the EFFECTIVE value the policy uses, so a garbage
        # config value reads as the default; 0 is shown as "Off".
        from samsara.execution_policy import (  # noqa: PLC0415
            CANCEL_WINDOW_MAX_S, cancel_window_settings)
        from types import SimpleNamespace  # noqa: PLC0415
        _cw_seconds, _cw_everyday = cancel_window_settings(SimpleNamespace(config=cfg))
        cancel_window_spin = QDoubleSpinBox()
        cancel_window_spin.setRange(0.0, CANCEL_WINDOW_MAX_S)
        cancel_window_spin.setDecimals(1)
        cancel_window_spin.setSingleStep(0.5)
        cancel_window_spin.setSuffix(" s")
        cancel_window_spin.setSpecialValueText("Off")
        cancel_window_spin.setValue(_cw_seconds)
        self._widgets['cmd_cancel_window'] = cancel_window_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Cancel window for commands heard while dictating",
            _CMD_CANCEL_WINDOW_DESC,
            cancel_window_spin,
            control_width=180,
            )
        )
        cancel_window_all_cb = QCheckBox()
        cancel_window_all_cb.setChecked(_cw_everyday)
        self._widgets['cmd_cancel_window_all'] = cancel_window_all_cb
        adv_area_layout.addLayout(
            self._setting_row(
            "Also for everyday words (submit, enter, focus...)",
            _CMD_CANCEL_WINDOW_ALL_DESC,
            cancel_window_all_cb,
            control_width=180,
            )
        )

        cmd_miss_spin = QSpinBox()
        cmd_miss_spin.setRange(1, 20)
        cmd_miss_spin.setValue(int(cmd_cfg.get('miss_limit', 5)))
        self._widgets['cmd_miss_limit'] = cmd_miss_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Miss limit",
            _CMD_MISS_LIMIT_DESC,
            cmd_miss_spin,
            control_width=180,
            )
        )

        quick_silence_spin = QDoubleSpinBox()
        quick_silence_spin.setRange(0.2, 5.0)
        quick_silence_spin.setSingleStep(0.1)
        quick_silence_spin.setDecimals(1)
        quick_silence_spin.setSuffix(" s")
        quick_silence_spin.setValue(float(ww_cfg.get('quick_silence_timeout', 1.0)))
        self._widgets['quick_silence'] = quick_silence_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Quick silence timeout",
            "How soon silence ends wake-word listening.",
            quick_silence_spin,
            control_width=180,
            )
        )

        oww_spin = QDoubleSpinBox()
        oww_spin.setRange(0.05, 1.0)
        oww_spin.setSingleStep(0.05)
        oww_spin.setDecimals(2)
        oww_spin.setValue(float(ww_cfg.get('oww_threshold', 0.20)))
        self._widgets['oww_threshold'] = oww_spin
        adv_area_layout.addLayout(
            self._setting_row(
            "Wake-word threshold",
            "0.05 = more sensitive, 0.50 = stricter detection.",
            oww_spin,
            control_width=180,
            )
        )

        advanced_layout.addWidget(adv_button)
        advanced_layout.addWidget(adv_area)

        self._check_modes_collisions()

        def _save(_acc):
            updates = {}
            for key in ('hotkey', 'continuous_hotkey', 'wake_word_hotkey',
                        'command_hotkey', 'cancel_hotkey',
                        'undo_hotkey', 'dictate_commit_hotkey',
                        'memo_hotkey', 'correction_hotkey', 'continuous_commit_hotkey'):
                btn = self._widgets.get(key)
                if isinstance(btn, _HotkeyButton):
                    updates[key] = btn.combo

            # hotkeys.capture_correction is nested: read-merge-write the dict.
            capture_btn = self._widgets.get('capture_correction_hotkey')
            if isinstance(capture_btn, _HotkeyButton):
                hotkeys_cfg = dict(self.app.config.get('hotkeys', {}) or {})
                hotkeys_cfg['capture_correction'] = capture_btn.combo
                updates['hotkeys'] = hotkeys_cfg

            if 'continuous_commit_trigger' in self._widgets:
                updates['continuous_commit_trigger'] = self._widgets['continuous_commit_trigger'].currentText()

            # Ava mode: named key from the shared list; an unsupported stored
            # value is kept verbatim until the user picks a real key.
            ava_combo = self._widgets.get('ava_mode_key')
            if isinstance(ava_combo, QComboBox):
                label = ava_combo.currentText()
                if label in _AI_CMD_KEY_OPTIONS:
                    updates['ava_mode_key'] = _AI_CMD_KEY_OPTIONS[label]
                else:
                    updates['ava_mode_key'] = ava_combo.currentData() or self.app.config.get('ava_mode_key', 'right_alt')
            if 'ava_mode_enabled' in self._widgets:
                updates['ava_mode_enabled'] = self._widgets['ava_mode_enabled'].isChecked()

            # Streaming preview goes through the SAME path as the tray toggle
            # (set_streaming_mode installs/uninstalls the CapsLock hook and
            # saves the flag itself), never through the bulk config write.
            streaming_cb = self._widgets.get('streaming_mode')
            if streaming_cb is not None:
                wanted = bool(streaming_cb.isChecked())
                setter = getattr(self.app, 'set_streaming_mode', None)
                if wanted != bool(self.app.config.get('streaming_mode', False)) and callable(setter):
                    setter(wanted)

            if 'mode' in self._widgets:
                updates['mode'] = self._widgets['mode'].currentText()
            if 'wake_word_enabled' in self._widgets:
                updates['wake_word_enabled'] = self._widgets['wake_word_enabled'].isChecked()

            # Wake word nested config (threshold_mode/cal_multiplier are set
            # by the Advanced tab -- the natural single home).
            if 'wake_cmd_timeout' in self._widgets:
                ww_cfg = dict(self.app.config.get('wake_word_config', {}) or {})
                ww_audio = dict(ww_cfg.get('audio', {}) or {})
                ww_audio['wake_command_timeout'] = self._widgets['wake_cmd_timeout'].value()
                ww_cfg['audio'] = ww_audio
                ww_cfg['quick_silence_timeout'] = self._widgets['quick_silence'].value()
                ww_cfg['oww_threshold'] = max(0.01, min(1.0, self._widgets['oww_threshold'].value()))
                updates['wake_word_config'] = ww_cfg

            # Command mode nested config -- button/suppress_button now live
            # here alongside mode/debounce/timeout/miss_limit; single writer.
            if 'cmd_mode' in self._widgets:
                cmd_cfg = dict(self.app.config.get('command_mode', {}) or {})
                cmd_cfg['enabled'] = self._widgets['cmd_tab_enabled'].isChecked()
                cmd_cfg['mode'] = self._widgets['cmd_mode'].currentText()
                cmd_cfg['enter_debounce_ms'] = self._widgets['cmd_debounce'].value()
                cmd_cfg['inactivity_timeout_s'] = self._widgets['cmd_timeout'].value()
                cmd_cfg['miss_limit'] = self._widgets['cmd_miss_limit'].value()
                if 'cmd_tts_char_limit' in self._widgets:
                    cmd_cfg['tts_char_limit'] = self._widgets['cmd_tts_char_limit'].value()
                if 'cmd_cancel_window' in self._widgets:
                    cmd_cfg['cancel_window_s'] = round(self._widgets['cmd_cancel_window'].value(), 1)
                    cmd_cfg['cancel_window_all_commands'] = self._widgets['cmd_cancel_window_all'].isChecked()
                # Queue 62 keys.
                cmd_cfg['utterance_silence_s'] = round(self._widgets['cmd_utterance_silence'].value(), 2)
                cmd_cfg['dictate_utterance_silence_s'] = round(
                    self._widgets['cmd_dictate_utterance_silence'].value(), 2)
                cmd_cfg['exit_earcon'] = self._widgets['cmd_exit_earcon'].isChecked()
                cmd_cfg['preview_idle_delay_s'] = round(self._widgets['cmd_preview_idle_delay'].value(), 1)
                cmd_cfg['preview_idle_opacity'] = round(
                    self._widgets['cmd_preview_idle_opacity'].value() / 100.0, 2)
                cmd_cfg['abort_phrases'] = _abort_phrase_lines(
                    self._widgets['cmd_abort_phrases'].toPlainText())
                cmd_cfg['stop_phrases'] = _abort_phrase_lines(
                    self._widgets['cmd_stop_phrases'].toPlainText())
                matching = self._widgets['cmd_matching_enabled'].isChecked()
                cmd_cfg['command_matching_enabled'] = matching
                # commands.py gates on the attribute, set once at startup;
                # keep it in step so the change applies without a restart.
                if hasattr(self.app, 'command_matching_enabled'):
                    self.app.command_matching_enabled = matching
                btn_label = self._widgets['cmd_tab_button'].currentText()
                cmd_cfg['button'] = _CMD_BUTTON_OPTIONS.get(btn_label, 'rctrl')
                cmd_cfg['suppress_button'] = self._widgets['cmd_tab_suppress'].isChecked()
                updates['command_mode'] = cmd_cfg

            if 'ava_cmd_enabled' in self._widgets:
                ai_cfg_out = dict(self.app.config.get('ava_command_session', {}) or {})
                key_label = self._widgets['ava_cmd_key'].currentText()
                ai_cfg_out['enabled']         = self._widgets['ava_cmd_enabled'].isChecked()
                ai_cfg_out['key']             = _AI_CMD_KEY_OPTIONS.get(key_label, ai_cfg_out.get('key', 'left_alt'))
                ai_cfg_out['backend']         = 'cloud' if self._widgets['ava_cmd_backend'].currentText() == 'Cloud' else 'ollama'
                ai_cfg_out['model']           = self._widgets['ava_cmd_model'].text().strip()
                ai_cfg_out['keep_warm']       = self._widgets['ava_cmd_keep_warm'].isChecked()
                ai_cfg_out['queue_depth_cap'] = self._widgets['ava_cmd_queue_depth'].value()
                ai_cfg_out['miss_limit']      = self._widgets['ava_cmd_miss_limit'].value()
                updates['ava_command_session'] = ai_cfg_out

            staged_consent = getattr(self, "_ava_consent_staged", None)
            if staged_consent is not None:
                ava_cfg = dict(_acc.get("ava", self.app.config.get("ava", {})) or {})
                ava_cfg["consent"] = staged_consent
                updates["ava"] = ava_cfg

            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll
    def _check_modes_collisions(self) -> None:
        """Tab-wide activation-binding collision checker for the Modes tab.

        Collects every _HotkeyButton combo plus the command-mode button and
        the AI-command key, normalizes each into a set of per-token keys
        (so multi-key combos like 'ctrl+shift' compare correctly), then
        flags exact-duplicate sets as errors and strict-subset relationships
        (e.g. 'ctrl+shift' vs 'ctrl+shift+a') as "may shadow" warnings.
        Both severities render in the same amber banner -- there is no
        separate error styling in this UI, so treat exact duplicates as the
        more urgent wording within one shared message.
        """
        warn = self._widgets.get('modes_collision_warn')
        if warn is None:
            return

        hotkey_labels = {
            'hotkey':            'Record',
            'continuous_hotkey': 'Toggle continuous',
            'wake_word_hotkey':  'Toggle wake word',
            'command_hotkey':    'Command only',
            'cancel_hotkey':     'Cancel recording',
            'undo_hotkey':       'Undo',
            'dictate_commit_hotkey': 'Paste staged thought',
            'memo_hotkey':       'Voice memo',
            'correction_hotkey': 'Correction report',
            'capture_correction_hotkey': 'Correction capture',
            'continuous_commit_hotkey': 'Continuous commit',
        }
        label_to_key = {v: k for k, v in hotkey_labels.items()}

        bindings: list = []  # (label, raw_combo)
        for config_key, label in hotkey_labels.items():
            widget = self._widgets.get(config_key)
            if isinstance(widget, _HotkeyButton) and widget.combo:
                bindings.append((label, widget.combo))

        ava_combo = self._widgets.get('ava_mode_key')
        if isinstance(ava_combo, QComboBox):
            raw = _AI_CMD_KEY_OPTIONS.get(ava_combo.currentText()) or ava_combo.currentData()
            if raw:
                bindings.append(("Ava mode", str(raw)))

        cmd_btn_widget = self._widgets.get('cmd_tab_button')
        if cmd_btn_widget is not None:
            raw = _CMD_BUTTON_OPTIONS.get(cmd_btn_widget.currentText(), cmd_btn_widget.currentText())
            bindings.append(("Command Mode button", raw))

        ai_key_widget = self._widgets.get('ava_cmd_key')
        if ai_key_widget is not None:
            raw = _AI_CMD_KEY_OPTIONS.get(ai_key_widget.currentText(), ai_key_widget.currentText())
            bindings.append(("Ava Command Session key", raw))

        def _normalize(combo: str) -> frozenset:
            tokens = [t for t in combo.split('+') if t]
            return frozenset(_KEY_NORMALIZE.get(t, t) for t in tokens)

        normed = [(label, combo, _normalize(combo)) for label, combo in bindings]

        # Exact duplicates: group labels sharing an identical normalized set.
        by_norm: dict = {}
        for label, combo, norm in normed:
            by_norm.setdefault(norm, []).append(label)

        def _exempt(label_a: str, label_b: str) -> bool:
            keys = frozenset({label_to_key.get(label_a, label_a), label_to_key.get(label_b, label_b)})
            return keys in _MODES_COLLISION_EXEMPT_PAIRS

        messages: list = []
        for norm, labels in by_norm.items():
            if len(labels) == 2 and _exempt(*labels):
                continue   # live in different modes, never both armed
            if len(labels) > 1:
                messages.append(
                    f"{', '.join(labels)} all use the same key ({'+'.join(sorted(norm))}) "
                    "-- each must have a distinct activation binding."
                )

        # Strict-subset relationships ("may shadow"), skipping exact matches.
        for i, (label_a, combo_a, norm_a) in enumerate(normed):
            for label_b, combo_b, norm_b in normed[i + 1:]:
                if norm_a == norm_b or _exempt(label_a, label_b):
                    continue
                if norm_a < norm_b:
                    messages.append(
                        f'"{label_a}" ({combo_a}) may shadow "{label_b}" ({combo_b}).'
                    )
                elif norm_b < norm_a:
                    messages.append(
                        f'"{label_b}" ({combo_b}) may shadow "{label_a}" ({combo_a}).'
                    )

        if messages:
            warn.setText("Key collision: " + " ".join(messages))
            warn.setVisible(True)
        else:
            warn.setVisible(False)
