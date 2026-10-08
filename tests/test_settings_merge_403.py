"""Settings Apply reconciles untouched controls with outside config changes."""
import copy
import logging
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def _merge(opened, live, updates):
    from samsara.settings_merge import merge_outside_changes
    return merge_outside_changes(opened, live, updates)


@pytest.mark.parametrize("opened", [
    {}, {"value": None}, {"value": False}, {"value": [1, {"nested": 2}]},
    {"value": {}}, {"value": {"nested": None}},
    {"value": {"nested": {"a": 1, "b": 2}}},
])
def test_unchanged_live_config_is_identity_for_updates(opened):
    values = [None, False, 0, "new", [], [1, 2], {}, {"nested": 3}]
    for updates in [{}, *({"value": value} for value in values)]:
        merged, kept = _merge(opened, copy.deepcopy(opened), updates)
        assert merged == updates
        assert merged is not updates
        assert kept == []


def test_voice_silent_keeps_both_leaves_and_dialog_changes_in_same_section():
    opened = {"tts": {"feedback_level": "brief", "enabled": True,
                      "engine": "winrt", "speed": 1.0, "volume": 0.5}}
    live = copy.deepcopy(opened)
    live["tts"].update(feedback_level="silent", enabled=False)
    updates = copy.deepcopy(opened)
    updates["tts"].update(engine="edge", speed=1.2, volume=0.3)
    merged, kept = _merge(opened, live, updates)
    assert merged == {"tts": {"feedback_level": "silent", "enabled": False,
                              "engine": "edge", "speed": 1.2, "volume": 0.3}}
    assert kept == ["tts.feedback_level", "tts.enabled"]


def test_explicit_dialog_feedback_level_wins_conflict():
    merged, kept = _merge(
        {"tts": {"feedback_level": "brief"}},
        {"tts": {"feedback_level": "silent"}},
        {"tts": {"feedback_level": "full"}},
    )
    assert merged == {"tts": {"feedback_level": "full"}}
    assert kept == []


@pytest.mark.parametrize("dialog,expected,kept", [
    (0.5, 0.7, ["sound_volume"]), (0.3, 0.3, []),
])
def test_sound_volume(dialog, expected, kept):
    assert _merge({"sound_volume": 0.5}, {"sound_volume": 0.7},
                  {"sound_volume": dialog}) == ({"sound_volume": expected}, kept)


def test_nested_leaves_are_decided_independently_and_partial_root_stays_partial():
    opened = {"section": {"inner": {"untouched": 1, "edited": 2}}, "unrelated": 1}
    live = {"section": {"inner": {"untouched": 3, "edited": 4}}, "unrelated": 2}
    updates = {"section": {"inner": {"untouched": 1, "edited": 5}}}
    assert _merge(opened, live, updates) == (
        {"section": {"inner": {"untouched": 3, "edited": 5}}},
        ["section.inner.untouched"],
    )


@pytest.mark.parametrize("opened,live,updates,expected,kept", [
    ({"s": {}}, {"s": {"added": None}}, {"s": {}},
     {"s": {"added": None}}, ["s.added"]),
    ({}, {"s": {"outside": 1}}, {"s": {"dialog": 2}},
     {"s": {"dialog": 2, "outside": 1}}, ["s.outside"]),
    ({}, {"new": 1}, {"new": 2}, {"new": 2}, []),
    ({"value": None}, {}, {"value": None}, {}, ["value"]),
    ({"value": 1}, {"value": None}, {"value": 1}, {"value": None}, ["value"]),
    ({"s": {"gone": 1}}, {"s": {}}, {"s": {"gone": 1}},
     {"s": {}}, ["s.gone"]),
    ({"s": {"gone": 1}}, {"s": {"gone": 2}}, {"s": {}}, {"s": {}}, []),
    ({"s": {"a": 1}}, {}, {"s": {"a": 1}}, {}, ["s"]),
    ({"s": {"a": 1, "b": 2}}, {}, {"s": {"a": 3, "b": 2}},
     {"s": {"a": 3}}, ["s.b"]),
])
def test_missing_keys_and_none_are_distinct(opened, live, updates, expected, kept):
    assert _merge(opened, live, updates) == (expected, kept)


@pytest.mark.parametrize("old,current,dialog,expected,kept", [
    ([1], [2], [1], [2], ["value"]),
    ([{"a": 1}], [{"a": 2}], [{"a": 3}], [{"a": 3}], []),
    (None, {"a": 2}, None, {"a": 2}, ["value"]),
    ({"a": 1}, None, {"a": 1}, None, ["value"]),
    ({"a": 1}, None, {"a": 2}, {"a": 2}, []),
    (None, {"a": 2}, {}, {}, []),
])
def test_lists_and_container_replacements_are_atomic(old, current, dialog, expected, kept):
    assert _merge({"value": old}, {"value": current}, {"value": dialog}) == (
        {"value": expected}, kept,
    )


def test_inputs_and_nested_output_are_independent():
    opened = {"s": {"list": [{"a": 1}], "edit": [1]}}
    live = {"s": {"list": [{"a": 2}], "edit": [1]}}
    updates = {"s": {"list": [{"a": 1}], "edit": [3]}}
    before = copy.deepcopy((opened, live, updates))
    merged, kept = _merge(opened, live, updates)
    assert kept == ["s.list"]
    assert (opened, live, updates) == before
    merged["s"]["list"][0]["a"] = 9
    merged["s"]["edit"].append(4)
    assert (opened, live, updates) == before


