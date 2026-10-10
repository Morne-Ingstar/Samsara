"""Focused real-platform tests for the code-native Qt startup splash."""
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPainter
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


def test_error_keeps_reported_ready_capability_honest(app):
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
    widget._set_ready_ladder((("hotkey: ready", "ready"), ("wake: loading", "loading"), ("Ava: checking", "checking")))
    widget._set_error("Startup failed", "Microphone unavailable")
    painter = RecordingPainter()
    widget._paint_text(painter)
    assert painter.text == ["Samsara", "De-articulating Splines.",
                            "Startup failed", "Microphone unavailable"]
    widget.close()


def test_close_holds_completed_state_briefly(app):
    widget = _SplashWidget()
    widget.show()
    widget._set_reduced_motion(True)
    widget._set_complete("Ready", "Voice services online")
    widget._begin_close()
    assert widget.isVisible()  # close is deferred for the completion hold

    widget._completion_started_ms = widget._now_ms() - _COMPLETION_HOLD_MS
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
    assert any(image.pixelColor(x, 50).alpha() > 0 for x in range(170, 270))
    assert image.pixelColor(40, 120).alpha() == 0  # no card behind the mark
    widget.close()


def test_centre_is_the_brand_mark_drawn_by_the_shared_routine(app, monkeypatch):
    """The splash and the static brand share the articulated, rotating mark."""
    import samsara.ui.splash_qt as splash_module
    from samsara.ui import theme

    calls = []
    monkeypatch.setattr(splash_module, "paint_mark",
                        lambda painter, rect, capture, eye, rotation=0.0, opacity=1.0, brand=False:
                        calls.append((capture, eye, rotation, brand)))
    widget = _SplashWidget()
    canvas = QImage(10, 10, QImage.Format.Format_ARGB32)   # must outlive the painter
    painter = QPainter(canvas)
    widget._paint_vortex(painter, 0.0)
    widget._paint_vortex(painter, 1.0)
    painter.end()
    assert [c[:2] for c in calls] == [("listening", "off")] * 2
    assert calls[1][2] != calls[0][2]  # the mark itself turns
    assert all(c[3] for c in calls)
    source = Path(splash_module.__file__).read_text(encoding="utf-8")
    assert "theme.RECORDING" not in source and "theme.BRAND_RED" not in source
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
    assert _SIGNATURE_TEXT not in painter.text  # keep the startup surface quiet
    widget.close()


def test_startup_copy_is_only_brand_until_a_real_delay_or_error(app):
    class RecordingPainter:
        def __init__(self):
            self.text = []
        def save(self): pass
        def restore(self): pass
        def setPen(self, pen): pass
        def setFont(self, font): pass
        def drawText(self, rect, alignment, text): self.text.append(text)

    widget = _SplashWidget(clock_ms=lambda: 0)
    widget._set_progress(0.65)
    painter = RecordingPainter()
    widget._paint_text(painter)
    assert painter.text == ["Samsara", "De-articulating Splines."]
    widget._set_error("Startup failed", "Microphone unavailable")
    painter.text.clear()
    widget._paint_text(painter)
    assert "Startup failed" in painter.text
    assert "Microphone unavailable" in painter.text
    widget.close()
