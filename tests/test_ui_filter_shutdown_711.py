"""Event-filter recursion and shutdown must stop before touching Qt wrappers."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QWidget
from shiboken6 import delete

from samsara.ui import focus_ring, passive_topmost


@pytest.fixture(params=['focus', 'topmost'])
def guard(request, qapp):
    widget = QWidget()
    controller = (focus_ring.FocusRings(qapp) if request.param == 'focus'
                  else passive_topmost.PassiveTopmost(widget))
    yield controller, widget
    controller.stop()
    delete(widget)
    if request.param == 'focus':
        delete(controller)


def test_filter_reentry_is_blocked_before_event_type(guard):
    controller, widget = guard
    calls = []
    def kind():
        calls.append(1)
        assert controller.eventFilter(widget, event) is False
        return QEvent.Type.User
    event = SimpleNamespace(type=kind)
    assert controller.eventFilter(widget, event) is False
    assert calls == [1]
    assert not controller._filtering


def test_guard_resets_after_event_exception(guard):
    controller, widget = guard
    def fail():
        raise ValueError('synthetic event failure')
    with pytest.raises(ValueError):
        controller.eventFilter(widget, SimpleNamespace(type=fail))
    assert not controller._filtering


@pytest.mark.parametrize('reason', ['stop', 'finalizing', 'closing'])
def test_shutdown_does_not_touch_event_or_widget(guard, monkeypatch, reason):
    controller, _ = guard
    module = focus_ring if isinstance(controller, focus_ring.FocusRings) else passive_topmost
    if reason == 'stop':
        controller.stop()
        controller.stop()
    elif reason == 'finalizing':
        monkeypatch.setattr(module.sys, 'is_finalizing', lambda: True)
    else:
        monkeypatch.setattr(module.QCoreApplication, 'closingDown', lambda: True)
    assert controller.eventFilter(object(), object()) is False
    if isinstance(controller, passive_topmost.PassiveTopmost):
        controller.refresh()
        assert not controller.timer.isActive()


def test_quit_signal_uninstalls_filters(qapp):
    widget = QWidget()
    focus = focus_ring.FocusRings(qapp)
    topmost = passive_topmost.PassiveTopmost(widget)
    topmost.timer.start()
    # Emit the shutdown notification without quitting the shared application.
    qapp.aboutToQuit.emit()
    assert focus._stopped and topmost._stopped
    assert not topmost.timer.isActive()
    delete(widget)
    delete(focus)


def test_widget_destruction_stops_passive_timer(qapp):
    widget = QWidget()
    topmost = passive_topmost.PassiveTopmost(widget)
    topmost.timer.start()
    delete(widget)
    assert topmost._stopped
    assert topmost.eventFilter(object(), object()) is False


def test_passive_refresh_reentry_is_blocked(qapp, monkeypatch):
    widget = QWidget()
    controller = passive_topmost.PassiveTopmost(widget)
    calls = []
    def repair(target):
        calls.append(target)
        controller.refresh()
    monkeypatch.setattr(passive_topmost, 'restore', repair)
    controller.refresh()
    assert calls == [widget]
    controller.stop()
    delete(widget)
