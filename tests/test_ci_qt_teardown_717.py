"""717: test windows must not outlive their callbacks or reach the next palette."""
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPropertyAnimation, QRect, QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from tests import conftest as support


LEAKED = []


@pytest.fixture(scope='module')
def shared_window(qapp):
    widget = QWidget()
    yield widget
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)


def test_01_leave_shown_hidden_and_pending_windows(shared_window):
    for state in ('shown', 'hidden', 'pending'):
        widget = QWidget()
        timer = QTimer(widget)
        timer.start(0)
        LEAKED.append((widget, timer))
        if state == 'shown':
            widget.show()
        elif state == 'pending':
            widget.deleteLater()


def test_02_previous_test_windows_are_gone_but_module_owner_survives(shared_window):
    assert len(LEAKED) == 3
    assert all(not isValid(obj) for pair in LEAKED for obj in pair)
    assert isValid(shared_window)


@pytest.mark.parametrize('fail', (False, True))
def test_cleanup_stops_activity_before_close_and_runs_on_failure(qapp, fail):
    observations = []
    previous = support._qt_windows_717()

    class Window(QWidget):
        def closeEvent(self, event):
            observations.append((not timer.isActive(),
                                 animation.state() == QPropertyAnimation.State.Stopped))
            event.ignore()  # A refused close must still be retired by teardown.

    widget = Window()
    timer = QTimer(widget)
    timer.start(0)
    animation = QPropertyAnimation(widget, b'geometry', widget)
    animation.setStartValue(QRect(0, 0, 10, 10))
    animation.setEndValue(QRect(10, 10, 20, 20))
    animation.setDuration(10000)
    animation.start()

    @contextmanager
    def scope():
        try:
            yield
        finally:
            support._qt_cleanup_windows_717(previous)

    if fail:
        with pytest.raises(AssertionError, match='synthetic'):
            with scope():
                raise AssertionError('synthetic')
    else:
        with scope():
            pass
    assert observations == [(True, True)]
    assert all(not isValid(obj) for obj in (widget, timer, animation))


def test_cleanup_preserves_existing_owner_pending_delete_and_callbacks(qapp):
    other = QWidget()
    timer = QTimer(other)
    ticks = []
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(0)
    other.deleteLater()
    previous = support._qt_windows_717()
    owned = QWidget()
    support._qt_cleanup_windows_717(previous)
    assert not isValid(owned)
    assert isValid(other) and timer.isActive() and not ticks
    timer.stop()
    QCoreApplication.sendPostedEvents(other, QEvent.Type.DeferredDelete)


def test_cleanup_handles_parent_child_windows_and_delete_on_close(qapp):
    previous = support._qt_windows_717()
    parent = QWidget()
    child = QWidget(parent, Qt.WindowType.Window)
    parent.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    parent.show()
    child.show()
    support._qt_cleanup_windows_717(previous)
    assert not isValid(parent) and not isValid(child)


def test_cleanup_closes_owner_before_its_tool_window(qapp, monkeypatch):
    previous = support._qt_windows_717()
    observations = []

    class Owner(QWidget):
        def closeEvent(self, event):
            observations.append(isValid(child))
            child.hide()
            super().closeEvent(event)

        def hide(self):
            raise AssertionError('retirement must bypass public hide overrides')

    owner = Owner()
    child = QWidget(owner, Qt.WindowType.Tool)
    # Qt does not promise parent-first ordering for its top-level inventory.
    monkeypatch.setattr(support, '_qt_windows_717',
                        lambda: tuple(w for w in (child, owner) if isValid(w)))
    support._qt_cleanup_windows_717(previous)
    assert observations == [True]
    assert not isValid(owner) and not isValid(child)


def test_cleanup_retires_a_window_created_by_close(qapp):
    previous = support._qt_windows_717()
    created = []

    class Window(QWidget):
        def closeEvent(self, event):
            created.append(QWidget())
            super().closeEvent(event)

    widget = Window()
    support._qt_cleanup_windows_717(previous)
    assert len(created) == 1
    assert not isValid(widget) and not isValid(created[0])


def test_non_qt_tests_do_not_import_or_create_an_application(monkeypatch):
    monkeypatch.delitem(support.sys.modules, 'PySide6.QtWidgets')
    assert support._qt_windows_717() == ()
    support._qt_cleanup_windows_717(())


@pytest.fixture
def patched_window(qapp, monkeypatch):
    widget = QWidget()
    native = widget.close
    calls = []
    monkeypatch.setattr(widget, 'close', lambda: (calls.append(True), native())[-1])
    LEAKED.append((widget, calls))
    return widget


def test_03_leave_window_with_a_test_owned_close_callback(patched_window):
    assert isValid(patched_window)


def test_04_cleanup_precedes_monkeypatch_restoration():
    widget, calls = LEAKED[-1]
    assert not isValid(widget) and calls == [True]
