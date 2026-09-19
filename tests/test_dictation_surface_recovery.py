"""Regressions for normal hold delivery and the compact surface controls.

Only bind capture methods from the bootstrap AST; never import dictation or
attach devices, global hotkeys, or the running app's log handler.
"""
import ast
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import CaptureState, DocumentState
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tests.test_hold_park_220 import make_manager
from tests.test_live_surface_review import review


@pytest.fixture
def capture_app():
    tree = ast.parse((Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    names = {"_start_recording_impl", "_live_surface_hold_parking_enabled"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    foreground = {"hwnd": 41}
    namespace = dict(logger=Mock(), flight_recorder=Mock(),
                     _get_foreground_hwnd=lambda: foreground["hwnd"],
                     _get_foreground_exe_lower=lambda: "writer.exe")
    exec(compile(ast.Module(body=methods, type_ignores=[]), "dictation.py", "exec"), namespace)
    manager, _, _ = make_manager()
    app = SimpleNamespace(
        config={"mode": "hold", "ui": {"live_surface": {"enabled": True}}},
        model_loaded=True, recording=False, _streaming_session=None, _stop_in_flight=False,
        _wake_consumer=None, _dictation_consumer=SimpleNamespace(activate=Mock(return_value=True)),
        _ensure_session_mode_manager=lambda: manager, _open_hold_capture_duck=Mock(),
        _request_icon_chase=Mock(), _duck_audio=Mock(),
    )
    app.set_app_state = lambda **kw: app.__dict__.update(kw)
    for name in names:
        setattr(app, name, MethodType(namespace[name], app))
    return app, manager, foreground


def test_normal_hold_saves_destination_before_capture_and_refreshes_each_hold(capture_app):
    app, manager, foreground = capture_app
    destinations = []
    app._dictation_consumer.activate.side_effect = lambda: destinations.append(
        getattr(app, "_hold_capture_target_hwnd", None)) or True
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    assert destinations == [41]
    assert app._hold_park_requested is False
    app.recording = False
    app._hold_park_requested = True  # Previous completed capture must not leak its latch.
    foreground["hwnd"] = 42
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    assert destinations == [41, 42]
    assert not app._hold_park_requested and not manager.draft_document.text


def test_explicitly_parked_text_is_preserved_when_a_new_hold_starts(capture_app):
    app, manager, _ = capture_app
    manager.pause_draft()
    manager.stage_parked_hold("Synthetic draft.")
    app._start_recording_impl(streaming=False, play_earcon=False)
    assert app._hold_park_requested
    assert manager.draft_document.text == "Synthetic draft."


def test_busy_start_does_not_overwrite_the_current_destination(capture_app):
    app, _, foreground = capture_app
    app.recording = True
    app._hold_capture_target_hwnd = 40
    app._start_recording_impl(streaming=False, play_earcon=False)
    assert app._hold_capture_target_hwnd == 40
    app._dictation_consumer.activate.assert_not_called()


@pytest.mark.parametrize("special", [None, "command_mode_recording", "ava_mode_recording", "_memo_recording"])
def test_live_surface_ordinary_hold_streams_but_special_captures_stay_batch(capture_app, monkeypatch, special):
    import samsara.streaming as streaming
    app, _, _ = capture_app
    app.live_surface = object()
    app._dictation_consumer.activate_streaming = Mock(return_value=True)
    if special:
        setattr(app, special, True)
    session = Mock()
    constructor = Mock(return_value=session)
    monkeypatch.setattr(streaming, "StreamingSession", constructor)
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    if special:
        constructor.assert_not_called()
        app._dictation_consumer.activate.assert_called_once()
    else:
        app._dictation_consumer.activate_streaming.assert_called_once()
        session.start.assert_called_once()
        assert app._streaming_session is session


def test_parked_holds_have_word_boundaries_and_undo_stays_local():
    manager, inserted, _ = make_manager()
    manager.stage_parked_hold("First sentence.")
    manager.stage_parked_hold("Second sentence.")
    assert manager.draft_document.text == "First sentence. Second sentence."
    assert manager._do_scratch_that()
    assert manager.draft_document.text == "First sentence."
    assert inserted == []


@pytest.mark.parametrize("action", ["_clear", "_commit"])
def test_surface_actions_synchronize_authoritative_document(action):
    manager, inserted, _ = make_manager()
    manager.stage_parked_hold("Synthetic draft.")
    controller = LiveSurfaceController(SimpleNamespace(_session_mode_manager=manager))
    controller.sync_draft(manager)
    controller.drain()
    assert controller.view().document is DocumentState.PARKED_DRAFT
    getattr(controller, action)()
    controller.drain()
    assert controller.view().text == ""
    assert controller.view().document is DocumentState.EMPTY
    assert inserted == (["Synthetic draft."] if action == "_commit" else [])


def test_failed_explicit_insert_keeps_the_surface_recoverable():
    manager, _, _ = make_manager()
    manager.stage_parked_hold("Synthetic draft.")
    manager._inject_fn = lambda *_: False
    controller = LiveSurfaceController(SimpleNamespace(_session_mode_manager=manager))
    controller._commit()
    controller.drain()
    assert controller.view().text == "Synthetic draft."
    assert controller.view().document is DocumentState.PARKED_DRAFT


def test_new_parked_draft_is_visible_after_clear_and_stale_updates_are_rejected():
    from samsara.live_surface.model import EventKind, SurfaceEvent

    manager, _, _ = make_manager()
    manager.stage_parked_hold("First draft.")
    controller = LiveSurfaceController(SimpleNamespace(_session_mode_manager=manager))
    controller.sync_draft(manager); controller.drain()
    old = controller.view()
    controller._clear(); controller.drain()
    manager.stage_parked_hold("Next draft.")
    controller.sync_draft(manager); controller.drain()
    assert controller.view().text == "Next draft."
    assert controller.view().document is DocumentState.PARKED_DRAFT
    assert not controller.model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED,
        document_id=old.document_id, revision=old.revision, text="Stale draft."))


@pytest.mark.parametrize("outcome", ["insert", "insert_ai_off", "pause", "focus", "failure", "exception"])
def test_streaming_final_inserts_or_preserves_the_whole_pending_document(monkeypatch, outcome):
    import samsara.streaming as streaming
    from samsara.live_surface.model import Lane
    from samsara.live_surface.partials import CapturePartials
    from tests.test_hold_park_220 import TestStreamingParkDelivery

    manager, _, _ = make_manager()
    app = SimpleNamespace(
        _hold_park_requested=outcome == "pause", _session_mode_manager=manager,
        _ensure_session_mode_manager=lambda: manager, _verbatim_rule=lambda: None,
        config={"auto_paste": True}, _dictation_consumer=None,
        _paste_preserving_clipboard=Mock(return_value=outcome != "failure"),
        _on_streaming_session_finished=Mock(), play_sound=Mock(),
        add_to_history=Mock(), _log_history=Mock(), _notify_main_window=Mock(),
    )
    if outcome == "exception":
        app._paste_preserving_clipboard.side_effect = RuntimeError("Synthetic insertion failure")
    controller = app.live_surface = LiveSurfaceController(app)
    session = TestStreamingParkDelivery()._session(app)
    session._target_hwnd = 41
    monkeypatch.setattr(streaming, "_user32", SimpleNamespace(GetForegroundWindow=lambda: 42 if outcome == "focus" else 41))
    capture_id = controller.begin_capture(Lane.HOLD)
    session._overlay = streaming._LiveSurfaceStreamingOverlay(CapturePartials(controller, Lane.HOLD, capture_id))
    if outcome == "insert_ai_off":
        from samsara.live_surface.controller import LiveSurfaceIndicatorAdapter
        LiveSurfaceIndicatorAdapter(controller).show_outcome("Optional AI is off", "error", 1800)
    if outcome == "pause":
        manager.stage_parked_hold("Earlier draft.")
    session._deliver_final("Final sentence.", None, 1.0, 0)
    controller.drain()
    assert controller.view().capture is CaptureState.OFF
    if outcome in ("insert", "insert_ai_off"):
        app._paste_preserving_clipboard.assert_called_once_with("Final sentence.")
        assert controller.view().document is DocumentState.DELIVERED
        assert not manager.has_parked_hold
    else:
        assert controller.view().document is DocumentState.PARKED_DRAFT
        assert controller.view().text == manager.draft_document.text
        assert controller.view().text == ("Earlier draft. " if outcome == "pause" else "") + "Final sentence."
        if outcome in ("pause", "focus"):
            app._paste_preserving_clipboard.assert_not_called()


def test_refresh_follows_new_text_without_inventing_unread_segments(qapp):
    widget = LiveSurfaceWidget(review("word " * 500))
    try:
        widget.show(); qapp.processEvents()
        for count in (501, 502, 503):
            widget.refresh(review("word " * count), animate=False)
            qapp.processEvents()
            bar = widget._transcript.verticalScrollBar()
            assert widget._follow_latest and bar.value() == bar.maximum()
            assert widget._unread_segments == 0 and not widget._latest.isVisible()
    finally:
        widget.deleteLater()


def test_scrollback_survives_refresh_and_header_controls_never_overlap(qapp):
    widget = LiveSurfaceWidget(review("word " * 500))
    try:
        widget.show(); qapp.processEvents()
        bar = widget._transcript.verticalScrollBar()
        widget.scroll_draft("up")
        position = bar.value()
        assert not widget._follow_latest
        widget.refresh(review("word " * 501), animate=False)
        qapp.processEvents()
        assert bar.value() == position
        assert widget._latest.isVisible()
        controls = (widget._state, widget._latest, widget._clear)
        for i, control in enumerate(controls):
            assert widget.card_content_rect.contains(control.geometry())
            for other in controls[i + 1:]:
                assert not control.geometry().intersects(other.geometry())
        widget._latest_clicked()
        assert widget._follow_latest and bar.value() == bar.maximum()
    finally:
        widget.deleteLater()


def test_surface_never_offers_an_insert_button_for_recording_pending_or_delivered_text(qapp):
    widget = LiveSurfaceWidget(review())
    try:
        widget.show(); qapp.processEvents()
        for view in (review(), replace(review(), capture=CaptureState.RECORDING),
                     replace(review(), document=DocumentState.EDITABLE_DRAFT),
                     replace(review(), document=DocumentState.DELIVERED)):
            widget.refresh(view, animate=False)
            assert all(button.text() != "Insert text" for button in widget.findChildren(type(widget._clear)))
        assert widget._state.text() == "Inserted"
    finally:
        widget.deleteLater()


def test_manual_scroll_shows_latest_without_waiting_for_more_dictation(qapp):
    from PySide6.QtWidgets import QAbstractSlider

    widget = LiveSurfaceWidget(review("word " * 500))
    try:
        widget.show(); qapp.processEvents()
        bar = widget._transcript.verticalScrollBar()
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        qapp.processEvents()
        assert not widget._follow_latest and widget._latest.isVisible()
        widget._latest.click(); qapp.processEvents()
        assert widget._follow_latest and not widget._latest.isVisible()
    finally:
        widget.deleteLater()


def test_correction_editor_replaces_choices_and_survives_refresh(qapp):
    widget = LiveSurfaceWidget(review())
    try:
        widget.show()
        widget.show_candidates({"word": "two", "revision": 1}, [])
        widget._open_editor("edit")
        widget._editor.setText("replacement")
        widget.refresh(animate=False); qapp.processEvents()
        assert widget._editor.isVisible() and widget._editor.text() == "replacement"
        assert not any(c.isVisible() for c in (*widget._picker_buttons, widget._keep, widget._spell, widget._edit))
        assert not widget._editor.geometry().intersects(widget._apply.geometry())
        assert widget.card_content_rect.contains(widget._cancel.geometry())
        widget._cancel_editor()
        assert widget._keep.isVisible() and not widget._editor.isVisible()
    finally:
        widget.deleteLater()
