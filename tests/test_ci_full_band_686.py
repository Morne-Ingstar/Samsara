"""Bounded Qt draining and fixture-owned Full display teardown (686)."""
from time import monotonic
from unittest.mock import patch

from PySide6.QtCore import QAbstractAnimation, QObject, QPropertyAnimation, QRect, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from samsara.ui import theme
from samsara.live_surface.controller import LiveSurfaceController
from tests.test_full_display_band_456 import surface


def test_drain_returns_with_repeating_zero_timer_alive(qapp, qt_deferred_delete):
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(None))
    victim = QObject()
    victim.deleteLater()
    timer.start(0)
    try:
        started = monotonic()
        qt_deferred_delete()
        elapsed = monotonic() - started
        assert elapsed < 0.5, f"Qt drain took {elapsed:.3f}s with a repeating timer"
        assert ticks and timer.isActive()
        assert not isValid(victim)
    finally:
        timer.stop()


def test_owned_delete_does_not_dispatch_unrelated_deletes_or_timers(qapp, qt_deferred_delete):
    owner = QWidget()
    other = QWidget()
    ticks = []
    timer = QTimer(other)
    timer.timeout.connect(lambda: ticks.append(None))
    timer.start(0)
    owner.deleteLater()
    other.deleteLater()
    try:
        qt_deferred_delete(owner)
        assert not isValid(owner)
        assert isValid(other) and timer.isActive()
        assert not ticks
    finally:
        if isValid(other):
            timer.stop()
            qt_deferred_delete(other)


def test_surface_stops_activity_and_deletes_before_patches_end(qapp, monkeypatch, qt_widget_scope):
    previous = theme.active_theme()
    close = LiveSurfaceController.close
    observed = []

    def checked_close(controller):
        widget = controller.widget
        observed.append((all(not timer.isActive() for timer in widget.findChildren(QTimer)),
                         all(animation.state() == QAbstractAnimation.State.Stopped
                             for animation in widget.findChildren(QAbstractAnimation))))
        close(controller)

    # The scope captures c.close on entry, so install the spy before next().
    monkeypatch.setattr(LiveSurfaceController, 'close', checked_close)
    with patch('samsara.live_surface.controller.qt_runtime.post'):
        fixture = surface.__wrapped__(qapp, monkeypatch, qt_widget_scope)
        controller, _, _ = next(fixture)
        widget = controller.widget
        timers = widget.findChildren(QTimer)
        assert any(timer.isActive() for timer in timers)
        # Exercise an animation even though 456 normally requests reduced motion.
        animation = QPropertyAnimation(widget, b"geometry", widget)
        animation.setStartValue(widget.geometry())
        animation.setEndValue(QRect(widget.x(), widget.y(), widget.width() + 1, widget.height()))
        animation.setDuration(10000)
        animation.start()
        destroyed = []
        widget.destroyed.connect(lambda *_: destroyed.append(theme.active_theme()))
        theme.set_theme('light' if previous == 'dark' else 'dark')
        during = theme.active_theme()
        try:
            fixture.close()
            assert observed == [(True, True)]
            assert controller.widget is None and not isValid(widget)
            assert destroyed == [during]
            assert theme.active_theme() == previous
        finally:
            fixture.close()
            theme.set_theme(previous)
