"""Dictation Settings destination."""

import copy
from functools import wraps
from samsara.config_defaults import cfg_get
from samsara.config_schema import SETTINGS_SCHEMA
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QLineEdit, QWidget, QSizePolicy, QPushButton, QVBoxLayout
from .presentation import SettingRow, CleanComboBox
from .audio_page import open_mic_setup_guide as open_microphone_guide

TITLE = "Dictation"
SUBTITLE = "Choose how you start speaking and deliver text."

_DEFAULT_PASTE_WORD = SETTINGS_SCHEMA["command_mode.dictate_commit_word"]["default"]
_HANDS_FREE_SENDS = (
    "As I speak types each sentence when you pause. When I say send holds your words "
    "in the display until you say '{word}' or choose Insert text."
)

BASE = [
    [
        "Cleanup mode",
        "Choose how dictated text is cleaned up.",
        "cleanup_mode",
        "cleanup_mode"
    ],
    [
        "Insertion / output",
        "Paste completed text, or keep it on the clipboard.",
        "auto_paste",
        "auto_paste"
    ],
    [
        "Capitalisation",
        "Capitalise dictated text automatically.",
        "auto_capitalize",
        "auto_capitalize"
    ],
    [
        "Number formatting",
        "Writes numbers you dictate as digits (twenty -> 20). Commands understand both either way.",
        "format_numbers",
        "format_numbers"
    ],
    [
        "Trailing space",
        "Add a space after inserted dictation.",
        "trailing_space",
        "add_trailing_space"
    ],
    [
        "Automatic corrections",
        "Enable the separate automatic cleanup pass.",
        "sc_enabled",
        "smart_corrections.enabled"
    ],
    [
        "Commands while dictating",
        "Recognise commands in a dictation session.",
        "cmd_matching_enabled",
        "command_mode.command_matching_enabled"
    ],
    [
        "Recognition model",
        "Choose the speech recognition model.",
        "model_combo",
        "model_size"
    ],
    [
        "Performance",
        "Balance recognition speed and accuracy.",
        "adv_perf_mode",
        "performance_mode"
    ],
    [
        "Session button",
        "Enable hands-free sessions from your shortcut.",
        "cmd_tab_enabled",
        "command_mode.enabled"
    ],
    # PLACEHOLDER COPY (1076): name the active paste word.
    ["Hands-free sends", _HANDS_FREE_SENDS.format(word=_DEFAULT_PASTE_WORD),
     "hands_free_send_mode", "hands_free.send_mode"],
    [
        "Wake activation",
        "Start a session by saying your wake phrase.",
        "wake_word_enabled",
        "wake_word_enabled"
    ],
    [
        "Wake phrase",
        "Choose the trained activation phrase.",
        "wake_word_phrase",
        "wake_word_config.phrase"
    ],
    [
        "How long you can pause",
        "Choose a pause in seconds.",
        "speech_pace",
        "accessibility.speech_pause_s"
    ],
]
ADVANCED = [
    [
        "Whole commands while dictating",
        "Use the command lane for commands that need an app, number or other argument.",
        "cmd_dictation_literal_only",
        "command_mode.dictation_literal_commands_only"
    ],
    [
        "Correction model",
        "Use this local model for automatic corrections.",
        "sc_model",
        "smart_corrections.ollama_model"
    ],
    [
        "Correct hold dictation",
        "Apply automatic corrections to held dictation.",
        "sc_mode_hotkey",
        "smart_corrections.modes.hotkey"
    ],
    [
        "Correct wake dictation",
        "Apply automatic corrections to wake dictation.",
        "sc_mode_wake",
        "smart_corrections.modes.wake"
    ],
    [
        "Correct streaming",
        "Apply automatic corrections to streaming dictation.",
        "sc_mode_streaming",
        "smart_corrections.modes.streaming"
    ],
    [
        "Repair disfluencies",
        "Remove filler words and spoken self-corrections.",
        "sc_repair_disfluencies",
        "smart_corrections.repair_disfluencies"
    ],
    [
        "Command pause",
        "Wait before completing a command phrase.",
        "cmd_utterance_silence",
        "command_mode.utterance_silence_s"
    ],
    [
        "Wake threshold",
        "Adjust wake detection sensitivity.",
        "oww_threshold",
        "wake_word_config.oww_threshold"
    ],
    [
        "Continuous preset",
        "Keep the existing continuous insertion trigger.",
        "continuous_commit_trigger",
        "continuous_commit_trigger"
    ],
    [
        "Microphone calibration",
        "Choose automatic or manual calibration.",
        "adv_threshold_mode",
        "threshold_mode"
    ],
    [
        "Calibration multiplier",
        "Adjust automatic microphone calibration.",
        "adv_cal_multiplier",
        "cal_multiplier"
    ],
    [
        "Manual threshold",
        "Set the speech threshold for manual calibration.",
        "adv_manual_threshold",
        "wake_word_config.audio.speech_threshold"
    ],
    [
        "Stop words",
        "Customise the session's stop words.",
        "cmd_stop_phrases",
        "command_mode.stop_phrases"
    ],
    [
        "Exit phrases",
        "Add phrases that leave a session.",
        "cmd_abort_phrases",
        "command_mode.abort_phrases"
    ],
    [
        "Command debounce",
        "Avoid repeated activation from one press.",
        "cmd_debounce",
        "command_mode.enter_debounce_ms"
    ],
    [
        "Session timeout",
        "End a session after inactivity.",
        "cmd_timeout",
        "command_mode.inactivity_timeout_s"
    ],
    [
        "Command timing",
        "Ordinary commands run immediately. Private messages have a 1 s sending grace; "
        "email and security, privacy or irreversible effects ask once. No answer cancels.",
        "cmd_timing_policy",
        ""
    ],
    [
        "Command miss limit",
        "Stop after repeated unrecognised commands.",
        "cmd_miss_limit",
        "command_mode.miss_limit"
    ],
    [
        "Command word",
        "Run a command by speaking this prefix first.",
        "adv_intent_command_prefix",
        "intent.command_prefix"
    ],
    [
        "Scroll the app you're using, not the one under the mouse",
        "Move the pointer into the active app before voice scrolling when needed.",
        "scroll_target_foreground",
        "scroll.target_foreground"
    ],
    [
        "Idle preview",
        "Delay before fading, then how visible the idle preview stays.",
        "cmd_preview_idle_delay,cmd_preview_idle_opacity",
        "command_mode.preview_idle_delay_s,command_mode.preview_idle_opacity"
    ],
    ["Game mode", "Use a resident CPU model with live words off while gaming; switch only when idle.", "game_enabled", "game_mode.enabled"],
    ["Game detection", "Auto stays on across alt-tabs; it leaves after the game is closed or minimized for 45 seconds. Manual uses the tray or voice controls.", "game_detect", "game_mode.detect"],  # PLACEHOLDER COPY (1152)
    ["Game CPU model", "A second model, loaded on first use and kept in memory.", "game_model", "game_mode.cpu_model"],
    [
        "Compute device",
        "Saved preference. Apply, then restart to change the usual device.",  # PLACEHOLDER COPY (1152)
        "adv_device",
        "device"
    ],
]

