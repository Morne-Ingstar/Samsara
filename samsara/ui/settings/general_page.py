"""General Settings destination (285; placeholder copy)."""
from samsara.help_terms import MICROPHONE_HELP
from samsara.listening_display import LABEL, QUESTION

TITLE = "General"
SUBTITLE = "Make Samsara comfortable for everyday use."
BASE = [
    [
        "Microphone",
        MICROPHONE_HELP,
        "mic_combo,microphoneRefreshButton",
        "microphone,microphone_name"
    ],
    [
        "Language",
        "Choose the language you dictate in.",
        "lang_combo",
        "language"
    ],
    [LABEL, QUESTION, "listening_display", "ui.listening_display"],
    [
        "Indicator position",
        "Choose the indicator's starting position.",
        "adv_indicator_pos",
        "listening_indicator_position"
    ],
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
        "Contextual hints",
        "Show guidance as you learn.",
        "hints_enabled,action:Reset hints",
        "hints_enabled"
    ],
    # PLACEHOLDER COPY (1020)
    ["Windows notifications", "Reminders you set are always shown.",
     "notifications_level", "notifications_level"],
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
        "Floating windows",
        "Recover the indicator or dictation preview.",
        "floatingWindowActions",
        ""
    ],
    # PLACEHOLDER COPY (826b)
    ["Show recent words on Home", "When off, Home shows outcomes only.",
     "home_recent_words", "home.show_recent_words"],
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
