"""Help Settings destination, including opt-in local diagnostics."""

from PySide6.QtWidgets import QLabel
from samsara.ui.community_links import community_actions

TITLE = "Help"
SUBTITLE = "Set up, practise and recover when you need to."
BASE = [
    [
        "Documentation",
        "Find commands and usage guidance.",
        "openDocumentationButton",
        ""
    ],
    [
        "Help and community",
        "Ask questions, report bugs, share ideas, or email me.",
        "communityActions",
        ""
    ],
    [
        "Setup / practice",
        "Find tutorials and microphone or Ava guides.",
        "setupGuides",
        ""
    ],
    [
        "Support / problem",
        "Review diagnostics and get help with a problem.",
        "supportChoices",
        ""
    ],
    [
        "Private backup / restore",
        "Manage private configuration backups.",
        "backupChoices",
        ""
    ],
    [
        "Updates",
        "Check GitHub for updates; daily checks are optional.",
        "checkForUpdatesButton,automatic_update_checks",
        "updates.automatic_checks"
    ]
]
ADVANCED = [
    [
        "Stage timing",
        "Record how long each dictation step takes in a local file.",
        "diagnostics_stage_timing",
        "diagnostics.stage_timing"
    ],
    [
        "Diagnostic event log",
        "Save dictation results and troubleshooting details to a local file.",
        "diagnostics_write_jsonl",
        "diagnostics.write_jsonl"
    ],
    [
        "Benchmark recording",
        "Save dictation audio and transcripts locally for review.",
        "adv_bench_collect",
        "benchmark.collect_samples"
    ]
]

def build(owner):
    owner._widgets["communityActions"] = community_actions()
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    # PLACEHOLDER COPY (1127): one name per way of speaking.
    words = QLabel(
        '<b>Words Samsara uses</b><br>'
        '<b>Wake word</b> listens for your wake phrase so you can start by voice.<br>'
        '<b>Hands-free</b> is an open session for dictation and commands without holding a key.<br>'
        '<b>Hold to talk</b> records while you hold Mouse 5 or your recording shortcut.'
    )
    words.setObjectName('featureWords')
    words.setWordWrap(True)
    page.column.insertWidget(3, words)
    heading = QLabel('Diagnostics')
    heading.setProperty('role', 'label')
    page.advanced.rows.insertWidget(0, heading)
    from samsara.hold_recording_recovery import build_incident_controls
    page.advanced.rows.insertWidget(1, build_incident_controls(owner.app))
    return page
