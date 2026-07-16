"""Focused, offscreen-safe tests for the code-native Qt startup splash."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from samsara.ui.splash_qt import (
    _COMPLETION_HOLD_MS,
    _LOGICAL_H,
    _LOGICAL_W,
    _SIGNATURE_TEXT,
    _SplashWidget,
    _system_reduced_motion,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_progress_accepts_fraction_percent_and_indeterminate(app):
    widget = _SplashWidget()
    widget._set_progress(0.42)
    assert widget._progress == pytest.approx(0.42)
    widget._set_progress(78)
    assert widget._progress == pytest.approx(0.78)
    widget._set_progress(500)
    assert widget._progress == 1.0
    widget._set_progress(None)
    assert widget._progress is None
    widget.close()


def test_reduced_motion_stops_animation_but_allows_updates(app):
    widget = _SplashWidget()
    widget._set_reduced_motion(False)
    widget.show()
    app.processEvents()
    assert widget._frame_timer.isActive()

    widget._set_reduced_motion(True)
    widget._set_status("Loading speech model")
    widget._set_detail("Verifying model files")
    widget._set_progress(0.5)

    assert not widget._frame_timer.isActive()
    assert widget._status == "Loading speech model"
    assert widget._detail == "Verifying model files"
    assert widget._progress == 0.5
    widget.close()


def test_complete_and_error_states_are_explicit(app):
    widget = _SplashWidget()
    widget._set_complete("Ready", "Voice services online")
    assert widget._complete is True
    assert widget._error is False
    assert widget._progress == 1.0
    assert widget._completion_started_ms is not None

    widget._set_error("Startup failed", "Microphone unavailable")
    assert widget._complete is False
    assert widget._error is True
    assert widget._status == "Startup failed"
    widget.close()


def test_close_holds_completed_state_briefly(app):
    widget = _SplashWidget()
    widget.show()
    widget._set_reduced_motion(True)
    widget._set_complete("Ready", "Voice services online")
    widget._begin_close()
    assert widget.isVisible()  # close is deferred for the completion hold

    widget._completion_started_ms = widget._elapsed.elapsed() - _COMPLETION_HOLD_MS
    widget._begin_close()
    app.processEvents()
    assert not isValid(widget)


def test_scene_renders_without_assets_or_opengl(app):
    widget = _SplashWidget()
    widget._set_reduced_motion(True)
    image = QImage(int(_LOGICAL_W), int(_LOGICAL_H), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image)
    assert not image.isNull()
    assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() > 0
    widget.close()


def test_timer_is_capped_at_thirty_frames_per_second(app):
    widget = _SplashWidget()
    assert widget._frame_timer.interval() >= 33
    widget.close()


def test_system_motion_preference_is_a_safe_boolean(app):
    assert isinstance(_system_reduced_motion(), bool)


def test_makers_mark_uses_exact_signature_text(app):
    class RecordingPainter:
        def __init__(self):
            self.text = []

        def save(self):
            pass

        def restore(self):
            pass

        def setPen(self, pen):
            pass

        def setFont(self, font):
            pass

        def drawText(self, rect, alignment, text):
            self.text.append(text)

    widget = _SplashWidget()
    painter = RecordingPainter()
    widget._paint_text(painter)
    assert _SIGNATURE_TEXT == "A Morne Ingstar Production"
    assert painter.text[-1] == _SIGNATURE_TEXT
    widget.close()
