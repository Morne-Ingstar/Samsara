"""Help Settings destination (285; placeholder copy)."""

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
        "Benchmark recording",
        "Save dictation audio and transcripts locally for review.",
        "adv_bench_collect",
        "benchmark.collect_samples"
    ]
]

def build(owner):
    return owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
