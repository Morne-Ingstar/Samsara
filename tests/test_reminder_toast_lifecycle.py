from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import Mock

import samsara.ui.reminder_toast as toast_module
import samsara.notifications as notifications_module
from samsara.notifications import NotificationManager
from samsara.ui.reminder_toast import ReminderToast


def test_stop_rejects_future_and_already_posted_show_requests(monkeypatch):
    posted = []
    monkeypatch.setattr(toast_module.qt_runtime, "post", posted.append)
    toast = ReminderToast()

    assert toast.show("Title", "Message") is True
    toast.stop()
    assert len(posted) == 2

    # Exercise the shutdown race: stop reaches Qt before an older show task.
    posted[1]()
    posted[0]()
    assert toast._window is None
    assert toast.show("Late", "Message") is False
    assert len(posted) == 2


def test_stop_hides_window_and_clears_gate_state(monkeypatch):
    posted = []
    monkeypatch.setattr(toast_module.qt_runtime, "post", posted.append)
    toast = ReminderToast()
    toast._window = Mock()
    toast._pending = [("Queued", "Message", None)]
    toast._gate_timer = Mock()

    toast.stop()
    posted.pop()()

    assert toast._gate_timer is None
    assert toast._pending == []
    toast._window.stop.assert_called_once_with()


def test_get_toast_constructs_one_singleton_across_threads(monkeypatch):
    created = []

    class _FakeToast:
        def __init__(self):
            time.sleep(0.01)
            created.append(self)

    monkeypatch.setattr(toast_module, "_toast", None)
    monkeypatch.setattr(toast_module, "ReminderToast", _FakeToast)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _n: toast_module.get_toast(), range(24)))

    assert len(created) == 1
    assert all(result is created[0] for result in results)


def test_notification_manager_reports_terminal_toast_rejection(monkeypatch, tmp_path):
    toast = Mock()
    toast.show.return_value = False
    monkeypatch.setattr(notifications_module, "get_toast", lambda: toast)
    manager = NotificationManager(tmp_path)

    assert manager.show_notification("Title", "Message") is False
    toast.show.assert_called_once_with(
        "Title",
        "Message",
        on_shown=None,
    )
