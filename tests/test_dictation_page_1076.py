from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox

from samsara import ai_preferences, smart_corrections
from samsara.config_defaults import cfg_get
from samsara.ui.settings.calm_layout import GroupHeading
from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
from tests.test_settings_pages import _StubApp, _schema_defaults


CORRECTION_OPTIONS = (
    "sc_model", "sc_mode_hotkey", "sc_mode_wake", "sc_mode_streaming", "sc_repair_disfluencies",
)
FORMATTING = ("auto_capitalize", "format_numbers", "trailing_space")
MOVED = {
    "auto_capitalize": "auto_capitalize",
    "format_numbers": "format_numbers",
    "trailing_space": "add_trailing_space",
    "adv_perf_mode": "performance_mode",
    "sc_enabled": "smart_corrections.enabled",
    "sc_model": "smart_corrections.ollama_model",
    "sc_mode_hotkey": "smart_corrections.modes.hotkey",
    "sc_mode_wake": "smart_corrections.modes.wake",
    "sc_mode_streaming": "smart_corrections.modes.streaming",
    "sc_repair_disfluencies": "smart_corrections.repair_disfluencies",
    "scroll_target_foreground": "scroll.target_foreground",
    "cmd_preview_idle_delay": "command_mode.preview_idle_delay_s",
    "cmd_preview_idle_opacity": "command_mode.preview_idle_opacity",
    "cmd_dictation_literal_only": "command_mode.dictation_literal_commands_only",
}
SWITCHES = (
    *FORMATTING, "sc_enabled", *CORRECTION_OPTIONS[1:],
    "scroll_target_foreground", "cmd_dictation_literal_only",
)


@pytest.fixture
def settings(monkeypatch, qt_widget_scope, tmp_path):
    monkeypatch.setattr("samsara.autostart.sync_run_entry", lambda *a, **k: None)
    monkeypatch.setattr("samsara.runtime.thread_registry.spawn", lambda *a, **k: None)
    monkeypatch.setattr("samsara.ui.first_run_qt.ComponentsPage.refresh_async", lambda *a: None)
    monkeypatch.setattr("samsara.ava_readiness.recheck_after_settings", lambda *a: None)
    monkeypatch.setattr(smart_corrections, "warm_up", lambda app: None)
    saved = tmp_path / "config.json"

    with ExitStack() as scope:
        def open_window(config):
            app = _StubApp(config)
            app.save_config = lambda: saved.write_text(json.dumps(app.config), encoding="utf-8")
            window = scope.enter_context(qt_widget_scope(_SettingsWindow(app)))
            monkeypatch.setattr(window, "_replace_tts_runtime", lambda *a: None)
            select(window, "Dictation")
            window.show()
            return window

        yield open_window, lambda: json.loads(saved.read_text(encoding="utf-8"))


def select(window, title):
    window._sidebar.setCurrentRow(_TAB_NAMES.index(title))
    return window._stack.currentWidget()


def close_and_discard(window):
    # 1356: Close asks before discarding staged changes.
    window._close_button.click()
    dialog, = (box for box in window.findChildren(QMessageBox) if box.isVisible())
    next(button for button in dialog.buttons() if button.text() == "Discard changes").click()
    assert not window.isVisible()


def defaults(**overrides):
    return {**deepcopy(_schema_defaults()), **overrides}


def grant(config, policy, *, features=("ava", "editing")):
    config["ava"]["consent"] = ai_preferences.accept_consent(
        config, provider=ai_preferences.provider_for(config, policy),
        features=features, cloud=policy == "cloud",
    )


def test_groups_and_search_follow_the_moved_rows(settings):
    open_window, _ = settings
    window = open_window(defaults())
    page = window._stack.currentWidget()
    assert not page.advanced.toggle.isChecked()
    assert [row.widget_keys[0] for row in page.base_rows[:14]] == [
        "cleanup_mode", "auto_paste", *FORMATTING, "sc_enabled", "cmd_matching_enabled",
        "model_combo", "adv_perf_mode", "cmd_tab_enabled", "hands_free_send_mode",
        "wake_word_enabled", "wake_word_phrase", "speech_pace",
    ]
    assert page.isAncestorOf(window._widgets["wake_setup_guide_button"])
    assert page.isAncestorOf(window._widgets["speech_pace_try"])
    assert window._rows_by_widget["sc_enabled"].help is not None
    page.advanced.toggle.setChecked(True)
    rows = [page.advanced.rows.itemAt(index).widget() for index in range(page.advanced.rows.count())]
    assert [row.widget_keys[0] for row in rows if hasattr(row, "widget_keys")][-2:] == [
        "adv_device", "wake_profiles",
    ]
    assert [group.text() for group in rows if isinstance(group, GroupHeading)] == [
        "Text behavior", "Timing and sensitivity", "Continuous dictation",
        "Microphone calibration", "Session controls", "Hardware acceleration",
    ]
    assert window._rows_by_widget["cmd_timing_policy"].disclosure is page.advanced
    main_groups = [group.text() for group in page.findChildren(GroupHeading) if group not in rows]
    assert Counter(main_groups) == {"Text behavior": 1, "Speech model": 1, "Hands-free sessions": 1}
    window._widgets["sc_enabled"].click()

    inventory = window.config_row_inventory()
    assert Counter((key, title) for key, title, _ in inventory if key in MOVED.values()) == {
        (key, "Dictation"): 1 for key in MOVED.values()
    }
    for widget_key, config_key in MOVED.items():
        select(window, "General")
        window._search_edit.setText(config_key)
        window._apply_search_filter()
        assert window._stack.currentWidget() is page, config_key
        assert window._rows_by_widget[widget_key].isVisibleTo(page), config_key


