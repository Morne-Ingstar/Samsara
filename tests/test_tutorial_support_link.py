"""The tutorial's final step offers a way to report a problem.

Beta testers without a GitHub account need one obvious route to the
Help & Support page (email + safe diagnostics); the tutorial's last page is
where a first-time user is most likely to have hit something.
"""
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import ExitStack

from PySide6.QtWidgets import QPushButton

from samsara.ui import qt_runtime, settings_qt
from samsara.ui.tutorial_qt import TutorialWindow
from tests.test_support_feedback_ui import _FeedbackApp


def test_done_step_button_opens_settings_on_help_and_support(qapp, monkeypatch, qt_widget_scope):
    posted = []
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    # Register each owner immediately; destroy in reverse order while patches
    # still apply, including Settings.closeEvent's visit/timer cancellation.
    with ExitStack() as owners:
        window = owners.enter_context(qt_widget_scope(settings_qt._SettingsWindow(_FeedbackApp())))
        app = SimpleNamespace(
            config={"hotkey": "ctrl+shift"},
            open_settings=Mock(),
            _settings_qt=SimpleNamespace(_window=window),
        )
        tutorial = owners.enter_context(qt_widget_scope(TutorialWindow(app)))
        done_page = tutorial._pages[[key for key, _ in tutorial._steps].index("done")]
        button = done_page.findChild(QPushButton, "tutorialSupportButton")
        assert button is not None
        assert button.text() == "Something wrong? Tell me"
        assert button.property("class") == "secondary"

        button.click()

        app.open_settings.assert_called_once_with()
        assert len(posted) == 1
        posted.pop()()  # Deliver only this route, never the global Qt queue.
        assert window._stack.currentIndex() == settings_qt._TAB_NAMES.index("Help")
        assert window._stack.currentWidget() is window._pages["Help"]
