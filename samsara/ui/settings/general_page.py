"""General Settings destination (285; placeholder copy)."""
from samsara.listening_display import LABEL, QUESTION

TITLE = "General"
SUBTITLE = "Make Samsara comfortable for everyday use."
BASE = [
    [
        "Interface size",
        "Size text and controls for comfortable use.",
        "ui_scale_combo",
        "ui_scale"
    ],
    [
        "Theme",
        "Choose Samsara's appearance.",
        "theme_combo",
        "ui.theme"
    ],
    [
        "Microphone",
        "Choose where Samsara listens.",
        "mic_combo,microphoneRefreshButton",
        "microphone,microphone_name"
    ],
    [
        "Language",
        "Choose the language you dictate in.",
        "lang_combo",
        "language"
    ],
    [
        "Contextual hints",
        "Show guidance as you learn.",
        "hints_enabled,action:Reset hints",
        "hints_enabled"
    ]
]
ADVANCED = [
    [LABEL, QUESTION, "listening_display", "ui.listening_display"],
    [
        "Start at sign-in",
        "Launch Samsara when you sign in to Windows.",
        "launch_at_login",
        "launch_at_login"
    ],
    [
        "Start hidden",
        "Start in the tray with the main window hidden.",
        "start_hidden",
        "start_hidden"
    ],
    [
        "Indicator position",
        "Choose the indicator's starting position.",
        "adv_indicator_pos",
        "listening_indicator_position"
    ],
    [
        "Floating windows",
        "Recover the indicator or dictation preview.",
        "floatingWindowActions",
        ""
    ],
    [
        "Preview fades after",
        "Delay before the idle dictation preview fades.",
        "cmd_preview_idle_delay",
        "command_mode.preview_idle_delay_s"
    ],
    [
        "Faded preview visibility",
        "Choose how visible the idle preview stays.",
        "cmd_preview_idle_opacity",
        "command_mode.preview_idle_opacity"
    ],
    [
        "Streaming capture",
        "Use the existing dedicated CapsLock capture.",
        "streaming_mode,streaming_key_display",
        "streaming_mode"
    ]
]

def build(owner):
    return owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
