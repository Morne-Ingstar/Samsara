"""941: deterministic coverage for queued Qt lifetime and watchdog gaps."""
import threading
from unittest.mock import Mock

from PySide6.QtCore import QEvent
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import delete, isValid

from tests import conftest as support
from tests.test_home_nav_825 import hub
from tools import ci_hang_reporter as reporter


def test_inline_details_survive_resize_during_their_own_layout(hub, monkeypatch):
    window, _app = hub
    window._nav_btns['Commands'].click()
    page = window._stack.currentWidget()
    page._filter.setText('open notepad')
    view = page._list
    original = view.doItemsLayout
    injected = []

    def layout():
        original()
        if not injected:
            injected.append(True)
            # Showing the inline row can resize the viewport/scrollbar. This
            # event belongs to placement and must not dismiss the same card.
            QApplication.sendEvent(view, QResizeEvent(view.size(), view.size()))

    monkeypatch.setattr(view, 'doItemsLayout', layout)
    page._on_item_clicked(view.item(0))
    assert injected
    assert page._detail_card.isVisibleTo(page)
    assert page._detail_card.phrase == 'open notepad'
    # A later user resize still dismisses details as before.
    QApplication.sendEvent(view, QResizeEvent(view.size(), view.size()))
    assert not page._detail_card.isVisibleTo(page)


def test_overview_queued_work_is_cancelled_when_receiver_is_deleted(qapp):
    from samsara.ui import window_overview_qt as ui

    calls = []

    class Overview(ui.OverviewWindow):
        def _register_thumbnails(self):
            calls.append(('register', isValid(self)))
            super()._register_thumbnails()

        def _update_thumbnails(self, *_):
            calls.append(('update', isValid(self)))
            super()._update_thumbnails()

    window = Overview([], Mock(), Mock(), thumbnails=Mock())
    window.show()
    calls.clear()
    delete(window)  # Keep its Python wrapper, as an outstanding timer would.
    qapp.processEvents()
    assert calls == []


def test_item_deadline_is_not_extended_by_other_workers(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_CI_HEARTBEAT_DIR', str(tmp_path))
    monkeypatch.setenv('SAMSARA_TEST_HARD_TIMEOUT', '7')
    monkeypatch.setattr(support, '_watchdog_monotonic_701', lambda: 10)
    support._ci_progress_711('test', 'tests/test_probe.py::test_stuck')
    states = reporter.heartbeat_state(tmp_path)
    state = next(iter(states.values()))
    assert state['deadline'] == 17
    states['other.json'] = dict(phase='between-tests', nodeid='other', progress=100)
    assert reporter.expired_items(states, 16.9) == []
    assert reporter.expired_items(states, 17) == ['tests/test_probe.py::test_stuck']
    support._ci_progress_711('between-tests', 'tests/test_probe.py::test_stuck')
    assert reporter.expired_items(reporter.heartbeat_state(tmp_path), 100) == []


def test_blocked_console_cannot_delay_termination_or_junit(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    running = [True]
    child = Mock(pid=123, returncode=1)
    child.poll.side_effect = lambda: None if running[0] else 1
    root = Mock(pid=123)
    root.children.return_value = []
    root.kill.side_effect = lambda: running.__setitem__(0, False)

    def blocked_print(*args, **kwargs):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(reporter, 'print', blocked_print, raising=False)
    monkeypatch.setattr(reporter.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(reporter.psutil, 'Process', lambda pid: root)
    monkeypatch.setattr(reporter, 'dump_stacks', lambda *a: None)
    monkeypatch.setattr(reporter, 'describe', lambda *a: [])
    tick = iter([0, 2, 2])
    monkeypatch.setattr(reporter.time, 'monotonic', lambda: next(tick))
    try:
        result = reporter.supervise(['pytest'], tmp_path, idle_seconds=1,
                                    junit_path=tmp_path / 'junit.xml')
        assert entered.is_set()
        assert result == 124
        root.kill.assert_called_once()
        assert 'CIHang' in (tmp_path / 'junit.xml').read_text(encoding='utf-8')
        assert 'deadline exceeded' in (tmp_path / 'reporter.log').read_text(encoding='utf-8')
    finally:
        release.set()


def test_native_crash_log_lives_until_session_cleanup(tmp_path, monkeypatch):
    import faulthandler

    monkeypatch.setenv('SAMSARA_CI_HEARTBEAT_DIR', str(tmp_path))
    enable, disable = Mock(), Mock()
    monkeypatch.setattr(faulthandler, 'enable', enable)
    monkeypatch.setattr(faulthandler, 'disable', disable)
    config = Mock()
    support._ci_native_crash_log_941(config)
    stream = enable.call_args.kwargs['file']
    assert not stream.closed
    assert stream.name.startswith(str(tmp_path / 'native-crash-'))
    assert enable.call_args.kwargs['all_threads'] is True
    config.add_cleanup.call_args.args[0]()
    assert stream.closed
    disable.assert_called_once_with()
