"""Shortcuts Settings destination (285; placeholder copy)."""

TITLE = "Shortcuts"
SUBTITLE = "Keep your essential actions within reach."
BASE = [
    [
        "Primary dictation",
        "Choose the dictation key and its behaviour.",
        "hotkey,mode",
        "hotkey,mode"
    ],
    [
        "Command-only key",
        "Capture one command without dictation output.",
        "command_hotkey",
        "command_hotkey"
    ],
    [
        "Session button",
        "Choose the hands-free button and its behaviour.",
        "cmd_tab_button,cmd_mode",
        "command_mode.button,command_mode.mode"
    ],
    [
        "Cancel",
        "Cancel the active recording.",
        "cancel_hotkey",
        "cancel_hotkey"
    ],
    [
        "Undo",
        "Undo the most recent supported action.",
        "undo_hotkey",
        "undo_hotkey"
    ],
    [
        "Paste staged thought",
        "Insert the thought waiting in your session.",
        "dictate_commit_hotkey",
        "dictate_commit_hotkey"
    ],
    [
        "Task held key",
        "Hold to capture a task.",
        "task_hotkey",
        "task_hotkey"
    ],
    [
        "Quick memo",
        "Capture a short voice memo.",
        "memo_hotkey",
        "memo_hotkey"
    ],
    [
        "Corrections",
        "Capture a correction; report from that window.",
        "capture_correction_hotkey,openCorrections",
        "hotkeys.capture_correction"
    ],
    [
        "Complete alarm",
        "Mark the active alarm as completed.",
        "alarms_complete_key",
        "alarms.complete_hotkey"
    ],
    [
        "Dismiss alarm",
        "Dismiss an alarm without completing it.",
        "alarms_dismiss_key",
        "alarms.dismiss_hotkey"
    ]
]
ADVANCED = [
    [
        "Wake toggle",
        "Start or stop wake listening.",
        "wake_word_hotkey",
        "wake_word_hotkey"
    ],
    [
        "Continuous toggle",
        "Start or stop the continuous preset.",
        "continuous_hotkey",
        "continuous_hotkey"
    ],
    [
        "Continuous commit",
        "Commit the current continuous capture.",
        "continuous_commit_hotkey",
        "continuous_commit_hotkey"
    ],
    [
        "Correction report shortcut",
        "Keep the existing direct report shortcut.",
        "correction_hotkey",
        "correction_hotkey"
    ],
    [
        "Mouse suppression",
        "Suppress browser-back when Mouse 4/5 is bound.",
        "cmd_tab_suppress",
        "command_mode.suppress_button"
    ],
    [
        "Scroll the app you're using, not the one under the mouse",
        "Move the pointer into the active app before voice scrolling when needed.",
        "scroll_target_foreground",
        "scroll.target_foreground"
    ]
]

def build(owner):
    return owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