def test_corrections_prerequisite_updates_with_pending_edit_and_route(settings):
    open_window, _ = settings
    config = defaults()
    config["ava"]["provider_policy"] = "local"
    config["ollama"]["enabled"] = True
    config["ava_edit"]["enabled"] = False
    grant(config, "local")
    window = open_window(config)
    page = window._stack.currentWidget()
    assert not dict.__contains__(window._pages, "Ava")
    page.advanced.toggle.setChecked(True)
    widgets = window._widgets
    status = widgets["sc_prerequisite"]
    assert status.text() == ai_preferences.runtime_state(config, "editing").reason
    assert widgets["sc_enabled"].isEnabled()

    def visible(key):
        return window._rows_by_widget[key].isVisibleTo(page)

    assert not any(visible(key) for key in CORRECTION_OPTIONS)
    widgets["sc_enabled"].click()
    assert all(visible(key) for key in CORRECTION_OPTIONS)
    select(window, "Ava")
    widgets["ava_edit_enabled"].click()
    assert status.text() == "Runs through Ollama."
    assert not config["ava_edit"]["enabled"]
    widgets["providerRoute"].setCurrentIndex(widgets["providerRoute"].findData("off"))
    assert status.text() == ai_preferences.runtime_state(config, "editing").reason
    assert "Ava is off" in status.text()
    select(window, "Dictation")
    assert all(visible(key) for key in CORRECTION_OPTIONS)
    widgets["sc_enabled"].click()
    assert not any(visible(key) for key in CORRECTION_OPTIONS)


def test_cloud_provider_changes_update_prerequisite_and_hide_local_model(settings):
    open_window, _ = settings
    config = defaults()
    config["ava"]["provider_policy"] = "cloud"
    config["ava_edit"]["enabled"] = True
    config["smart_corrections"]["enabled"] = True
    config["cloud_llm"].update(enabled=True, provider="openai")
    config["cloud_llm"]["provider_settings"] = {"deepseek": {"api_key": "sample"}}
    grant(config, "cloud")
    config["cloud_llm"]["provider"] = "deepseek"
    grant(config, "cloud")
    config["cloud_llm"]["provider"] = "openai"
    window = open_window(config)
    page = window._stack.currentWidget()
    page.advanced.toggle.setChecked(True)
    assert not dict.__contains__(window._pages, "Ava")
    assert window._rows_by_widget["sc_model"].isHidden()
    select(window, "Ava")
    widgets = window._widgets
    status = widgets["sc_prerequisite"]
    assert status.text() == ai_preferences.runtime_state(config, "editing").reason
    assert "needs its own API key" in status.text()
    widgets["cloud_api_key"].setText("sample")
    assert status.text() == "Runs through OpenAI."
    widgets["cloud_provider"].setCurrentText("DeepSeek (default)")
    assert widgets["cloud_provider"].currentText() == "DeepSeek (default)"
    assert status.text() == "Runs through DeepSeek."
    select(window, "Dictation")
    assert window._rows_by_widget["sc_model"].isHidden()
    assert all(window._rows_by_widget[key].isVisibleTo(page) for key in CORRECTION_OPTIONS[1:])


def test_runtime_still_requires_editing_consent_and_provider(monkeypatch):
    config = defaults()
    config["smart_corrections"]["enabled"] = True
    config["ava"]["provider_policy"] = "local"
    config["ollama"]["enabled"] = True
    config["ava_edit"]["enabled"] = False
    grant(config, "local")
    fake = SimpleNamespace(config=config)
    assert not smart_corrections.is_enabled(fake)
    config["ava_edit"]["enabled"] = True
    del config["ava"]["consent"]
    assert not smart_corrections.is_enabled(fake)
    grant(config, "local", features=("ava",))
    assert not smart_corrections.is_enabled(fake)
    grant(config, "local")
    config["ollama"]["enabled"] = False
    assert not smart_corrections.is_enabled(fake)
    config["ollama"]["enabled"] = True
    assert smart_corrections.is_enabled(fake)
    local_probes = []
    monkeypatch.setattr(smart_corrections, "_ollama_reachable",
                        lambda app: local_probes.append(app) or True)
    assert smart_corrections.resolve_backend(fake) == "ollama"
    config["ava"]["provider_policy"] = "cloud"
    config["cloud_llm"].update(enabled=True, provider="openrouter")
    grant(config, "cloud")
    assert not smart_corrections.is_enabled(fake)
    config["cloud_llm"]["api_key"] = "sample"
    assert smart_corrections.is_enabled(fake)
    assert smart_corrections.resolve_backend(fake) == "cloud"
    assert local_probes == [fake]


