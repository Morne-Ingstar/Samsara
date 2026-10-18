"""Review fixtures release native Qt objects before interpreter shutdown (687)."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPropertyAnimation, QRect, QTimer
from shiboken6 import isValid

from samsara.live_surface.controller import LiveSurfaceController
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tests.test_live_surface_review import manager, review, surface_owner


@pytest.mark.parametrize("body_fails", [False, True])
def test_review_ownership_deletes_widgets_and_controller_on_every_exit(
        qapp, qt_deferred_delete, body_fails):
    ticks = []
    unrelated = QObject()
    unrelated_timer = QTimer(unrelated)
    unrelated_timer.timeout.connect(lambda: ticks.append(True))
    unrelated_timer.start(0)
    fixture = contextmanager(surface_owner.__wrapped__)
    observed = []
    try:
        try:
            with fixture(qapp, qt_deferred_delete) as own:
                subject = manager()
                subject.pause_draft()
                subject.stage_parked_hold("Synthetic review words ")
                controller = own(LiveSurfaceController(SimpleNamespace(
                    _session_mode_manager=subject, voice_training_window=None)))
                controller.sync_draft(subject)
                controller.drain()
                controller.start()
                widget = controller.widget
                # Direct review widgets also contain callbacks retaining themselves.
                direct = own(LiveSurfaceWidget(review()))
                direct.show_word_picker()
                timers = widget.findChildren(QTimer)
                assert any(timer.isActive() for timer in timers)
                animation = QPropertyAnimation(widget, b"geometry", widget)
                animation.setStartValue(widget.geometry())
                animation.setEndValue(QRect(0, 0, 600, 300))
                animation.setDuration(10000)
                animation.start()
                close = controller.close

                def checked_close():
                    observed.append((all(not timer.isActive() for timer in timers),
                                     animation.state() == QPropertyAnimation.State.Stopped))
                    close()

                controller.close = checked_close
                QTimer.singleShot(0, qapp, controller.drain)
                unrelated.deleteLater()
                if body_fails:
                    raise AssertionError("synthetic test-body failure")
        except AssertionError as error:
            if not body_fails or str(error) != "synthetic test-body failure":
                raise
        assert observed == [(True, True)]
        assert controller.widget is None
        assert not isValid(widget) and not isValid(direct)
        assert not isValid(animation) and all(not isValid(timer) for timer in timers)
        assert isValid(unrelated) and unrelated_timer.isActive()
        assert ticks == []
        # Pending controller work is safe after its owned widget has gone.
        unrelated_timer.stop()
        qapp.processEvents()
        assert controller.widget is None
    finally:
        if isValid(unrelated):
            unrelated_timer.stop()
            unrelated.deleteLater()
            qt_deferred_delete(unrelated)