GROUPS = {
    'cleanup_mode': 'Text behavior',
    'model_combo': 'Speech model',
    'cmd_tab_enabled': 'Hands-free sessions',
    'cmd_dictation_literal_only': 'Text behavior',
    'cmd_utterance_silence': 'Timing and sensitivity',
    'continuous_commit_trigger': 'Continuous dictation',
    'adv_threshold_mode': 'Microphone calibration',
    'cmd_stop_phrases': 'Session controls',
    'game_enabled': None,
    'adv_device': 'Hardware acceleration',
    'wake_profiles': None,
}


class _WakeProfilesEditor(QWidget):
    """Keep the wide table, stacking each profile at narrow page widths."""
    def __init__(self):
        super().__init__()
        self.setObjectName("wakeProfilesEditor")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        # PLACEHOLDER COPY (1076)
        self.add_button = self.profile_button("Add wake profile")
        layout.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.empty = QLabel("No wake profiles yet.")
        self.empty.setProperty("role", "description")
        layout.addWidget(self.empty)
        self.columns = QGridLayout()
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(6)
        layout.addLayout(self.columns)
        self.entries = []
        self._narrow = None

    @staticmethod
    def profile_button(label):
        from .calm_layout import _style_row_button

        button = QPushButton(label)
        button.setProperty("settingsToolAction", True)
        button.setMinimumSize(44, 44)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        _style_row_button(button)
        return button

    def reflow(self, *, force=False):
        narrow = self.width() < 650
        if narrow == self._narrow and not force:
            return
        self._narrow = narrow
        while self.columns.count():
            self.columns.takeAt(0)
        for column in range(6):
            self.columns.setColumnStretch(column, 0)
        self.empty.setVisible(not self.entries)
        for index, (headers, fields) in enumerate(self.entries):
            for column, (header, field) in enumerate(zip(headers, fields)):
                alignment = Qt.AlignmentFlag.AlignVCenter
                if header is None:
                    alignment |= Qt.AlignmentFlag.AlignLeft
                else:
                    header.setVisible(narrow or index == 0)
                if narrow:
                    line = index * 4
                    positions = ((line, 0), (line, 1), (line + 2, 0),
                                 (line + 2, 1), (line, 2), (line + 2, 2))
                    row, col = positions[column]
                    if header is not None:
                        self.columns.addWidget(header, row, col)
                    self.columns.addWidget(field, row + 1, col, alignment)
                else:
                    if index == 0 and header is not None:
                        self.columns.addWidget(header, 0, column)
                    self.columns.addWidget(field, index + 1, column, alignment)
        for column, stretch in enumerate((2, 2, 1) if narrow else (1, 2, 2, 1, 0, 0)):
            self.columns.setColumnStretch(column, stretch)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


