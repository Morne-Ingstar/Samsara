"""Recording feedback regressions; synthetic input, no microphone or live app."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import CaptureState, Lane, LiveSurfaceModel, NoticeKind
from samsara.live_surface.partials import CapturePartials


def test_silent_hold_is_a_receipt_not_a_persistent_document(monkeypatch):
    from samsara import streaming

    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    clock = SimpleNamespace(now=0.0)
    controller = LiveSurfaceController(SimpleNamespace(config={}),
        model=LiveSurfaceModel(clock=lambda: clock.now))
    capture = controller.begin_capture(Lane.HOLD)
    partials = CapturePartials(controller, Lane.HOLD, capture)
    session = streaming.StreamingSession.__new__(streaming.StreamingSession)
    session._state_lock = threading.Lock()
    session._parking_enabled = True
    session.cancel_event = threading.Event()
    session._direct_paste = False
    session._live_partials = partials
    session.app = SimpleNamespace(_log_history=Mock())
    session._overlay = streaming._LiveSurfaceStreamingOverlay(partials)
    session._mark_done = Mock()
    session._deliver_final('', '', 1.0, 0)
    controller.drain()
    assert controller.view().text == ''
    assert controller.view().notice.kind is NoticeKind.RESULT
    clock.now = 2.0
    assert controller.view().notice.kind is NoticeKind.NONE
    assert controller.view().capture is CaptureState.OFF
    controller.begin_capture(Lane.HOLD)
    controller.drain()
    assert controller.view().text == ''
    assert controller.view().notice.kind is NoticeKind.NONE


def test_wake_toggle_does_not_enter_a_listening_session(monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceIndicatorAdapter

    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    controller = LiveSurfaceController(SimpleNamespace(config={}))
    indicator = LiveSurfaceIndicatorAdapter(controller)
    indicator.set_mode('Hold')
    controller.drain()
    before = controller.view()
    indicator.set_wake_armed(True)
    controller.drain()
    assert controller.view() == before
    indicator.set_session_mode('HANDS FREE')
    controller.drain()
    assert controller.view().capture is CaptureState.HANDS_FREE_LISTENING
    indicator.set_session_mode(None)
    controller.drain()
    assert controller.view().capture is CaptureState.OFF


def test_long_partial_after_a_draft_is_scrollable_to_its_last_word(qapp):
    from PySide6.QtCore import Qt
    from samsara.live_surface.model import EventKind, SurfaceEvent
    from samsara.ui.live_surface_qt import LiveSurfaceWidget

    model = LiveSurfaceModel()
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, text='Earlier draft.'))
    model.consume(SurfaceEvent(EventKind.CAPTURE_STARTED, capture_id='hold', lane=Lane.HOLD))
    model.consume(SurfaceEvent(EventKind.PARTIAL, text=('Synthetic speech ' * 180) + 'LASTWORD'))
    widget = LiveSurfaceWidget(model, reduced_motion=True)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        widget.show()
        qapp.processEvents()
        assert 'Earlier draft.' in widget._transcript.toPlainText()
        assert 'LASTWORD' in widget._transcript.toPlainText()
        bar = widget._transcript.verticalScrollBar()
        assert bar.maximum() > 0
        assert bar.value() == bar.maximum()
        assert widget.card_content_rect.contains(widget._transcript.geometry())
    finally:
        widget.hide()
        widget.deleteLater()
