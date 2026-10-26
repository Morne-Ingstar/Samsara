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

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from samsara.ui import tutorial_qt, theme
from samsara.ui.focus_ring import install


@pytest.fixture
def recovery_host(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr(tutorial_qt, "_tutorial_catalog", lambda app: None)
    opener = Mock()
    monkeypatch.setattr(tutorial_qt, "open_support_tab", opener)
    install(qapp)
    with qt_widget_scope(TutorialWindow(SimpleNamespace(config={"hotkey": "ctrl+shift"}))) as window:
        window._step = [key for key, _ in window._steps].index("command")
        window._show_step()
        window._cmd_hint.show()  # Same label exposed by the command hint timer.
        window.show()
        qapp.processEvents()
        window._cancel_hint_timer()
        yield window, opener


def _tab_to(qapp, window, label):
    window._back_btn.setFocus()
    qapp.processEvents()
    for _ in range(20):
        QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Tab)
        qapp.processEvents()
        if qapp.focusWidget() is label:
            return
    pytest.fail("Tab never reached the recovery Help label")


@pytest.mark.parametrize("label_index", [0, 1], ids=["instruction", "delayed-hint"])
@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter], ids=["return", "keypad-enter"])
def test_recovery_help_tab_and_enter(qapp, recovery_host, label_index, key):
    window, opener = recovery_host
    labels = [label for label in window._current_page.findChildren(QLabel)
              if tutorial_qt.HELP_LINK in label.text()]
    assert len(labels) == 2
    label = labels[label_index]
    _tab_to(qapp, window, label)
    assert label.hasFocus()
    assert label.property("keyboardFocus") is True
    assert theme.ACCENT in label.styleSheet()
    assert label.accessibleName() == "Help and Support for command examples"
    QTest.keyClick(label, key)
    qapp.processEvents()
    opener.assert_called_once_with(window._app)


@pytest.mark.parametrize("label_index", [0, 1], ids=["instruction", "delayed-hint"])
def test_recovery_help_mouse_activation(qapp, recovery_host, label_index):
    window, opener = recovery_host
    labels = [label for label in window._current_page.findChildren(QLabel)
              if tutorial_qt.HELP_LINK in label.text()]
    label = labels[label_index]
    hovered = []
    label.linkHovered.connect(hovered.append)
    # Locate the actual laid-out anchor with real pointer events, then click once.
    for y in range(2, label.height(), 3):
        for x in range(2, label.width(), 3):
            QTest.mouseMove(label, QPoint(x, y))
            if hovered and hovered[-1] == tutorial_qt.HELP_LINK:
                QTest.mouseClick(label, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
                opener.assert_called_once_with(window._app)
                return
    pytest.fail("Pointer never reached the actual Help anchor")


def test_available_examples_stay_plain_text(qapp, monkeypatch, recovery_host):
    window, opener = recovery_host
    monkeypatch.setattr(tutorial_qt, "_command_examples", lambda *args: ["scroll down"])
    page = window._build_command()
    try:
        labels = page.findChildren(tutorial_qt._HelpLabel)
        assert len(labels) == 2
        for label in labels:
            assert tutorial_qt.HELP_LINK not in label.text()
            assert label.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert not label.accessibleName()
            assert "border" not in label.styleSheet()
            QTest.keyClick(label, Qt.Key.Key_Return)
        opener.assert_not_called()
    finally:
        page.deleteLater()


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