def test_moved_values_apply_and_cancel_round_trip_with_precision(settings):
    open_window, read_saved = settings
    config = defaults(compute_type="int8")
    config["command_mode"]["dictate_commit_word"] = "over"
    window = open_window(config)
    page = window._stack.currentWidget()
    page.advanced.toggle.setChecked(True)
    widgets = window._widgets
    assert "until you say 'over'" in window._rows_by_widget["hands_free_send_mode"].detail.text()
    expected = {}
    for key in SWITCHES:
        assert window._rows_by_widget[key].isVisibleTo(page)
        widget = widgets[key]
        widget.click()
        expected[MOVED[key]] = widget.isChecked()
    performance = widgets["adv_perf_mode"]
    performance.parentWidget().buttons[performance.findText("fast")].click()
    widgets["sc_model"].setText("fixture-model")
    widgets["cmd_preview_idle_delay"].setValue(2.5)
    widgets["cmd_preview_idle_opacity"].setValue(45)
    expected.update({
        "performance_mode": "fast", "smart_corrections.ollama_model": "fixture-model",
        "command_mode.preview_idle_delay_s": 2.5, "command_mode.preview_idle_opacity": 0.45,
    })
    assert set(expected) == set(MOVED.values())
    assert all(cfg_get(config, key) != value for key, value in expected.items())
    window._apply_button.click()
    persisted = read_saved()
    assert {key: cfg_get(persisted, key) for key in expected} == expected
    assert persisted["compute_type"] == "int8"
    assert "computePrecisionStatus" not in widgets

    window.show()
    window._pages["Dictation"].advanced.toggle.setChecked(True)
    window._widgets["sc_model"].setText("discarded-model")
    window._widgets["scroll_target_foreground"].click()
    close_and_discard(window)
    window.show()
    widgets = window._widgets
    assert widgets["sc_model"].text() == "fixture-model"
    assert widgets["scroll_target_foreground"].isChecked() == expected["scroll.target_foreground"]
    assert read_saved() == persisted


def test_wake_profiles_add_and_apply_from_empty(settings):
    open_window, read_saved = settings
    window = open_window(defaults(wake_profiles=[]))
    page = window._stack.currentWidget()
    page.advanced.toggle.setChecked(True)
    row = window._rows_by_widget["wake_profiles"]
    empty = next(label for label in row.findChildren(QLabel) if label.text() == "No wake profiles yet.")
    assert empty.isVisibleTo(page) and row.help is not None
    button = window._widgets["wake_profile_add"]
    assert button.isVisibleTo(page)
    button.click()
    assert empty.isHidden()
    window._apply_button.click()
    assert read_saved()["wake_profiles"] == [{
        "id": "profile-1", "phrase": "", "target_process": "", "send_word": "", "enabled": True,
    }]


def test_wake_profiles_remove_middle_add_and_cancel(settings):
    open_window, read_saved = settings
    records = [
        {"id": f"profile-{index}", "phrase": f"Open the fixture {index}",
         "target_process": "fixture.exe", "send_word": "over", "enabled": True,
         "future_field": {"number": index}}
        for index in range(1, 4)
    ]
    window = open_window(defaults(wake_profiles=deepcopy(records)))
    page = window._stack.currentWidget()
    page.advanced.toggle.setChecked(True)
    controls = window._widgets["wake_profile_controls"]
    controls[1]["remove"].click()
    assert [control["id"] for control in controls] == ["profile-1", "profile-3"]
    window._apply_button.click()
    assert read_saved()["wake_profiles"] == [records[0], records[2]]

    window.show()
    window._pages["Dictation"].advanced.toggle.setChecked(True)
    window._widgets["wake_profile_add"].click()
    controls = window._widgets["wake_profile_controls"]
    assert controls[-1]["id"] == "profile-2"
    controls[-1]["phrase"].setText("  Open the new fixture  ")
    controls[-1]["target_process"].setText("new.exe")
    controls[-1]["send_word"].setText("done")
    window._apply_button.click()
    saved = read_saved()["wake_profiles"]
    assert saved == [records[0], records[2], {
        "id": "profile-2", "phrase": "Open the new fixture", "target_process": "new.exe",
        "send_word": "done", "enabled": True,
    }]

    window.show()
    window._pages["Dictation"].advanced.toggle.setChecked(True)
    window._widgets["wake_profile_controls"][0]["remove"].click()
    window._widgets["wake_profile_add"].click()
    close_and_discard(window)
    window.show()
    controls = window._widgets["wake_profile_controls"]
    assert [control["id"] for control in controls] == [record["id"] for record in saved]
    assert [control["phrase"].text() for control in controls] == [record["phrase"] for record in saved]
    assert read_saved()["wake_profiles"] == saved
