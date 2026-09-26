"""Dictation Settings destination."""

import copy
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget
from .presentation import SettingRow

TITLE = "Dictation"
SUBTITLE = "Choose how you start speaking and deliver text."
BASE = [
    [
        "Session button",
        "Enable hands-free sessions from your shortcut.",
        "cmd_tab_enabled",
        "command_mode.enabled",
        "Turn this on when you want to use hands-free sessions. You can then start a session with the session button you choose in Shortcuts."
    ],
    [
        "Wake activation",
        "Start a session by saying your wake phrase.",
        "wake_word_enabled",
        "wake_word_enabled",
        "Turn this on to begin a session by saying your chosen wake phrase. Turn it off when you want Samsara to listen only after a button press."
    ],
    [
        "Speech pace",
        "Allow time for pauses in your speech.",
        "speech_pace",
        "accessibility.speech_pace",
        "Choose the pace that gives you enough room to pause naturally while speaking. A slower pace is useful when you need more time between words or phrases."
    ],
    [
        "Cleanup mode",
        "Choose how dictated text is cleaned up.",
        "cleanup_mode",
        "cleanup_mode",
        "Choose how much cleanup Samsara applies before it delivers your words. Pick the least processing that gives you text you are comfortable sending."
    ],
    [
        "Insertion / output",
        "Paste completed text, or keep it on the clipboard.",
        "auto_paste",
        "auto_paste",
        "Choose whether completed dictation is pasted into the active text box or left on the clipboard. Use the clipboard option when you want to review or place the text yourself."
    ],
    [
        "Commands while dictating",
        "Recognise commands in a dictation session.",
        "cmd_matching_enabled",
        "command_mode.command_matching_enabled",
        "Turn this on if you want spoken commands to work while you are dictating normally. Turn it off if words that sound like commands should always be entered as text."
    ]
]
ADVANCED = [
    [
        "Wake phrase",
        "Choose the trained activation phrase.",
        "wake_word_phrase",
        "wake_word_config.phrase",
        "Choose the phrase you will say to wake Samsara. Use a phrase you can say comfortably and that is unlikely to appear in ordinary conversation."
    ],
    [
        "Recognition model",
        "Choose the speech recognition model.",
        "model_combo",
        "model_size",
        "Choose the speech-recognition model Samsara uses for your dictation. Larger models can be more accurate but need more computer resources and may respond more slowly."
    ],
    [
        "Compute device",
        "Choose the hardware used for recognition.",
        "adv_device",
        "device",
        "Choose the hardware Samsara uses to recognise your speech. Leave the current choice in place unless you know another device is available and works better for you."
    ],
    [
        "Precision metadata",
        "Stored diagnostic precision; the loader chooses actual precision.",
        "computePrecisionStatus",
        "compute_type",
        "This shows diagnostic information about how recognition is configured. You do not need to change it for ordinary dictation."
    ],
    [
        "Performance",
        "Balance recognition speed and accuracy.",
        "adv_perf_mode",
        "performance_mode",
        "Choose whether you prefer faster results or more time for recognition to refine them. Try a different setting only if the current speed or accuracy is not working for you."
    ],
    [
        "Trailing space",
        "Add a space after inserted dictation.",
        "trailing_space",
        "add_trailing_space",
        "Turn this on to add a space after Samsara inserts completed text. Turn it off if you usually dictate into places where you want to control spacing yourself."
    ],
    [
        "Capitalisation",
        "Capitalise dictated text automatically.",
        "auto_capitalize",
        "auto_capitalize",
        "Turn this on to start dictated sentences with a capital letter automatically. Turn it off if the program you dictate into handles capitalisation for you."
    ],
    [
        "Number formatting",
        "Format spoken numbers as digits.",
        "format_numbers",
        "format_numbers",
        "Turn this on to turn spoken numbers into digits when Samsara is confident about them. Turn it off if you prefer numbers to stay as words."
    ],
    [
        "Timeout after wake",
        "Wait for speech after wake activation.",
        "wake_cmd_timeout",
        "wake_word_config.audio.wake_command_timeout",
        "Choose how long Samsara waits for you to begin speaking after it hears your wake phrase. Increase it if you need extra time before starting your request."
    ],
    [
        "Command pause",
        "Wait before completing a command phrase.",
        "cmd_utterance_silence",
        "command_mode.utterance_silence_s",
        "Choose how long a pause tells Samsara that you have finished a spoken command. A longer pause gives you more time to speak; a shorter pause makes commands run sooner."
    ],
    [
        "Dictation pause",
        "Wait before completing a dictation phrase.",
        "cmd_dictate_utterance_silence",
        "command_mode.dictate_utterance_silence_s",
        "Choose how long a pause closes one piece of hands-free dictation. A longer pause keeps more words together, while a shorter pause can split your speech into smaller pieces."
    ],
    [
        "Quick silence timeout",
        "Adjust recognition silence timing.",
        "quick_silence",
        "wake_word_config.quick_silence_timeout",
        "Choose how quickly Samsara treats a quiet gap as silence while recognising speech. Change this only if normal pauses are being cut off or recognition waits too long."
    ],
    [
        "Wake threshold",
        "Adjust wake detection sensitivity.",
        "oww_threshold",
        "wake_word_config.oww_threshold",
        "Choose how readily Samsara responds to your wake phrase. A lower threshold may hear the phrase more easily, while a higher threshold can reduce accidental wake-ups."
    ],
    [
        "Continuous preset",
        "Keep the existing continuous insertion trigger.",
        "continuous_commit_trigger",
        "continuous_commit_trigger",
        "Choose how an existing continuous dictation session commits speech. This setting is kept for people who already use continuous mode and does not change the primary dictation key."
    ],
    [
        "Continuous silence",
        "Silence before a continuous phrase completes.",
        "adv_silence",
        "silence_threshold",
        "Choose how much silence ends a phrase in an existing continuous dictation session. Increase it if you need longer pauses before a phrase completes."
    ],
    [
        "Minimum speech",
        "Ignore speech shorter than this duration.",
        "adv_min_speech",
        "min_speech_duration",
        "Choose the shortest speech Samsara should accept as a phrase. Increase it if brief sounds are being treated as dictation, or decrease it if short words are missed."
    ],
    [
        "Microphone calibration",
        "Choose automatic or manual calibration.",
        "adv_threshold_mode",
        "threshold_mode",
        "Choose whether Samsara adjusts microphone sensitivity automatically or uses a setting you provide. Automatic calibration is the usual choice unless you are tuning a known microphone problem."
    ],
    [
        "Calibration multiplier",
        "Adjust automatic microphone calibration.",
        "adv_cal_multiplier",
        "cal_multiplier",
        "Adjust how strongly automatic microphone calibration responds to your environment. Change it gradually and test ordinary dictation before making another adjustment."
    ],
    [
        "Manual threshold",
        "Set the speech threshold for manual calibration.",
        "adv_manual_threshold",
        "wake_word_config.audio.speech_threshold",
        "Set the microphone sensitivity yourself when you use manual calibration. Change it only if automatic calibration does not respond reliably in your usual environment."
    ],
    [
        "Stop words",
        "Customise the session's stop words.",
        "cmd_stop_phrases",
        "command_mode.stop_phrases",
        "Add words you want to use to stop what Samsara is doing. Put one phrase on each line and keep the words easy to say in an emergency."
    ],
    [
        "Exit phrases",
        "Add phrases that leave a session.",
        "cmd_abort_phrases",
        "command_mode.abort_phrases",
        "Add extra phrases that should leave a hands-free session. Put one phrase on each line and avoid phrases you might say while dictating normally."
    ],
    [
        "Command debounce",
        "Avoid repeated activation from one press.",
        "cmd_debounce",
        "command_mode.enter_debounce_ms",
        "Choose how long Samsara ignores repeated button presses after one press. Increase it only if one press is accidentally starting more than one action."
    ],
    [
        "Session timeout",
        "End a session after inactivity.",
        "cmd_timeout",
        "command_mode.inactivity_timeout_s",
        "Choose how long an unused hands-free session stays open before it ends. A longer timeout gives you more time between requests, but leaves the session available for longer."
    ],
    [
        "Cancel window",
        "Allow time to cancel commands heard during dictation.",
        "cmd_cancel_window,cmd_cancel_window_all",
        "command_mode.cancel_window_s,command_mode.cancel_window_all_commands",
        "Choose whether Samsara briefly waits before it runs a command heard while you are dictating. Use the extra time when you want a chance to say no or cancel."
    ],
    [
        "Command miss limit",
        "Stop after repeated unrecognised commands.",
        "cmd_miss_limit",
        "command_mode.miss_limit",
        "Choose how many unrecognised requests a hands-free session accepts before it stops. Lower it if you want a session to end quickly after repeated misunderstandings."
    ],
    [
        "Command word",
        "Run a command by speaking this prefix first.",
        "adv_intent_command_prefix",
        "intent.command_prefix",
        "Choose the word you say before a spoken command. Pick a word that is easy to remember and unlikely to occur in the text you dictate."
    ]
]

def build(owner):
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    profiles = copy.deepcopy(owner.app.config.get("wake_profiles", []))
    if "wake_profiles" not in owner.app.config:
        from samsara.config_schema import SETTINGS_SCHEMA
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
                     "Edit the wake phrase you say, the app executable Samsara opens, and the word you use to send. Turn each profile on when you want Samsara to listen for it, or off when you do not.")
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
