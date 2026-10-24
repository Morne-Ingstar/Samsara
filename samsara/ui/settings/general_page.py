"""General Settings destination (285; placeholder copy)."""
from samsara.listening_display import LABEL, QUESTION

TITLE = "General"
SUBTITLE = "Make Samsara comfortable for everyday use."
BASE = [
    [LABEL, QUESTION, "listening_display", "ui.listening_display"],
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
    ],
    [
        "Start at sign-in",
        "Launch Samsara when you sign in to Windows.",
        "launch_at_login",
        "launch_at_login"
    ]
]
ADVANCED = [
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
    # PLACEHOLDER COPY (826b)
    ["Show recent words on Home", "When off, Home shows outcomes only.",
     "home_recent_words", "home.show_recent_words"],
    # 826b: group related preview controls to retain the five-row Advanced budget.
    [
        "Idle preview",
        "Delay before fading, then how visible the idle preview stays.",
        "cmd_preview_idle_delay,cmd_preview_idle_opacity",
        "command_mode.preview_idle_delay_s,command_mode.preview_idle_opacity"
    ]
]

def build(owner):
    from PySide6.QtWidgets import QLabel
    from samsara import __version__
    from samsara.ui.listening_display_page import bind_settings_description
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    version = QLabel(f"v{__version__}")
    version.setObjectName("runningVersion")
    version.setProperty("role", "description")
    page.column.insertWidget(1, version)
    bind_settings_description(owner)
    return page
