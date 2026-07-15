"""Focused Qt coverage for the Settings Help & Feedback controls."""

import threading
from types import SimpleNamespace

from PySide6.QtWidgets import QLabel, QPushButton

from samsara.support_feedback import BETA_FEEDBACK_URL, BUG_REPORT_URL
from samsara.ui import settings_qt


class _FeedbackApp:
    def __init__(self):
        self.config = {
            "model_size": "medium",
            "device": "cuda",
            "command_mode": {"enabled": True},
            "cloud_llm": {"api_key": "cloud-secret"},
            "supporter_key": "supporter-secret",
            "microphone": "Private Microphone Name",
            "last_dictation": "private dictated words",
        }
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda _phrase: None,
        )
        self.hints = None
        self.alarm_manager = None
        self.log_open_count = 0

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

    def open_log_viewer(self):
        self.log_open_count += 1


class _Clipboard:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


def _button(window, object_name):
    button = window.findChild(QPushButton, object_name)
    assert button is not None
    return button


def test_help_feedback_buttons_exist_and_open_structured_forms(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_qt.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        report = _button(window, "reportBugButton")
        beta = _button(window, "betaFeedbackButton")
        _button(window, "openLiveLogButton")
        _button(window, "copyDiagnosticButton")
        status = window.findChild(QLabel, "feedbackStatusLabel")
        assert status is not None

        report.click()
        beta.click()

        assert opened == [BUG_REPORT_URL, BETA_FEEDBACK_URL]
        assert status.text() == "Opened in your browser."
    finally:
        window.deleteLater()


def test_help_feedback_opens_live_log_and_copies_allowlisted_diagnostics(
    qapp, monkeypatch,
):
    clipboard = _Clipboard()
    monkeypatch.setattr(
        settings_qt.QApplication,
        "clipboard",
        staticmethod(lambda: clipboard),
    )
    app = _FeedbackApp()
    window = settings_qt._SettingsWindow(app)
    try:
        _button(window, "openLiveLogButton").click()
        assert app.log_open_count == 1

        _button(window, "copyDiagnosticButton").click()
        assert "Model: medium" in clipboard.text
        assert "Requested device: cuda" in clipboard.text
        assert "HANDS FREE enabled: True" in clipboard.text
        for private_value in (
            "cloud-secret",
            "supporter-secret",
            "Private Microphone Name",
            "private dictated words",
        ):
            assert private_value not in clipboard.text

        status = window.findChild(QLabel, "feedbackStatusLabel")
        assert status is not None
        assert "no logs or secrets" in status.text().lower()
    finally:
        window.deleteLater()


def test_supporter_copy_promises_only_cosmetic_extras(qapp):
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        cloud_page = window._stack.widget(settings_qt._TAB_NAMES.index("Ava / Cloud"))
        copy = " ".join(label.text() for label in cloud_page.findChildren(QLabel))
        lowered = copy.lower()

        assert "never unlocks functional features" in lowered
        assert "future supporter extras will be cosmetic" in lowered
        assert "early builds" not in lowered
        assert "managed cloud key" not in lowered
    finally:
        window.deleteLater()
