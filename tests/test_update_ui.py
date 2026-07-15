"""Focused privacy, threading, and Qt integration tests for app updates."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from PySide6.QtWidgets import QCheckBox, QLabel, QMessageBox, QPushButton

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui import settings_qt, update_qt
from samsara.ui.tray_qt import SamsaraTrayQt


def _release(version="0.22.1", size=536_250_717):
    return SimpleNamespace(version=version, asset_size=size)


class _SettingsApp:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda _phrase: None,
        )
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *_args, **_kwargs):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def _tray_app(config=None):
    app = Mock()
    app.config = {
        "microphone": "mic-1",
        "mode": "hold",
        "wake_word_config": {"phrase": "samsara"},
        "wake_word_enabled": False,
        "streaming_mode": False,
        "gesture": {"enabled": False},
        "listening_indicator_enabled": False,
        "cleanup_mode": "clean",
        "hotkey": "ctrl+shift",
        "model_size": "base",
        **(config or {}),
    }
    app.available_mics = [{"id": "mic-1", "name": "Test Microphone"}]
    app._is_audio_capture_active = Mock(return_value=True)
    app.get_current_microphone_name = Mock(return_value="Test Microphone")
    app.snoozed = False
    app.cheat_sheet = None
    app.create_icon_image = Mock(
        return_value=Image.new("RGBA", (16, 16)),
    )
    app._splash_progress = 0
    app._startup_failed = False
    return app


def _texts(menu):
    return [action.text() for action in menu.actions()]


def test_settings_updates_are_opt_in_and_explain_network_privacy(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        update_qt,
        "show_update_dialog",
        lambda app, **kwargs: opened.append((app, kwargs)),
    )
    app = _SettingsApp({"updates": {"channel": "stable"}})
    window = settings_qt._SettingsWindow(app)
    try:
        checkbox = window.findChild(QCheckBox, "automaticUpdateChecksCheckbox")
        button = window.findChild(QPushButton, "checkForUpdatesButton")
        assert checkbox is not None
        assert button is not None
        assert checkbox.isChecked() is False
        assert SETTINGS_SCHEMA["updates.automatic_checks"]["default"] is False

        general_page = window._stack.widget(settings_qt._TAB_NAMES.index("General"))
        copy = " ".join(label.text() for label in general_page.findChildren(QLabel))
        lowered = copy.lower()
        for phrase in (
            "no update server or push channel",
            "off by default",
            "github releases",
            "no more than once every 24 hours",
            "sends no audio",
            "dictated text",
            "device identifier",
            "ip address",
            "request headers",
        ):
            assert phrase in lowered

        button.click()
        assert opened == [(app, {"check_immediately": True})]

        checkbox.setChecked(True)
        general_updates = window._save_fns[0]({})
        assert general_updates["updates"] == {
            "channel": "stable",
            "automatic_checks": True,
        }
    finally:
        window.deleteLater()


def test_update_dialog_source_mode_never_starts_network_worker(qapp, monkeypatch):
    network = Mock()
    spawn = Mock()
    monkeypatch.setattr(update_qt, "check_for_update", network)
    monkeypatch.setattr(update_qt.thread_registry, "spawn", spawn)
    monkeypatch.setattr(
        update_qt,
        "update_unavailable_reason",
        lambda: "Automatic updates are disabled when Samsara runs from source.",
    )

    dialog = update_qt._UpdateDialog(SimpleNamespace(quit_app=Mock()))
    try:
        dialog.start_check()
        assert dialog._phase == "unsupported"
        assert "runs from source" in dialog._status.text()
        assert dialog._primary.isEnabled() is False
        network.assert_not_called()
        spawn.assert_not_called()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_manual_check_worker_reports_available_release_to_dialog(qapp, monkeypatch):
    release = _release()
    workers = []
    network = Mock(return_value=release)
    monkeypatch.setattr(update_qt, "check_for_update", network)
    monkeypatch.setattr(update_qt, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(
        update_qt.thread_registry,
        "spawn",
        lambda name, target, daemon=True: workers.append((name, target, daemon)),
    )

    dialog = update_qt._UpdateDialog(SimpleNamespace(quit_app=Mock()))
    try:
        dialog.start_check()
        assert dialog._phase == "checking"
        network.assert_not_called()
        assert workers[0][0] == "update-manual-check"
        assert workers[0][2] is True

        workers[0][1]()
        qapp.processEvents()

        network.assert_called_once_with(current_version=update_qt.__version__)
        assert dialog._phase == "available"
        assert dialog._release is release
        assert dialog._primary.text() == "Install v0.22.1…"
        assert "you have v" in dialog._status.text().lower()
        assert "download size" in dialog._status.text().lower()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_automatic_check_off_and_recent_check_do_no_work(monkeypatch):
    spawn = Mock()
    network = Mock()
    monkeypatch.setattr(update_qt, "is_frozen_build", lambda: True)
    monkeypatch.setattr(update_qt, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(update_qt, "check_for_update", network)
    monkeypatch.setattr(update_qt.thread_registry, "spawn", spawn)
    monkeypatch.setattr(update_qt.time, "time", lambda: 100_000.0)

    off = SimpleNamespace(config={}, update_config_and_save=Mock())
    recent = SimpleNamespace(
        config={
            "updates": {
                "automatic_checks": True,
                "last_check_epoch": 100_000.0 - update_qt.AUTO_CHECK_INTERVAL_S + 1,
            }
        },
        update_config_and_save=Mock(),
    )

    assert update_qt.maybe_start_automatic_update_check(off, Mock()) is False
    assert update_qt.maybe_start_automatic_update_check(recent, Mock()) is False
    spawn.assert_not_called()
    network.assert_not_called()
    off.update_config_and_save.assert_not_called()
    recent.update_config_and_save.assert_not_called()


def test_opted_in_automatic_check_is_daily_and_marshals_result_to_qt(monkeypatch):
    release = _release()
    workers = []
    posted = []
    available = Mock()
    app = SimpleNamespace(
        config={"updates": {"automatic_checks": True, "last_check_epoch": 1.0}},
        update_config_and_save=Mock(),
    )
    monkeypatch.setattr(update_qt, "is_frozen_build", lambda: True)
    monkeypatch.setattr(update_qt, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(update_qt.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(update_qt, "check_for_update", lambda **_kwargs: release)
    monkeypatch.setattr(update_qt.qt_runtime, "post", posted.append)
    monkeypatch.setattr(
        update_qt.thread_registry,
        "spawn",
        lambda name, target, daemon=True: workers.append((name, target, daemon)),
    )

    assert update_qt.maybe_start_automatic_update_check(app, available) is True
    app.update_config_and_save.assert_called_once_with({
        "updates": {"automatic_checks": True, "last_check_epoch": 100_000.0}
    })
    assert workers[0][0] == "update-automatic-check"
    assert workers[0][2] is True

    workers[0][1]()
    available.assert_not_called()
    assert len(posted) == 1
    posted[0]()
    available.assert_called_once_with(release)


def test_tray_update_action_changes_to_install_and_notifies(qapp, monkeypatch):
    monkeypatch.setattr(
        update_qt, "maybe_start_automatic_update_check", lambda *_args: False,
    )
    tray = SamsaraTrayQt(_tray_app())
    message = Mock()
    monkeypatch.setattr(tray._tray, "showMessage", message)
    try:
        tray._rebuild_menu()
        assert "Check for Updates…" in _texts(tray._menu)

        release = _release()
        tray._show_update_available(release)
        assert tray._available_update is release
        message.assert_called_once()
        assert message.call_args.args[0] == "Samsara update available"
        assert "Version 0.22.1 is ready" in message.call_args.args[1]

        tray._rebuild_menu()
        assert "Install Samsara v0.22.1…" in _texts(tray._menu)
        assert "Check for Updates…" not in _texts(tray._menu)
    finally:
        tray.stop()


def test_tray_does_not_confirm_update_or_check_network_before_healthy_startup(
    qapp, monkeypatch,
):
    from samsara import updater

    reconcile = Mock()
    automatic = Mock()
    monkeypatch.setattr(updater, "reconcile_update_on_startup", reconcile)
    monkeypatch.setattr(
        update_qt, "maybe_start_automatic_update_check", automatic,
    )
    app = _tray_app({"updates": {"automatic_checks": True}})
    tray = SamsaraTrayQt(app)
    try:
        tray._poll_startup_health()
        reconcile.assert_not_called()
        automatic.assert_not_called()

        app._splash_progress = 99
        tray._poll_startup_health()
        reconcile.assert_not_called()
        automatic.assert_not_called()
    finally:
        tray.stop()


def test_tray_confirms_update_only_after_full_startup(qapp, monkeypatch):
    from samsara import updater

    status = updater.UpdateStatus(
        "installed", "Updated to Samsara v0.22.1.", "v0.22.1",
    )
    reconcile = Mock(return_value=status)
    automatic = Mock(return_value=False)
    monkeypatch.setattr(updater, "reconcile_update_on_startup", reconcile)
    monkeypatch.setattr(
        update_qt, "maybe_start_automatic_update_check", automatic,
    )
    app = _tray_app()
    tray = SamsaraTrayQt(app)
    message = Mock()
    monkeypatch.setattr(tray._tray, "showMessage", message)
    try:
        app._splash_progress = 100
        tray._poll_startup_health()

        reconcile.assert_called_once_with()
        automatic.assert_called_once_with(app, tray._show_update_available)
        assert message.call_args.args[:2] == (
            "Samsara updated", "Updated to Samsara v0.22.1.",
        )

        tray._poll_startup_health()
        reconcile.assert_called_once_with()
    finally:
        tray.stop()


def test_failed_startup_never_confirms_replacement(qapp, monkeypatch):
    from samsara import updater

    reconcile = Mock()
    automatic = Mock()
    monkeypatch.setattr(updater, "reconcile_update_on_startup", reconcile)
    monkeypatch.setattr(
        update_qt, "maybe_start_automatic_update_check", automatic,
    )
    app = _tray_app()
    app._splash_progress = 100
    app._startup_failed = True
    tray = SamsaraTrayQt(app)
    try:
        tray._poll_startup_health()
        reconcile.assert_not_called()
        automatic.assert_not_called()
    finally:
        tray.stop()


def test_voice_command_opens_explicit_update_check(monkeypatch):
    from plugins.commands import core_utils

    posted = []
    opened = []
    app = SimpleNamespace()
    monkeypatch.setattr(core_utils.thread_registry, "spawn", Mock())
    monkeypatch.setattr(update_qt.qt_runtime, "post", posted.append)
    monkeypatch.setattr(
        update_qt,
        "show_update_dialog",
        lambda target, **kwargs: opened.append((target, kwargs)),
    )

    assert core_utils.check_for_updates(app) is True
    assert len(posted) == 1
    posted[0]()
    assert opened == [(app, {"check_immediately": True})]


def test_confirmed_install_prepares_off_qt_then_launches_and_queues_quit(
    qapp, monkeypatch,
):
    release = _release()
    prepared = SimpleNamespace(version=release.version)
    workers = []
    scheduled = []
    app = SimpleNamespace(quit_app=Mock())
    prepare = Mock()

    def _prepare(actual_release, *, progress_callback):
        prepare(actual_release, progress_callback=progress_callback)
        progress_callback(50, 100)
        return prepared

    launch = Mock()
    monkeypatch.setattr(
        update_qt.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(update_qt, "prepare_update", _prepare)
    monkeypatch.setattr(update_qt, "launch_prepared_update", launch)
    monkeypatch.setattr(
        update_qt.thread_registry,
        "spawn",
        lambda name, target, daemon=True: workers.append((name, target, daemon)),
    )
    monkeypatch.setattr(
        update_qt.QTimer,
        "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    dialog = update_qt._UpdateDialog(app, initial_release=release)
    try:
        dialog._primary_clicked()
        assert dialog._phase == "preparing"
        assert workers[0][0] == "update-download-prepare"
        assert workers[0][2] is True
        prepare.assert_not_called()
        launch.assert_not_called()

        workers[0][1]()
        qapp.processEvents()

        prepare.assert_called_once()
        assert prepare.call_args.args == (release,)
        assert callable(prepare.call_args.kwargs["progress_callback"])
        assert dialog._progress.value() == 50
        launch.assert_called_once_with(prepared, current_pid=update_qt.os.getpid())
        app.quit_app.assert_not_called()
        assert len(scheduled) == 1
        assert scheduled[0][0] == 0
        scheduled[0][1]()
        app.quit_app.assert_called_once_with()
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize("failure", ["prepare", "launch"])
def test_install_failure_stays_visible_and_never_quits(
    qapp, monkeypatch, failure,
):
    release = _release()
    prepared = SimpleNamespace(version=release.version)
    workers = []
    app = SimpleNamespace(quit_app=Mock())
    launch = Mock()

    if failure == "prepare":
        def _prepare(*_args, **_kwargs):
            raise update_qt.UpdateError("verification failed")
    else:
        _prepare = Mock(return_value=prepared)
        launch.side_effect = OSError("helper blocked")

    monkeypatch.setattr(
        update_qt.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(update_qt, "prepare_update", _prepare)
    monkeypatch.setattr(update_qt, "launch_prepared_update", launch)
    monkeypatch.setattr(
        update_qt.thread_registry,
        "spawn",
        lambda name, target, daemon=True: workers.append((name, target, daemon)),
    )

    dialog = update_qt._UpdateDialog(app, initial_release=release)
    try:
        dialog._primary_clicked()
        workers[0][1]()
        qapp.processEvents()

        assert dialog._phase == "available"
        assert dialog._primary.isEnabled() is True
        assert dialog._primary.text() == "Try v0.22.1 Again"
        assert "unchanged" in dialog._status.text().lower()
        assert "live log" in dialog._status.text().lower()
        app.quit_app.assert_not_called()
        if failure == "prepare":
            launch.assert_not_called()
        else:
            launch.assert_called_once_with(
                prepared, current_pid=update_qt.os.getpid(),
            )
    finally:
        dialog.close()
        dialog.deleteLater()
