"""Ava Settings destination with one consent-gated route control."""

TITLE = "Ava"
SUBTITLE = "Choose how Ava helps, with permissions you control."
BASE = [
    [
        "Ava runs",
        "Choose Off, on this PC, or your cloud provider.",
        "providerRoute",
        "ava.provider_policy,ava_mode_enabled,ava_command_session.enabled,command_packs.ai,cloud_llm.enabled,ava_command_session.backend,ava.consent"
    ],
    [
        "Effective route",
        "Where Ava will handle your requests after Apply.",
        "ava_route_status",
        ""
    ],
    [
        "Memory retention",
        "Choose whether Ava starts fresh, keeps the last conversation, or remembers facts you tell her.",  # OWNER COPY
        "ava_memory_mode",
        "ava_memory.mode"
    ],
    [
        "Editing",
        "Allow Ava to edit what you just dictated.",
        "ava_edit_enabled",
        "ava_edit.enabled"
    ],
    [
        "Ava interprets commands",
        "After a normal command miss, Ava may choose one available command.",
        "ava_interpret_misses",
        "ava_command_session.interpret_misses"
    ]
]
ADVANCED = [
    [
        "Ava consent",
        "Read how Ava handles local and cloud requests.",
        "ava:Read this again",
        ""
    ],
    [
        "Cloud provider",
        "Choose the provider receiving cloud requests.",
        "cloud_provider",
        "cloud_llm.provider"
    ],
    [
        "Cloud credential",
        "Enter your provider credential and test the connection.",
        "cloud_api_key,ava:Show,ava:Test Connection",
        "cloud_llm.api_key"
    ],
    [
        "Cloud model override",
        "Use a particular provider model.",
        "cloud_model",
        "cloud_llm.model"
    ],
    [
        "Web search",
        "Allow supported providers to search the web.",
        "cloud_web_search",
        "cloud_llm.web_search"
    ],
    [
        "Cloud timeout",
        "Limit how long a cloud request can take.",
        "cloud_timeout",
        "cloud_llm.timeout_seconds"
    ],
    [
        "Local model",
        "Choose the installed model used locally.",
        "ava_cmd_model",
        "ava_command_session.model"
    ],
    [
        "Keep model warm",
        "Keep the local model ready between requests.",
        "ava_cmd_keep_warm",
        "ava_command_session.keep_warm"
    ],
    [
        "Warm at startup",
        "Send a small request at startup to prepare Ava.",
        "ava_warm_on_boot",
        "ava.warm_on_boot"
    ],
    [
        "Ava personality",
        "Choose the style of Ava's answers.",
        "ava_personality",
        "ava_personality"
    ],
    [
        "Remembered turns",
        "Limit the conversation retained as context.",
        "ava_memory_max_turns",
        "ava_memory.max_turns"
    ],
    [
        "Editing timeout",
        "Limit how long an editing request can take.",
        "ava_edit_timeout",
        "ava_edit.timeout_seconds"
    ],
    [
        "Local editing model",
        "Choose the local model for editing.",
        "ava_edit_model",
        "ava_edit.model"
    ],
    [
        "Demo pacing",
        "Use slower editing animations for demonstrations.",
        "ava_edit_demo_pacing",
        "ava_edit.demo_pacing"
    ],
    [
        "Automatic corrections",
        "Enable the separate automatic cleanup pass.",
        "sc_enabled",
        "smart_corrections.enabled"
    ],
    [
        "Correction model",
        "Use this local model for automatic corrections.",
        "sc_model",
        "smart_corrections.ollama_model"
    ],
    [
        "Correct hold dictation",
        "Apply automatic corrections to held dictation.",
        "sc_mode_hotkey",
        "smart_corrections.modes.hotkey"
    ],
    [
        "Correct wake dictation",
        "Apply automatic corrections to wake dictation.",
        "sc_mode_wake",
        "smart_corrections.modes.wake"
    ],
    [
        "Correct streaming",
        "Apply automatic corrections to streaming dictation.",
        "sc_mode_streaming",
        "smart_corrections.modes.streaming"
    ],
    [
        "Repair disfluencies",
        "Remove filler words and spoken self-corrections.",
        "sc_repair_disfluencies",
        "smart_corrections.repair_disfluencies"
    ]
]

def build(owner):
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    owner._widgets["ava_route_status"].setMinimumWidth(260)
    return page