@pytest.fixture
def settings_window(qapp, monkeypatch):
    import sounddevice
    from samsara import autostart
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults

    monkeypatch.setattr(sounddevice, "query_devices", lambda *a, **kw: [])
    monkeypatch.setattr(sounddevice, "query_hostapis", lambda *a, **kw: [])
    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **kw: None)
    app = _StubApp(copy.deepcopy(_schema_defaults()))
    app.config["tts"].update(feedback_level="brief", enabled=True, engine="winrt")
    app.config["sound_volume"] = 0.5
    app.tts_engine = SimpleNamespace(list_voices=lambda: [])
    app.audio_coordinator = SimpleNamespace(_dispatch_lock=RLock())
    app.save_config = Mock()
    app._session_mode_manager = SimpleNamespace(set_spoken_notices=Mock())
    monkeypatch.setattr(_SettingsWindow, "_replace_tts_runtime", Mock())
    window = _SettingsWindow(app)
    try:
        yield window
    finally:
        window.deleteLater()
        qapp.processEvents()


def _outside_silent(window):
    from samsara.config_schema import apply_feedback_level
    apply_feedback_level(window.app.config, "silent")
    window.app.config["sound_volume"] = 0.7
    window.app.tts_engine = None


def test_window_apply_keeps_voice_changes_without_rebuilding_tts(settings_window, caplog):
    window = settings_window
    _outside_silent(window)
    expected_tts = copy.deepcopy(window.app.config["tts"])
    window._widgets["auto_capitalize"].setChecked(False)
    with caplog.at_level(logging.INFO):
        window._apply_and_close()
    assert window.app.config["tts"]["feedback_level"] == "silent"
    assert window.app.config["tts"]["enabled"] is False
    assert window.app.config["tts"] == expected_tts
    assert window.app.config["auto_capitalize"] is False
    assert window.app.config["sound_volume"] == 0.7
    window._replace_tts_runtime.assert_not_called()
    window.app.save_config.assert_called_once()
    window.app._session_mode_manager.set_spoken_notices.assert_called_once_with("questions")
    messages = [record.getMessage() for record in caplog.records
                if "kept changes made outside this window" in record.getMessage()]
    assert len(messages) == 1
    for path in ("tts.feedback_level", "tts.enabled", "sound_volume"):
        assert path in messages[0]
    assert "silent" not in messages[0] and "0.7" not in messages[0]


def test_window_explicit_level_wins_but_unchanged_enabled_leaf_keeps_live(settings_window):
    window = settings_window
    _outside_silent(window)
    combo = window._widgets["tts_feedback_level"]
    combo.setCurrentIndex(combo.findData("full"))
    window._apply_and_close()
    assert window.app.config["tts"]["feedback_level"] == "full"
    # The mandated independent leaf rule retains this unchanged dialog leaf.
    assert window.app.config["tts"]["enabled"] is False
    assert window.app.config["sound_volume"] == 0.7
    window.app._session_mode_manager.set_spoken_notices.assert_called_once_with("everything")


def test_window_without_snapshot_retains_ordinary_apply_behavior(settings_window):
    window = settings_window
    del window._config_at_open
    _outside_silent(window)
    window._apply_and_close()
    assert window.app.config["tts"]["feedback_level"] == "brief"
    assert window.app.config["tts"]["enabled"] is True
    assert window.app.config["sound_volume"] == 0.5
    window._replace_tts_runtime.assert_called_once()


def test_snapshot_is_deep_and_precedes_page_construction(settings_window):
    window = settings_window
    before = copy.deepcopy(window.app.config)
    assert window._config_at_open == before
    window.app.config["tts"]["feedback_level"] = "silent"
    assert window._config_at_open == before


def test_rounded_untouched_value_cannot_be_recognized_as_unchanged():
    assert _merge({"sound_volume": 0.555}, {"sound_volume": 0.7},
                  {"sound_volume": 0.55}) == ({"sound_volume": 0.55}, [])


def test_merge_precedes_microphone_output_and_spoken_notice_side_effects():
    from samsara.ui.settings.services import SettingsServices
    opened = {"microphone": 1, "microphone_name": "Old input",
              "output_device": 1, "output_device_name": "Old output",
              "tts": {"enabled": True, "feedback_level": "brief", "engine": "winrt"}}
    live = copy.deepcopy(opened)
    live.update(microphone=2, microphone_name="New input",
                output_device=2, output_device_name="New output")
    live["tts"].update(enabled=False, feedback_level="silent")
    app = SimpleNamespace(
        config=live, _config_lock=RLock(), tts_engine=None,
        audio_coordinator=SimpleNamespace(_dispatch_lock=RLock()),
        switch_microphone=Mock(), switch_output_device=Mock(), save_config=Mock(),
        _session_mode_manager=SimpleNamespace(set_spoken_notices=Mock()),
    )
    controller = SettingsServices()
    controller.app = app
    controller._config_at_open = copy.deepcopy(opened)
    controller._save_fns = [lambda acc: copy.deepcopy(opened)]
    controller._replace_tts_runtime = Mock()
    controller.close = Mock()
    expected = copy.deepcopy(live)
    controller._apply_and_close()
    assert app.config == expected
    app.switch_microphone.assert_not_called()
    app.switch_output_device.assert_not_called()
    controller._replace_tts_runtime.assert_not_called()
    app._session_mode_manager.set_spoken_notices.assert_called_once_with("questions")
    app.save_config.assert_called_once()
