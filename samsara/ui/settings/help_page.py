"""Help Settings destination, including opt-in local diagnostics."""

from PySide6.QtWidgets import QLabel

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
        "diagnostics.stage_timing",
        "Turn this on to measure delays when tuning dictation performance. "
        "It saves timings locally without dictated text; leave it off unless you are debugging."
    ],
    [
        "Diagnostic event log",
        "Save dictation results and troubleshooting details to a local file.",
        "diagnostics_write_jsonl",
        "diagnostics.write_jsonl",
        "Turn this on when investigating missing or unexpected dictation results. "
        "The local file can include dictated text, so leave it off unless you are debugging."
    ],
    [
        "Benchmark recording",
        "Save dictation audio and transcripts locally for review.",
        "adv_bench_collect",
        "benchmark.collect_samples"
    ]
]

def build(owner):
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    heading = QLabel('Diagnostics')
    heading.setProperty('role', 'label')
    page.advanced.rows.insertWidget(0, heading)
    return page
