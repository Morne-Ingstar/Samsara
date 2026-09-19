"""Focused Qt coverage for the Settings Help & Feedback controls."""

import threading
from types import SimpleNamespace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton
from PySide6.QtCore import QUrl

from samsara.support_feedback import (
    BETA_SUPPORT_EMAIL,
    BETA_SUPPORT_SUBJECT,
    BUG_REPORT_URL,
    DOCUMENTATION_URL,
)
from samsara.ui import settings_qt

_PRIVATE_VALUES = (
    "cloud-secret",
    "supporter-secret",
    "Private Microphone Name",
    "private dictated words",
)


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
        report.click()

        assert opened == [DOCUMENTATION_URL, BUG_REPORT_URL]
        assert status.text() == "Opened in your browser."
        assert beta.text() == "Email the developer"

        support_page = window._stack.widget(
            settings_qt._TAB_NAMES.index("Help & Support")
        )
        support_text = " ".join(
            label.text() for label in support_page.findChildren(QLabel)
        )
        assert BETA_SUPPORT_EMAIL in support_text
    finally:
        window.deleteLater()


def _email_window(qapp, monkeypatch, open_result):
    events = []
    clipboard = _Clipboard()
    real_set = clipboard.setText

    def set_text(text):
        events.append("clipboard")
        real_set(text)

    clipboard.setText = set_text
    monkeypatch.setattr(settings_qt.QApplication, "clipboard", staticmethod(lambda: clipboard))

    def open_url(url):
        events.append(("open", url.toString(QUrl.ComponentFormattingOption.FullyEncoded)))
        if isinstance(open_result, Exception):
            raise open_result
        return open_result

    monkeypatch.setattr(settings_qt.QDesktopServices, "openUrl", open_url)
    return settings_qt._SettingsWindow(_FeedbackApp()), clipboard, events


@pytest.mark.parametrize("open_result", [True, False, RuntimeError("no handler")])
def test_email_copies_the_full_message_before_trying_mailto_and_never_claims_it_opened(
    qapp, monkeypatch, open_result,
):
    window, clipboard, events = _email_window(qapp, monkeypatch, open_result)
    try:
        status = window.findChild(QLabel, "feedbackStatusLabel")
        _button(window, "betaFeedbackButton").click()

        assert events[0] == "clipboard"
        assert events[1][0] == "open"
        assert events[1][1].startswith(f"mailto:{BETA_SUPPORT_EMAIL}?subject=")
        assert "&body=" in events[1][1]

        assert f"To: {BETA_SUPPORT_EMAIL}" in clipboard.text
        assert f"Subject: {BETA_SUPPORT_SUBJECT}" in clipboard.text
        assert "What I said / What it did / What I expected:" in clipboard.text
        assert "Model: medium" in clipboard.text
        for private_value in _PRIVATE_VALUES:
            assert private_value not in clipboard.text

        assert status.text() == (
            "Message copied to your clipboard. If your mail app didn't open, "
            f"paste it into any email to {BETA_SUPPORT_EMAIL}."
        )
        assert "opened" not in status.text().lower()
    finally:
        window.deleteLater()


def test_support_rows_are_email_diagnostics_github_then_live_log(qapp):
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        page = window._stack.widget(settings_qt._TAB_NAMES.index("Help & Support"))
        wanted = {"betaFeedbackButton", "copyDiagnosticButton", "reportBugButton", "openLiveLogButton"}
        seen = []

        def walk(layout_or_widget):
            layout = layout_or_widget if hasattr(layout_or_widget, "itemAt") else layout_or_widget.layout()
            if layout is None:
                name = layout_or_widget.objectName()
                if name in wanted:
                    seen.append(name)
                return
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item.layout() is not None:
                    walk(item.layout())
                elif item.widget() is not None:
                    walk(item.widget())

        walk(page.widget().layout())
        assert seen == ["betaFeedbackButton", "copyDiagnosticButton", "reportBugButton", "openLiveLogButton"]

        intro = page.findChild(QLabel, "supportContactIntroLabel")
        assert intro.text() == "Testers: email is fine. GitHub is for people who already have an account."
        address = page.findChild(QLabel, "betaSupportAddressLabel")
        assert address.text() == BETA_SUPPORT_EMAIL
        assert address.textInteractionFlags() & settings_qt.Qt.TextInteractionFlag.TextSelectableByMouse
    finally:
        window.deleteLater()


def test_show_tab_selects_help_and_support(qapp):
    window = settings_qt._SettingsWindow(_FeedbackApp())
    try:
        window.show_tab("Help & Support")
        support_index = settings_qt._TAB_NAMES.index("Help & Support")
        assert window._stack.currentIndex() == support_index
        row = window._sidebar.currentRow()
        assert window._sidebar_row_to_stack_index[row] == support_index
    finally:
        window.hide()
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
    assert "QComboBox::down-arrow" in settings_qt.stylesheet()
    assert settings_qt.theme.ARROW_PATH in settings_qt.stylesheet()
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
        for private_value in _PRIVATE_VALUES:
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

        assert "never unlocks features" not in lowered  # Prompt 244 removed the supporter UI copy.
        assert "supporting is optional" not in lowered  # Prompt 244 removed the supporter UI copy.
        assert "early builds" not in lowered
        assert "managed cloud key" not in lowered
    finally:
        window.deleteLater()
