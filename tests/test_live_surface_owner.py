"""Owner reported preview regressions, exercised with synthetic documents only."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from samsara.live_surface.model import (
    CaptureState, EventKind, Lane, LiveSurfaceModel, SurfaceEvent, VisibleForm,
)
from samsara.ui.live_surface_qt import LiveSurfaceWidget, render_device_mark
from samsara.ui import theme


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_staged_draft_stays_inspectable_after_idle_and_capture_changes():
    clock = Clock()
    model = LiveSurfaceModel(clock=clock)
    model.consume(SurfaceEvent(EventKind.LANE_CHANGED, lane=Lane.HANDS_FREE_DICTATE,
                               capture=CaptureState.HANDS_FREE_LISTENING))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="synthetic-draft",
                               revision=7, text="Synthetic staged sentence."))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="synthetic-draft",
                               revision=7))
    for event in (
        SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING),
        SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.TRANSCRIBING),
        SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.HANDS_FREE_LISTENING),
        SurfaceEvent(EventKind.PARTIAL, text=""),
    ):
        model.consume(event)
        assert model.view().text == "Synthetic staged sentence."
    clock.now += 5.1
    view = model.view()
    assert view.form is VisibleForm.LIVE
    assert (view.document_id, view.revision) == ("synthetic-draft", 7)


def test_parked_draft_is_visible_and_navigation_has_no_send_or_clear(qapp):
    clock = Clock()
    model = LiveSurfaceModel(clock=clock)
    model.consume(SurfaceEvent(EventKind.LANE_CHANGED, lane=Lane.HANDS_FREE_DICTATE,
                               capture=CaptureState.HANDS_FREE_LISTENING))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="synthetic-draft",
                               revision=7, text="Synthetic staged sentence."))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="synthetic-draft",
                               revision=7))
    clock.now = 5.1
    widget = LiveSurfaceWidget(model, reduced_motion=True)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    sent, cleared = [], []
    widget.commit_requested.connect(lambda: sent.append(True))
    widget.clear_requested.connect(lambda: cleared.append(True))
    widget.show()
    qapp.processEvents()
    assert widget._transcript.isVisible()
    assert "Synthetic staged sentence." in widget._transcript.toPlainText()
    assert widget._insert.isVisible() and widget._clear.isVisible()
    widget.hide()
    widget.show()
    widget.refresh()
    qapp.processEvents()
    assert widget._transcript.isVisible()
    assert not sent and not cleared
    assert (model.view().document_id, model.view().revision) == ("synthetic-draft", 7)
    widget.hide()
    widget.deleteLater()


def test_long_parked_draft_keeps_scroll_and_explicit_actions(qapp):
    clock = Clock()
    model = LiveSurfaceModel(clock=clock)
    text = " ".join(f"Synthetic{i}" for i in range(180))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="synthetic-scroll",
                               revision=1, text=text))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="synthetic-scroll",
                               revision=1))
    clock.now = 6
    widget = LiveSurfaceWidget(model, reduced_motion=True)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    qapp.processEvents()
    bar = widget._transcript.verticalScrollBar()
    assert bar.maximum() > 0
    widget.scroll_draft("top")
    assert not widget._follow_latest and bar.value() == bar.minimum()
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="synthetic-scroll",
                               revision=2, text=text + " Final synthetic segment."))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="synthetic-scroll",
                               revision=2))
    widget.refresh()
    qapp.processEvents()
    assert bar.value() == bar.minimum()
    assert widget._latest.isVisible() and widget._insert.isVisible() and widget._clear.isVisible()
    assert (widget.view.document_id, widget.view.revision) == ("synthetic-scroll", 2)
    widget._latest.click()
    assert widget._follow_latest and bar.value() == bar.maximum()
    widget.hide()
    widget.deleteLater()


def test_empty_hands_free_state_remains_compact():
    model = LiveSurfaceModel(clock=Clock())
    model.consume(SurfaceEvent(EventKind.LANE_CHANGED, lane=Lane.HANDS_FREE_DICTATE,
                               capture=CaptureState.HANDS_FREE_LISTENING))
    assert model.view().form is VisibleForm.STATUS


def test_clock_driven_mark_rotation_colour_reversal_and_reduced_motion(qapp):
    clock = Clock()
    model = LiveSurfaceModel(clock=clock)
    widget = LiveSurfaceWidget(model, reduced_motion=False, motion_clock=clock)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    idle = QColor(theme.ICON_IDLE)
    listening = QColor(theme.ACCENT)
    recording = QColor(theme.RECORDING)
    transcribing = QColor(theme.AVA)
    assert widget._mark.color == idle
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED,
                               capture=CaptureState.HANDS_FREE_LISTENING))
    widget.refresh()
    assert widget._mark.color == idle
    clock.now += .15
    widget._advance_mark()
    assert widget._mark.rotation > 0
    assert widget._mark.color not in (idle, listening)
    clock.now += .2
    widget._advance_mark()
    assert widget._mark.color == listening
    angle = widget._mark.rotation
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING))
    widget.refresh()
    assert widget._mark.rotation == angle
    clock.now += .15
    widget._advance_mark()
    assert widget._mark.color not in (listening, recording)
    midway = widget._mark.color
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.TRANSCRIBING))
    widget.refresh()
    assert widget._mark.color == midway
    clock.now += .35
    widget._advance_mark()
    assert widget._mark.color == transcribing
    widget.set_reduced_motion(True)
    assert not widget._spin_timer.isActive()
    assert widget._mark.rotation == 0
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING))
    widget.refresh()
    assert widget._mark.color == recording
    assert widget._state.text() == "Hold — recording"
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="synthetic-motion",
                               revision=2, text="Synthetic visible draft."))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="synthetic-motion",
                               revision=2))
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED,
                               capture=CaptureState.HANDS_FREE_LISTENING))
    widget.refresh()
    assert widget._mark.color == listening
    assert widget._transcript.isVisible()
    assert widget._mark.rotation == 0
    widget.hide()
    widget.deleteLater()


def test_mark_recolour_preserves_rendered_brand_silhouette(qapp):
    grey = render_device_mark("listening", "armed", dpr=1.0, rotation=32,
                              color=QColor(theme.ICON_IDLE))
    turquoise = render_device_mark("listening", "armed", dpr=1.0, rotation=32,
                                   color=QColor(theme.ACCENT))
    alpha = []
    colored = []
    for y in range(grey.height()):
        for x in range(grey.width()):
            a = grey.pixelColor(x, y)
            b = turquoise.pixelColor(x, y)
            assert a.alpha() == b.alpha()
            if a.alpha() == 255:
                alpha.append(a)
                colored.append(b)
    assert alpha and colored
    assert alpha[0] == QColor(theme.ICON_IDLE)
    assert colored[0] == QColor(theme.ACCENT)


def test_mark_timers_pause_while_hidden_and_resume_without_angle_jump(qapp):
    clock = Clock()
    model = LiveSurfaceModel(clock=clock)
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING))
    widget = LiveSurfaceWidget(model, reduced_motion=False, motion_clock=clock)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    assert not widget._spin_timer.isActive()
    widget.show()
    assert widget._spin_timer.isActive()
    clock.now += .25
    widget._advance_mark()
    angle = widget._mark.rotation
    widget.hide()
    assert not widget._spin_timer.isActive() and not widget._motion_timer.isActive()
    clock.now += 10
    widget.show()
    assert widget._mark.rotation == angle
    clock.now += .1
    widget._advance_mark()
    assert widget._mark.rotation > angle
    widget.hide()
    widget.deleteLater()
