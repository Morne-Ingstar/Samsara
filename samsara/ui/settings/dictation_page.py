"""Dictation Settings destination (285; placeholder copy)."""

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
    return owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
