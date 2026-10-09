"""Dictation Settings destination."""

import copy
from samsara.config_schema import SETTINGS_SCHEMA
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget
from .presentation import SettingRow

TITLE = "Dictation"
SUBTITLE = "Choose how you start speaking and deliver text."
BASE = [
    [
        "Session button",
        "Enable hands-free sessions from your shortcut.",
        "cmd_tab_enabled",
        "command_mode.enabled"
    ],
    [
        "Wake activation",
        "Start a session by saying your wake phrase.",
        "wake_word_enabled",
        "wake_word_enabled"
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
    ]
]
ADVANCED = [
    [
        "Wake phrase",
        "Choose the trained activation phrase.",
        "wake_word_phrase",
        "wake_word_config.phrase"
    ],
    [
        "Recognition model",
        "Choose the speech recognition model.",
        "model_combo",
        "model_size"
    ],
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
        "Format spoken numbers as digits.",
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

def build(owner):
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    profiles = copy.deepcopy(owner.app.config.get("wake_profiles", []))
    if "wake_profiles" not in owner.app.config:
        profiles = copy.deepcopy(SETTINGS_SCHEMA["wake_profiles"]["default"])

    editor = QWidget()
    columns = QVBoxLayout(editor)
    columns.setContentsMargins(0, 0, 0, 0)
    columns.setSpacing(6)
    editor.setMinimumHeight(44 * (len(profiles) + 1))
    header = QHBoxLayout()
    header.addWidget(QLabel("Profile"), 1)
    header.addWidget(QLabel("Phrase"), 2)
    header.addWidget(QLabel("Target app (.exe)"), 2)
    header.addWidget(QLabel("Send word"), 1)
    header.addWidget(QLabel("On / Off"), 0)
    columns.addLayout(header)
    controls = []
    for profile in profiles:
        profile = profile if isinstance(profile, dict) else {}
        line = QHBoxLayout()
        line.addWidget(QLabel(str(profile.get("id", "Profile"))), 1)
        phrase = QLineEdit(str(profile.get("phrase", "")))
        phrase.setMinimumHeight(44)
        phrase.setAccessibleName(f"{profile.get('id', 'Profile')} wake phrase")
        line.addWidget(phrase, 2)
        target = QLineEdit(str(profile.get("target_process", "")))
        target.setMinimumHeight(44)
        target.setAccessibleName(f"{profile.get('id', 'Profile')} target app executable")
        line.addWidget(target, 2)
        send_word = QLineEdit(str(profile.get("send_word", "")))
        send_word.setMinimumHeight(44)
        send_word.setAccessibleName(f"{profile.get('id', 'Profile')} send word")
        line.addWidget(send_word, 1)
        enabled = QCheckBox("On" if profile.get("enabled", True) else "Off")
        enabled.setMinimumHeight(44)
        enabled.setAccessibleName(f"{profile.get('id', 'Profile')} wake profile")
        enabled.setChecked(profile.get("enabled", True))
        enabled.toggled.connect(lambda checked, button=enabled: button.setText("On" if checked else "Off"))
        line.addWidget(enabled)
        columns.addLayout(line)
        controls.append({"id": profile.get("id"), "phrase": phrase, "target_process": target,
                         "send_word": send_word, "enabled": enabled})

    description = "Edit the wake phrase, target app, and send word for each profile. Turn a profile off when you do not want Samsara to respond to it."
    row = SettingRow("Wake profiles", "Choose the phrases that open your target apps.", [editor],
                     SETTINGS_SCHEMA["wake_profiles"].get("help"))
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
    return page
