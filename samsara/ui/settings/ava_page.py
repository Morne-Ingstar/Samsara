"""Ava Settings destination with one consent-gated route control."""

from .shortcuts_page import AVA_HELD_KEY_COPY

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
        "Local model",
        "Choose the installed model used locally.",
        "ava_cmd_model",
        "ava_command_session.model"
    ],
    [
        *AVA_HELD_KEY_COPY,
        "ava_key_picker",
        ""  # The Shortcuts control remains the config inventory entry.
    ],
    [
        "Before Ava can answer",
        "",
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
    ],
]
ADVANCED = [
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
        "Keep a conversation log",
        "Saves each request to Ava, her reply and the context she used in a file on this PC, for troubleshooting. Off by default.",
        "ava_conversation_log_enabled",
        "ava.conversation_log_enabled"
    ],
    [
        "Ava consent",
        "Read how Ava handles local and cloud requests.",
        "ava:Read this again",
        ""
    ],
]

def _conversation_log_control(owner):
    from PySide6.QtWidgets import QCheckBox
    from samsara.config_defaults import DEFAULTS
    log_switch = QCheckBox()
    log_switch.setChecked((owner.app.config.get("ava", {}) or {}).get(
        "conversation_log_enabled", DEFAULTS["ava.conversation_log_enabled"]) is True)
    log_switch.setAccessibleName("Keep a conversation log")
    return log_switch


def _add_setup_guide(owner, page):
    import weakref
    from samsara.ui import theme

    # PLACEHOLDER COPY (1015): the same guide remains available after setup.
    button = page.add_guide(
        "Ava guide", lambda: getattr(owner.app, "open_ava_guide", lambda: None)(),
        action_below=True,
    )
    button.setText("Open Ava guide")
    theme.make_primary(button)
    line = page.subtitle
    page.ava_header = page.guide_header
    owner._widgets["ava_setup_guide_line"] = line
    owner._widgets["ava_setup_guide_button"] = button

    def refresh_line():
        from samsara import ai_preferences, ava_readiness

        # Configuration and temporary provider availability are different: a
        # configured but offline/warming provider keeps the normal controls.
        pending = not ai_preferences.runtime_state(owner.app.config, "ava").allowed
        readiness = ava_readiness.readiness_for(owner.app)
        # PLACEHOLDER COPY (1010): setup status uses the existing subtitle slot.
        line.setText(
            ("Ava is ready. You can change or test the setup in the guide."
             if readiness.ready else
             "Ava is not set up yet. Use the guide to get it ready.")
            if pending else SUBTITLE
        )

    refresh_line()
    from samsara import ava_readiness
    from samsara.ui import qt_runtime
    import shiboken6

    tracker = ava_readiness.tracker
    page_ref = weakref.ref(page)

    def readiness_changed(_old, _new):
        def refresh_if_alive():
            target = page_ref()
            if target is None or not shiboken6.isValid(target):
                tracker.remove_listener(readiness_changed)
                return
            refresh_line()

        qt_runtime.post(refresh_if_alive)

    page._ava_setup_guide_tracker = tracker
    page._ava_setup_guide_listener = readiness_changed
    tracker.add_listener(readiness_changed)
    page.destroyed.connect(lambda *_: tracker.remove_listener(readiness_changed))
    return refresh_line


def _prepare_held_key_picker(owner):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from samsara.ui import theme

    picker = owner._widgets["ava_mode_key"].linked_hold_picker()
    notes = QWidget()
    layout = QVBoxLayout(notes)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE_1)
    layout.addWidget(picker.reason_label())
    layout.addWidget(owner._widgets["ava_mode_key_off_note"])
    owner._widgets["ava_key_picker"] = picker
    owner._widgets["ava_key_picker_warn"] = notes


def build(owner):
    from functools import wraps
    log_switch = _conversation_log_control(owner)
    owner._widgets["ava_conversation_log_enabled"] = log_switch
    save_ava = owner._source_saves["ava"][0]
    @wraps(save_ava)
    def save_log(acc):
        updates = save_ava(acc)
        ava = dict(updates.get("ava", owner.app.config.get("ava", {})) or {})
        ava["conversation_log_enabled"] = log_switch.isChecked()
        updates["ava"] = ava
        return updates
    owner._source_saves["ava"][0] = save_log
    _prepare_held_key_picker(owner)
    page = owner.make_page(TITLE, SUBTITLE, BASE, ADVANCED)
    owner._widgets["ava_mode_key_off_note"].setVisible(not owner._widgets["ava_enabled"].isChecked())
    refresh_guide_line = _add_setup_guide(owner, page)
    owner._widgets["ava_route_status"].setMinimumWidth(260)
    # Keep shared condition/search refreshes intact; refine Ava's model row.
    refresh = owner._refresh_conditions
    def refresh_conditions():
        refresh()
        row = owner._rows_by_widget["ava_cmd_model"]
        visible = owner._widgets["providerRoute"].currentData() == "local"
        row.setProperty("conditionVisible", visible)
        row.setVisible(visible)
        owner._refresh_ava_route_status()
        refresh_guide_line()
    owner._widgets['ava_cmd_model'].textChanged.connect(lambda _text: owner._refresh_ava_route_status())
    owner._refresh_conditions = refresh_conditions
    refresh_conditions()
    return page
