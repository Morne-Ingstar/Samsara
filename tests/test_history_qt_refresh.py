from types import SimpleNamespace
from unittest.mock import Mock

from samsara.ui import history_qt


def _existing_history_shell():
    shell = history_qt.HistoryQt(SimpleNamespace())
    shell._window = SimpleNamespace(
        _view=SimpleNamespace(refresh=Mock()),
        show=Mock(),
        raise_=Mock(),
        activateWindow=Mock(),
        isMinimized=Mock(return_value=False),
        showNormal=Mock(),
    )
    return shell


def test_existing_history_window_refreshes_when_reopened(monkeypatch, qapp):
    shell = _existing_history_shell()
    posted = []
    monkeypatch.setattr(history_qt.qt_runtime, "post", posted.append)
    confirm = Mock()
    monkeypatch.setattr(history_qt.window_activation, 'confirm_foreground', confirm)

    shell.show()

    assert posted == [shell._show_and_refresh]
    posted[0]()
    shell._window._view.refresh.assert_called_once()
    shell._window.show.assert_called_once()
    shell._window.raise_.assert_called_once()
    shell._window.activateWindow.assert_called_once()
    shell._window.showNormal.assert_not_called()
    confirm.assert_called_once_with(shell._window)


def test_live_history_refresh_is_marshaled_to_qt_thread(monkeypatch):
    shell = _existing_history_shell()
    posted = []
    monkeypatch.setattr(history_qt.qt_runtime, "post", posted.append)

    shell.refresh()

    assert posted == [shell._window._view.refresh]
    shell._window._view.refresh.assert_not_called()