def refresh_corrections_prerequisite(owner):
    from samsara import ai_preferences
    from .ava_cloud_qt import _CODE_TO_DISPLAY

    widgets = owner._widgets
    saved = owner.app.config
    effective = saved
    if "providerRoute" in widgets:
        effective = owner._ava_staged_config()
        effective["ava_edit"] = {"enabled": widgets["ava_edit_enabled"].isChecked()}
        # The staged Ava form assumes provider opt-ins from the selected route.
        # Use the actual opt-ins, including a disabled provider in old configs.
        for provider in ("ollama", "cloud_llm"):
            effective[provider]["enabled"] = (saved.get(provider) or {}).get("enabled")
    state = ai_preferences.runtime_state(effective, "editing")
    text = state.reason
    if state.allowed:
        identity = state.provider.identity
        name = "Ollama" if identity == "ollama" else _CODE_TO_DISPLAY[identity].removesuffix(" (default)")
        # PLACEHOLDER COPY (1076)
        text = f"Runs through {name}."
    widgets["sc_prerequisite"].setText(text)


def build(owner):
    sends = CleanComboBox()
    sends.addItem("When I say send", "staged")
    sends.addItem("As I speak", "as_i_speak")
    current = (owner.app.config.get('hands_free') or {}).get('send_mode', 'staged')
    sends.setCurrentIndex(max(0, sends.findData(current)))
    sends.setAccessibleName('Hands-free sends')
    owner._widgets['hands_free_send_mode'] = sends

    save_general = owner._source_saves['general'][0]

    @wraps(save_general)
    def save_sends(_updates):
        updates = save_general(_updates)
        hands_free = dict(updates.get('hands_free') or owner.app.config.get('hands_free') or {})
        hands_free['send_mode'] = sends.currentData()
        updates['hands_free'] = hands_free
        return updates

    owner._source_saves['general'][0] = save_sends
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    sends_row = owner._rows_by_widget["hands_free_send_mode"]
    paste_word = cfg_get(owner.app.config, "command_mode.dictate_commit_word") or _DEFAULT_PASTE_WORD
    description = _HANDS_FREE_SENDS.format(word=paste_word)
    sends_row.full_help = sends_row.full_help.replace(sends_row.description, description, 1)
    sends_row.description = description
    sends_row.detail.setTextFormat(Qt.TextFormat.PlainText)
    sends_row.detail.setText(description)

    corrections_row = owner._rows_by_widget["sc_enabled"]
    prerequisite = QLabel()
    prerequisite.setProperty("role", "description")
    prerequisite.setTextFormat(Qt.TextFormat.PlainText)
    prerequisite.setWordWrap(True)
    corrections_row.text.layout().addWidget(prerequisite)
    owner._widgets["sc_prerequisite"] = prerequisite
    from .game_mode import build_runtime
    build_runtime(owner)
    # PLACEHOLDER COPY (1015): microphone guide stays available in the header.
    owner._widgets['dictation_mic_setup_guide_button'] = page.add_guide(
        'Microphone setup',
        lambda: open_microphone_guide(owner.app))
    pace = owner._widgets['speech_pace']
    # _take_control resets source widths; restore the shared slider's extent.
    pace.setFixedWidth(390)
    pace.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    pace_row = owner._rows_by_widget['speech_pace']
    # PLACEHOLDER COPY (1040): scope line remains visible for every value.
    scope = "Affects hands-free, wake word and commands. Held dictation isn't split by pauses — stray full stops there come from the recognizer; use 'clean up' or the Dictation accuracy check."
    explanation = QLabel(scope, pace_row)
    explanation.setObjectName('speechPaceExplanation')
    explanation.setProperty('role', 'description')
    explanation.setWordWrap(True)
    pace_row.detail.hide()
    pace_row.grid.addWidget(explanation, 2, 0, 1, 3)
    try_button = QPushButton('Try it')
    try_button.setFixedHeight(32)
    try_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    pace_row.values.insertWidget(1, try_button)
    owner._widgets['speech_pace_try'] = try_button

    def open_pace_trial():
        from samsara.ui.tutorial_try_qt import open_pace_trial
        owner._pace_trial = open_pace_trial(owner.app, pace, parent=owner)

    try_button.clicked.connect(open_pace_trial)
    # Advanced includes Game mode's save before its legacy timing controls.
    # Derive after the final save so no hidden control can overwrite the pace.
    from samsara.speech_pace import with_pause
    save_advanced = owner._source_saves['advanced'][-1]

    @wraps(save_advanced)
    def save_pace(acc):
        updates = save_advanced(acc)
        merged = copy.deepcopy(owner.app.config)
        merged.update(acc)
        merged.update(updates)
        # Use original endpoint values, not the hidden spin boxes' rounded values.
        migrated = with_pause(owner.app.config, pace.seconds(), preserve_legacy=not pace.changed)
        for group in ('accessibility', 'command_mode', 'wake_word_config'):
            target = copy.deepcopy(merged.get(group, {}) or {})
            source = migrated[group]
            if group == 'accessibility':
                target.pop('speech_pause_anchor', None)
                target.update({key: value for key, value in source.items()
                               if key.startswith('speech_pa')})
            elif group == 'command_mode':
                target['dictate_utterance_silence_s'] = source['dictate_utterance_silence_s']
            else:
                target['quick_silence_timeout'] = source['quick_silence_timeout']
                target.setdefault('audio', {})['wake_command_timeout'] = source['audio']['wake_command_timeout']
            updates[group] = target
        for key in ('silence_threshold', 'min_speech_duration'):
            updates[key] = migrated[key]
        return updates

    owner._source_saves['advanced'][-1] = save_pace
    _add_wake_profiles(owner, page)
    from .calm_layout import GroupHeading, group_page
    group_page(page, GROUPS)
    from .presentation import GuideHeader
    from samsara.ui import theme

    def open_wake_guide():
        from samsara.ui.mic_setup_wizard_qt import open_wake_calibration
        open_wake_calibration(owner.app, parent=owner)

    # Wake settings share this page. Give their existing section one action,
    # using exactly the same title/action treatment as the page header.
    for index in range(page.base.count()):
        heading = page.base.itemAt(index).widget()
        if isinstance(heading, GroupHeading) and heading.text() == GROUPS['cmd_tab_enabled']:
            page.base.removeWidget(heading)
            heading.setContentsMargins(0, 0, 0, 0)
            # PLACEHOLDER COPY (1015)
            page.wake_guide_header = GuideHeader(
                heading, 'Wake word setup', open_wake_guide, parent=page.container)
            page.wake_guide_header.setContentsMargins(0, theme.SPACE_4, 0, theme.SPACE_2)
            page.base.insertWidget(index, page.wake_guide_header)
            owner._widgets['wake_setup_guide_button'] = page.wake_guide_header.button
            break
    return page


