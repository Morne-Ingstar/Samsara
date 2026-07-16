"""Focused Qt coverage for the Settings Help & Feedback controls."""

import threading
from types import SimpleNamespace
from pathlib import Path

from PySide6.QtWidgets import QLabel, QPushButton
from PySide6.QtCore import QUrl

from samsara.support_feedback import (
    BETA_SUPPORT_EMAIL,
    BETA_SUPPORT_MAILTO,
    BUG_REPORT_URL,
    DOCUMENTATION_URL,
)
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


def test_help_support_tab_is_discoverable_and_opens_requested_routes(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_qt.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        assert "Help & Support" in settings_qt._TAB_NAMES
        assert settings_qt._SIDEBAR_GROUPS[-1] == ("Support", ["Help & Support"])

        docs = _button(window, "openDocumentationButton")
        report = _button(window, "reportBugButton")
        beta = _button(window, "betaFeedbackButton")
        _button(window, "openLiveLogButton")
        _button(window, "copyDiagnosticButton")
        status = window.findChild(QLabel, "feedbackStatusLabel")
        assert status is not None

        docs.click()
        beta.click()
        report.click()

        assert opened == [
            DOCUMENTATION_URL,
            QUrl(BETA_SUPPORT_MAILTO).toString(),
            BUG_REPORT_URL,
        ]
        assert status.text() == "Opened in your browser."

        support_page = window._stack.widget(
            settings_qt._TAB_NAMES.index("Help & Support")
        )
        support_text = " ".join(
            label.text() for label in support_page.findChildren(QLabel)
        )
        assert BETA_SUPPORT_EMAIL in support_text
    finally:
        window.deleteLater()


def test_settings_search_indexes_support_actions(qapp):
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        support_index = settings_qt._TAB_NAMES.index("Help & Support")
        support_rows = [row for row in window._search_rows if row[2] == support_index]
        searchable = " ".join(f"{label} {description}" for label, description, *_ in support_rows)
        assert "documentation" in searchable.lower()
        assert "email" in searchable.lower()
        assert "diagnostic" in searchable.lower()
    finally:
        window.deleteLater()


def test_settings_stylesheet_keeps_the_visible_combo_chevron():
    """Settings has a local QSS layer, so it must repeat the shared arrow.

    A blank QComboBox::drop-down area is not an acceptable fallback: users
    cannot tell a selector from a text field, particularly at high DPI.
    """
    assert "QComboBox::down-arrow" in settings_qt.STYLESHEET
    assert settings_qt.theme.ARROW_PATH in settings_qt.STYLESHEET
    assert Path(settings_qt.theme.ARROW_PATH).is_file()


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
