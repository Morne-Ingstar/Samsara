"""996: Home reuses the updater without unsolicited network activity."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from samsara.ui import update_qt
from samsara.ui.home_update_line_qt import HomeUpdateLine, RELEASE_NOTE


@pytest.fixture
def row(qapp, monkeypatch):
    monkeypatch.setattr(update_qt, "_dialog", None)
    monkeypatch.setattr(update_qt, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(update_qt, "previous_request_pending", lambda: False)
    monkeypatch.setattr(update_qt, "previous_entry", lambda: (None, "No previous version."))
    from samsara import update_installer
    monkeypatch.setattr(update_installer, "dialog_notice", lambda: "")
    workers = []
    monkeypatch.setattr(update_qt.thread_registry, "spawn",
                        lambda name, target, **kw: workers.append(target))
    network = Mock(return_value=None)
    monkeypatch.setattr(update_qt, "check_for_update", network)
    app = SimpleNamespace(config={"updates": {"automatic_checks": False}},
                          tray_icon=SimpleNamespace(_available_update=None))
    widget = HomeUpdateLine(app)
    widget.resize(650, 60)
    widget.show()
    qapp.processEvents()
    yield widget, app, workers, network
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def finish(row, qapp, *, release=None, error=None):
    widget, app, workers, network = row
    network.return_value = release
    network.side_effect = error
    workers.pop()()
    qapp.processEvents()
    widget.refresh()


def test_idle_never_checks_or_enables_automatic_updates(row):
    widget, app, workers, network = row
    assert widget.status.text() == RELEASE_NOTE
    assert widget.button.text() == "Check for updates"
    assert widget.button.isEnabled()
    assert widget.button.width() < widget.width() / 2
    assert widget.button.height() >= 44
    assert not workers
    network.assert_not_called()
    assert app.config["updates"]["automatic_checks"] is False


@pytest.mark.parametrize("beta", [False, True])
def test_checking_disabled_then_up_to_date_uses_saved_channel(row, qapp, beta):
    widget, app, workers, network = row
    app.config["updates"]["include_prerelease"] = beta
    widget.button.click()
    assert widget.status.text() == "Checking for updates…"
    assert not widget.button.isEnabled()
    widget.button.click()
    assert len(workers) == 1
    finish(row, qapp)
    options = {"allow_prerelease": True} if beta else {}
    network.assert_called_once_with(current_version=update_qt.__version__, **options)
    assert "Up to date" in widget.status.text()
    assert update_qt.__version__ in widget.status.text()
    assert widget.button.isEnabled()
    assert not widget._controller.isVisible()


def test_available_opens_existing_install_flow(row, qapp, monkeypatch):
    widget, app, workers, network = row
    release = SimpleNamespace(version="9.9.9", asset_size=123)
    widget.button.click()
    finish(row, qapp, release=release)
    assert widget.status.text() == "Version 9.9.9 is available."
    assert widget.button.text() == "Install v9.9.9…"
    show = Mock()
    monkeypatch.setattr(update_qt, "show_update_dialog", show)
    widget.button.click()
    show.assert_called_once_with(app, initial_release=release)
    assert network.call_count == 1


def test_offline_can_retry(row, qapp):
    widget, app, workers, network = row
    widget.button.click()
    finish(row, qapp, error=OSError("offline"))
    assert "check for updates" in widget.status.text()
    assert "connection" in widget.status.text()
    assert widget.button.isEnabled()
    widget.button.click()
    finish(row, qapp)
    assert "Up to date" in widget.status.text()


@pytest.mark.parametrize("automatic", [False, True])
def test_observes_existing_automatic_result_only_when_enabled(row, automatic):
    widget, app, workers, network = row
    app.config["updates"]["automatic_checks"] = automatic
    app.tray_icon._available_update = SimpleNamespace(version="9.9.9", asset_size=123)
    widget.refresh()
    assert ("9.9.9" in widget.status.text()) is automatic
    network.assert_not_called()
    assert not workers


def test_channel_change_invalidates_manual_and_cached_automatic_release(row, qapp):
    widget, app, workers, network = row
    app.config["updates"]["automatic_checks"] = True
    release = SimpleNamespace(version="9.9.9-beta.1", asset_size=123)
    app.tray_icon._available_update = release
    widget.refresh()
    assert "available" in widget.status.text()
    app._update_channel_generation = 1
    widget.refresh()
    assert widget.status.text() == RELEASE_NOTE
    widget.button.click()
    app._update_channel_generation = 2
    finish(row, qapp, release=release)
    assert "available" not in widget.status.text()
    assert widget._release is None


def test_source_build_explains_unavailable_without_network(row, monkeypatch):
    widget, app, workers, network = row
    monkeypatch.setattr(update_qt, "update_unavailable_reason", lambda: "Packaged builds only.")
    widget.button.click()
    assert widget.status.text() == "Packaged builds only."
    network.assert_not_called()
    assert not workers


def test_existing_settings_check_is_observed_and_reused(row, qapp, monkeypatch):
    widget, app, workers, network = row
    dialog = update_qt._UpdateDialog(app)
    monkeypatch.setattr(update_qt, "_dialog", dialog)
    dialog.start_check()
    widget.refresh()
    assert not widget.button.isEnabled()
    assert len(workers) == 1
    finish(row, qapp)
    assert "Up to date" in widget.status.text()
    assert widget._controller is None
    dialog.close()


def test_later_automatic_result_replaces_earlier_manual_up_to_date(row, qapp):
    widget, app, workers, network = row
    app.config["updates"]["automatic_checks"] = True
    widget.button.click()
    finish(row, qapp)
    assert "Up to date" in widget.status.text()
    app.tray_icon._available_update = SimpleNamespace(version="9.9.9", asset_size=123)
    widget.refresh()
    assert widget.status.text() == "Version 9.9.9 is available."
    assert network.call_count == 1