def _add_wake_profiles(owner, page):
    from .calm_controls import switch

    profiles = cfg_get(owner.app.config, "wake_profiles")
    editor = _WakeProfilesEditor()
    controls = []
    description = "Edit the wake phrase, target app, and send word for each profile. Turn a profile off when you do not want Samsara to respond to it."
    row = SettingRow("Wake profiles", "Choose the phrases that open your target apps.", [],
                     SETTINGS_SCHEMA["wake_profiles"].get("help"))
    # Keep the editor below the semantic heading/help, outside the value column.
    row.grid.addWidget(editor, 2, 0, 1, 3)
    row.config_keys = ("wake_profiles",)
    row.widget_keys = ("wake_profiles",)
    row.page_name = TITLE
    row.disclosure = page.advanced
    row._wake_profile_controls = controls
    owner._widgets["wake_profile_controls"] = controls
    owner._widgets["wake_profile_add"] = editor.add_button
    page.add_row(row, advanced=True)
    owner._destination_rows.append(row)
    owner._rows_by_widget["wake_profiles"] = row

    def update_search_text(*_args):
        values = " ".join(
            " ".join(control[key].text() for key in ("phrase", "target_process", "send_word"))
            for control in controls
        )
        row.full_help = f"{description} {values} profile wake"

    def append_profile(profile, *, new=False):
        profile_id = profile.get("id")
        name = str(profile.get("id", "Profile"))
        control = {"id": profile_id, "new": new}
        for key, label in (("phrase", "wake phrase"), ("target_process", "target app executable"),
                           ("send_word", "send word")):
            field = QLineEdit(str(profile.get(key, "")), editor)
            field.setAccessibleName(f"{name} {label}")
            field.setFixedHeight(44)
            field.setMinimumWidth(100)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            field.textChanged.connect(update_search_text)
            control[key] = field
        enabled = switch(QCheckBox(editor))
        enabled.setAccessibleName(f"{name} wake profile")
        enabled.setChecked(profile.get("enabled", True))
        control["enabled"] = enabled
        # PLACEHOLDER COPY (1076)
        remove = editor.profile_button("Remove")
        remove.setAccessibleName(f"Remove wake profile {name}")
        control["remove"] = remove
        headers = [QLabel(text, editor) for text in
                   ("Profile", "Phrase", "Target app (.exe)", "Send word", "On / Off")] + [None]
        fields = (QLabel(name, editor), control["phrase"], control["target_process"],
                  control["send_word"], enabled, remove)
        entry = (headers, fields)
        editor.entries.append(entry)
        controls.append(control)

        def remove_profile():
            controls.remove(control)
            editor.entries.remove(entry)
            for widget in (*headers[:-1], *fields):
                widget.hide()
                widget.deleteLater()
            editor.reflow(force=True)
            update_search_text()
            editor.add_button.setFocus()

        remove.clicked.connect(remove_profile)
        return control["phrase"]

    def add_profile():
        ids = {control["id"] for control in controls}
        number = 1
        while f"profile-{number}" in ids:
            number += 1
        phrase = append_profile({"id": f"profile-{number}"}, new=True)
        editor.reflow(force=True)
        update_search_text()
        phrase.setFocus()

    for profile in profiles:
        if isinstance(profile, dict):
            append_profile(profile)
    editor.add_button.clicked.connect(add_profile)
    editor.reflow()
    update_search_text()
