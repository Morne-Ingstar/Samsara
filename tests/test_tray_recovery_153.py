"""Recovery-first tray guarantees for queue 153."""

from unittest.mock import Mock

import pytest

from samsara import updater
from tests.test_tray_qt import _make_app, _stub_tray


def _top_level_texts(menu):
    return {action.text() for action in menu.actions() if not action.isSeparator()}


def test_source_update_check_stops_before_network_and_labels_the_entry_honestly():
    opener = Mock()

    with pytest.raises(updater.UpdateNotSupported):
        updater.check_for_update(opener=opener)

    opener.assert_not_called()
    assert updater.update_check_menu_label() == "Updates unavailable…"


def test_tray_label_comes_from_updater_availability(qapp, monkeypatch):
    app = _make_app()
    app.config["updates"] = {"tray_menu_entry": True}
    label = "Updates unavailable…"
    monkeypatch.setattr("samsara.ui.tray_qt.update_check_menu_label", lambda: label)
    tray, _started = _stub_tray(monkeypatch, app)

    tools = next(action.menu() for action in tray._menu.actions() if action.text() == "Tools")
    assert label in [action.text() for action in tools.actions()]


def test_recovery_affordances_stay_top_level(qapp, monkeypatch):
    app = _make_app()
    app._mouse_hook = None
    tray, _started = _stub_tray(monkeypatch, app)

    top = _top_level_texts(tray._menu)
    assert {
        "Show Samsara", "Snooze", "Wake Word  (samsara)",
        "[MIC]  Test Microphone", "Mode:  Hold", "Quick Reference",
        "Settings", "Something wrong?", "Exit",
    } <= top
