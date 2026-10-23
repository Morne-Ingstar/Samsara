"""Dictation Settings destination."""

import copy
from functools import wraps
from samsara.config_schema import SETTINGS_SCHEMA
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QLineEdit, QWidget, QSizePolicy
from .presentation import SettingRow, CleanComboBox

TITLE = "Dictation"
SUBTITLE = "Choose how you start speaking and deliver text."
BASE = [
    [
        "Session button",
        "Enable hands-free sessions from your shortcut.",
        "cmd_tab_enabled",
        "command_mode.enabled"
    ],
    ["Hands-free sends",
     "As I speak types each sentence when you pause. When I say send holds your words in the display until you say your paste word or choose Insert text.",
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
        "Speech pace",
        "Allow time for pauses in your speech.",
        "speech_pace",
        "accessibility.speech_pace"
    ],
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
        "Commands while dictating",
        "Recognise commands in a dictation session.",
        "cmd_matching_enabled",
        "command_mode.command_matching_enabled"
    ],
    [
        "Whole commands while dictating",
        "Use the command lane for commands that need an app, number or other argument.",
        "cmd_dictation_literal_only",
        "command_mode.dictation_literal_commands_only"
    ],
    [
        "Recognition model",
        "Choose the speech recognition model.",
        "model_combo",
        "model_size"
    ]
]
ADVANCED = [
    [
        "Compute device",
        "Choose the hardware used for recognition.",
        "adv_device",
        "device"
    ],
    [
        "Precision metadata",
        "Stored diagnostic precision; the loader chooses actual precision.",
        "computePrecisionStatus",
        "compute_type"
    ],
    [
        "Performance",
        "Balance recognition speed and accuracy.",
        "adv_perf_mode",
        "performance_mode"
    ],
    [
        "Trailing space",
        "Add a space after inserted dictation.",
        "trailing_space",
        "add_trailing_space"
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
        "Timeout after wake",
        "Wait for speech after wake activation.",
        "wake_cmd_timeout",
        "wake_word_config.audio.wake_command_timeout"
    ],
    [
        "Command pause",
        "Wait before completing a command phrase.",
        "cmd_utterance_silence",
        "command_mode.utterance_silence_s"
    ],
    [
        "Dictation pause",
        "Wait before completing a dictation phrase.",
        "cmd_dictate_utterance_silence",
        "command_mode.dictate_utterance_silence_s"
    ],
    [
        "Quick silence timeout",
        "Adjust recognition silence timing.",
        "quick_silence",
        "wake_word_config.quick_silence_timeout"
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
        "Continuous silence",
        "Silence before a continuous phrase completes.",
        "adv_silence",
        "silence_threshold"
    ],
    [
        "Minimum speech",
        "Ignore speech shorter than this duration.",
        "adv_min_speech",
        "min_speech_duration"
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
        "Cancel window",
        "Allow time to cancel commands heard during dictation.",
        "cmd_cancel_window,cmd_cancel_window_all",
        "command_mode.cancel_window_s,command_mode.cancel_window_all_commands"
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
    ]
]

GROUPS = {
    'cmd_tab_enabled': 'Hands-free sessions',
    'cleanup_mode': 'Text behavior',
    'model_combo': 'Speech model',
    'adv_device': 'Hardware acceleration',
    'trailing_space': 'Text behavior',
    'wake_cmd_timeout': 'Timing and sensitivity',
    'continuous_commit_trigger': 'Continuous dictation',
    'adv_threshold_mode': 'Microphone calibration',
    'cmd_stop_phrases': 'Session controls',
}


class _WakeProfilesEditor(QWidget):
    """Keep the wide table, stacking each profile at narrow page widths."""
    def __init__(self):
        super().__init__()
        self.setObjectName("wakeProfilesEditor")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.columns = QGridLayout(self)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(6)
        self.entries = []
        self._narrow = None

    def reflow(self):
        narrow = self.width() < 650
        if narrow == self._narrow:
            return
        self._narrow = narrow
        while self.columns.count():
            self.columns.takeAt(0)
        for column in range(5):
            self.columns.setColumnStretch(column, 0)
        for index, (headers, fields) in enumerate(self.entries):
            for column, (header, field) in enumerate(zip(headers, fields)):
                header.setVisible(narrow or index == 0)
                if narrow:
                    line = index * 4
                    positions = ((line, 0, 1), (line, 1, 1),
                                 (line + 2, 0, 2), (line + 2, 2, 1), (line, 2, 1))
                    row, col, span = positions[column]
                    self.columns.addWidget(header, row, col, 1, span)
                    self.columns.addWidget(field, row + 1, col, 1, span, Qt.AlignmentFlag.AlignVCenter)
                else:
                    if index == 0:
                        self.columns.addWidget(header, 0, column)
                    self.columns.addWidget(field, index + 1, column, Qt.AlignmentFlag.AlignVCenter)
        for column, stretch in enumerate((1, 2, 1) if narrow else (1, 2, 2, 1, 0)):
            self.columns.setColumnStretch(column, stretch)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


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
    profiles = copy.deepcopy(owner.app.config.get("wake_profiles", []))
    if "wake_profiles" not in owner.app.config:
        profiles = copy.deepcopy(SETTINGS_SCHEMA["wake_profiles"]["default"])

    editor = _WakeProfilesEditor()
    controls = []
    for profile in profiles:
        profile = profile if isinstance(profile, dict) else {}
        name = QLabel(str(profile.get("id", "Profile")))
        phrase = QLineEdit(str(profile.get("phrase", "")))
        phrase.setAccessibleName(f"{profile.get('id', 'Profile')} wake phrase")
        target = QLineEdit(str(profile.get("target_process", "")))
        target.setAccessibleName(f"{profile.get('id', 'Profile')} target app executable")
        send_word = QLineEdit(str(profile.get("send_word", "")))
        send_word.setAccessibleName(f"{profile.get('id', 'Profile')} send word")
        enabled = QCheckBox("On" if profile.get("enabled", True) else "Off")
        enabled.setMinimumHeight(44)
        enabled.setAccessibleName(f"{profile.get('id', 'Profile')} wake profile")
        enabled.setChecked(profile.get("enabled", True))
        enabled.toggled.connect(lambda checked, button=enabled: button.setText("On" if checked else "Off"))
        for field in (phrase, target, send_word):
            field.setFixedHeight(44)
            field.setMinimumWidth(100)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        headers = [QLabel(text, editor) for text in
                   ("Profile", "Phrase", "Target app (.exe)", "Send word", "On / Off")]
        editor.entries.append((headers, (name, phrase, target, send_word, enabled)))
        controls.append({"id": profile.get("id"), "phrase": phrase, "target_process": target,
                         "send_word": send_word, "enabled": enabled})

    editor.reflow()
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
    page.add_row(row, advanced=True)
    owner._destination_rows.append(row)
    owner._rows_by_widget["wake_profiles"] = row

    def update_search_text(*_args):
        values = " ".join(
            " ".join(control[key].text() for key in ("phrase", "target_process", "send_word"))
            for control in controls
        )
        row.full_help = f"{description} {values} profile wake"

    for control in controls:
        control["phrase"].textChanged.connect(update_search_text)
        control["target_process"].textChanged.connect(update_search_text)
        control["send_word"].textChanged.connect(update_search_text)
    update_search_text()
    from .calm_layout import group_page
    group_page(page, GROUPS)
    return page
