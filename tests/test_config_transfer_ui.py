import json
import threading
from types import SimpleNamespace

from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from samsara.config_transfer import export_config, load_config_export
from samsara.ui import settings_qt


class _TransferApp:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda phrase: None
        )
        self.hints = None
        self.alarm_manager = None
        self.saved_while_locked = False
        self.saved_config = None

    def play_sound(self, *args, **kwargs):
        pass

    def save_config(self):
        self.saved_while_locked = self._config_lock.locked()
        self.saved_config = json.loads(json.dumps(self.config))

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def _window(qapp, config=None):
    return settings_qt._SettingsWindow(_TransferApp(config))


def test_general_tab_exposes_backup_controls_and_private_value_warning(qapp):
    window = _window(qapp)
    try:
        assert window.findChild(QPushButton, "exportConfigurationButton") is not None
        assert window.findChild(QPushButton, "importConfigurationButton") is not None
        warning = window.findChild(QLabel, "configBackupPrivacyWarning")
        assert warning is not None
        warning_text = warning.text().lower()
        assert "private" in warning_text
        assert "api keys" in warning_text
    finally:
        window.deleteLater()


def test_export_ui_warns_then_writes_complete_snapshot(qapp, tmp_path, monkeypatch):
    window = _window(qapp, {
        "language": "en",
        "cloud_llm": {"api_key": "private-secret"},
    })
    destination = tmp_path / "backup-without-extension"
    questions = []
    notices = []

    monkeypatch.setattr(
        settings_qt.QMessageBox,
        "question",
        lambda *args: questions.append(args) or QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        settings_qt.QFileDialog,
        "getSaveFileName",
        lambda *args: (str(destination), "JSON files (*.json)"),
    )
    monkeypatch.setattr(
        settings_qt.QMessageBox,
        "information",
        lambda *args: notices.append(args),
    )

    try:
        window._export_configuration()
        written = destination.with_suffix(".json")
        assert load_config_export(written) == window.app.config
        assert len(questions) == 1
        assert "private" in questions[0][2].lower()
        assert len(notices) == 1
    finally:
        window.deleteLater()


def test_import_ui_merges_saves_under_lock_and_offers_restart(
    qapp, tmp_path, monkeypatch
):
    source = export_config(
        tmp_path / "backup.json",
        {"language": "fr", "cloud_llm": {"provider": "openrouter"}},
    )
    window = _window(qapp, {
        "language": "en",
        "future_setting": True,
        "cloud_llm": {"provider": "deepseek", "timeout_seconds": 30},
    })
    questions = []
    restarted = []

    monkeypatch.setattr(
        settings_qt.QFileDialog,
        "getOpenFileName",
        lambda *args: (str(source), "JSON files (*.json)"),
    )
    monkeypatch.setattr(
        settings_qt.QMessageBox,
        "question",
        lambda *args: questions.append(args) or QMessageBox.StandardButton.Yes,
    )
    from plugins.commands import core_utils
    monkeypatch.setattr(core_utils, "restart_app", lambda app: restarted.append(app))

    try:
        window._import_configuration()
        assert window.app.saved_while_locked is True
        assert window.app.saved_config == {
            "language": "fr",
            "future_setting": True,
            "cloud_llm": {"provider": "openrouter", "timeout_seconds": 30},
        }
        assert len(questions) == 2
        assert "config.json.bak" in questions[0][2]
        assert restarted == [window.app]
    finally:
        window.deleteLater()
