from copy import deepcopy
from pathlib import Path
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QEventLoop

from samsara import config_defaults
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui import settings_qt
from tests.test_settings_pages import _StubApp, _schema_defaults


def _window(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "samsara-home"))
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **kw: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    config = deepcopy(_schema_defaults())
    config['wake_profiles'] = _sample_profiles()  # 940: explicit editor fixtures.
    return settings_qt._SettingsWindow(_StubApp(config))


def _sample_profiles():
    return [
        {
            "id": "claude", "phrase": "okay claude", "oww_model": "okay_claude.onnx",
            "target_process": "claude.exe", "enabled": True, "mode": "focus_dictate", "send_word": "over",
        },
        {
            "id": "hermes", "phrase": "activate hermes", "oww_model": "activate_hermes.onnx",
            "target_process": "Hermes.exe", "enabled": True, "mode": "stage_send", "send_word": "send",
        },
        {
            "id": "warp", "phrase": "activate warp", "oww_model": "activate_warp.onnx",
            "target_process": "warp.exe", "enabled": False, "mode": "focus_dictate", "send_word": "over",
        },
    ]
def test_schema_default_is_the_shipped_profile_list():
    # 940: 919 removed personal profiles from the shipped defaults.
    shipped = []
    assert SETTINGS_SCHEMA["wake_profiles"]["type"] == "list"
    assert SETTINGS_SCHEMA["wake_profiles"]["item_type"] == "dict"
    assert SETTINGS_SCHEMA["wake_profiles"]["default"] == shipped
    assert config_defaults.DEFAULTS["wake_profiles"] == shipped


def test_profile_editor_search_and_save_round_trip(qapp, monkeypatch, tmp_path, qt_widget_scope):
    window = _window(monkeypatch, tmp_path)
    with qt_widget_scope(window):
        window.show_tab("Dictation")
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        profiles = deepcopy(window.app.config["wake_profiles"])
        controls = window._widgets["wake_profile_controls"]
        assert len(controls) == len(profiles) == 3
        row = next(row for row in window._destination_rows if row.title == "Wake profiles")
        assert row.page_name == "Dictation"
        assert row.disclosure is window._pages["Dictation"].advanced
        assert "second" not in row.help.accessibleDescription().lower()
        assert 'matching trained model' in row.help.accessibleDescription().lower()

        window._search_edit.setText("claude")
        window._apply_search_filter()
        assert window._pages["Dictation"].advanced.toggle.isChecked()
        assert row.isVisible()
        proof = tmp_path
        proof.mkdir(parents=True, exist_ok=True)
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        QWidget.grab(window).save(str(proof / "312-wake-profiles.png"))

        controls[0]["phrase"].setText("start claude")
        controls[0]["target_process"].setText("claude.exe")
        controls[0]["send_word"].setText("over")
        controls[1]["enabled"].setChecked(False)
        updates = window._collect_destination_updates({})
        saved = updates["wake_profiles"]
        assert saved[0]["phrase"] == "start claude"
        assert saved[0]["target_process"] == "claude.exe"
        assert saved[0]["send_word"] == "over"
        assert saved[0]["oww_model"] == profiles[0]["oww_model"]
        assert saved[0]["mode"] == profiles[0]["mode"]
        assert saved[1]["enabled"] is False
        assert window.app.config["wake_profiles"] == profiles
