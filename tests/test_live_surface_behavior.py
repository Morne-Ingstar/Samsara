"""Behavior regressions with synthetic input; never import the desktop bootstrap."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, QPointF, QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
                                        LiveSurfaceModel, NoticeKind, SurfaceEvent, VisibleForm)
from samsara.live_surface.partials import CapturePartials
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record


@pytest.fixture
def surface(qapp, monkeypatch):
    # Dispatch is controlled by each test; timers run on this test's Qt loop.
    monkeypatch.setattr("samsara.live_surface.controller.qt_runtime.post", lambda *_: None)
    app = SimpleNamespace(config={})
    clock = [10.0]
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]))
    controller._screens = lambda: [Screen("test", Rect(0, 0, 1280, 900), True)]
    controller.start()
    yield controller, LiveSurfaceIndicatorAdapter(controller), clock
    controller.close()
    qapp.processEvents()


@pytest.mark.parametrize("edge", list(Edge))
def test_actual_widget_keeps_its_form_size_through_repeated_placement(surface, edge):
    c, _, _ = surface
    c.app.config = {"ui": {"live_surface": {"placement": {
        "preferred_monitor": "test", "monitors": {"test": placement_record(Placement(edge))}}}}}
    for text in (None, "Synthetic preview. " * 8):
        if text:
            c.begin_capture(Lane.HOLD)
            c.post(SurfaceEvent(EventKind.PARTIAL, text=text))
        c.drain()
        expected = (44, 44) if text is None else (500, c.widget.height())
        for _ in range(4):
            c._place_widget()
            assert (c.widget.width(), c.widget.height()) == expected
            assert c.widget.rect().contains(c.widget.mark_rect)
        QTest.qWait(150)  # No stale geometry animation can move it back.
        assert (c.widget.width(), c.widget.height()) == expected


@pytest.mark.parametrize("expanded", [False, True])
def test_drag_preserves_exact_mark_position_without_snap_or_resize(surface, expanded):
    c, _, _ = surface
    if expanded:
        c.begin_capture(Lane.HOLD)
        c.post(SurfaceEvent(EventKind.PARTIAL, text="Synthetic preview."))
        c.drain()
    w = c.widget
    initial_size = w.size()
    local = QPointF(w.mark_rect.center())
    start = QPointF(w.mapToGlobal(local.toPoint()))
    target = QPointF(460, 315)
    w.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, local, start,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    w.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, local, target,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    drop = w.geometry()
    c._place_widget()
    assert w.geometry() == drop and w.size() == initial_size
    w.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, local, target,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    QTest.qWait(150)
    assert w.geometry() == drop and w.size() == initial_size
    assert c.app.config["ui"]["live_surface"]["placement"]["monitors"]["test"]["edge"] == "free"


def test_success_expires_on_timer_without_another_capture(surface):
    c, indicator, clock = surface
    indicator.flash_success()
    c.drain()
    assert c.widget.view.form is VisibleForm.STATUS
    assert c.widget.width() == 240
    clock[0] += 1.1
    QTest.qWait(150)
    assert c.widget.view.form is VisibleForm.MARK
    assert c.widget.size().width() == c.widget.size().height() == 44


@pytest.mark.parametrize("name,lane,label", [("COMMAND", Lane.COMMAND, "Command mode"),
    ("AVA", Lane.AVA, "Ava listening"), ("HANDS FREE", Lane.HANDS_FREE_DICTATE, "Hands-free dictation")])
def test_mode_feedback_survives_legacy_hide_and_hold_completion(surface, name, lane, label):
    c, indicator, _ = surface
    indicator.set_session_mode(name, "unused")
    indicator.hide()
    c.drain()
    assert c.widget.isVisible()
    assert c.view().lane is lane and c.widget._state.text() == label
    hold = c.begin_capture(Lane.HOLD)
    c.stop_capture(capture_id=hold)
    c.drain()
    assert c.widget._state.text() == label
    indicator.set_session_mode(None)
    c.drain()
    assert c.view().capture is CaptureState.OFF


def test_surface_is_topmost_and_never_activates_on_feedback(surface):
    w = surface[0].widget
    assert w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_streaming_token_survives_delayed_legacy_indicator_callbacks(surface):
    c, indicator, _ = surface
    capture = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, capture)
    indicator.set_listening(True)
    partials.partial("Synthetic partial.")
    indicator.set_listening(False)
    indicator.show_outcome("Working", "pending", None)
    partials.final("Synthetic final.", delivered=True)
    partials.stop()
    c.drain()
    assert c.view().document is DocumentState.DELIVERED
    assert c.view().text == "Synthetic final."
    assert c.view().notice.kind is NoticeKind.NONE


def test_preview_pause_does_not_stop_hold_and_resume_can_publish(surface):
    from samsara.streaming import _LiveSurfaceDictateOverlay
    c, indicator, _ = surface
    indicator.set_session_mode("HANDS FREE")
    preview = CapturePartials(c, Lane.HANDS_FREE_DICTATE, c.begin_capture(Lane.HANDS_FREE_DICTATE))
    overlay = _LiveSurfaceDictateOverlay(c, preview)
    hold = c.begin_capture(Lane.HOLD)
    overlay.set_paused([])
    assert c._active_capture == hold
    c.stop_capture(capture_id=hold)
    overlay.resume()
    overlay.set_transcript([], "Synthetic resumed partial.")
    c.drain()
    assert c.view().provisional_text == "Synthetic resumed partial."
    assert c.view().capture is CaptureState.HANDS_FREE_LISTENING
    assert c.view().form is VisibleForm.LIVE


def test_preview_stays_suspended_until_stream_final_finishes():
    from samsara.streaming import DictatePreviewSession
    session = DictatePreviewSession.__new__(DictatePreviewSession)
    session.app = SimpleNamespace(config={}, _hotkey_recording=False, _streaming_session=object())
    assert session._suspended_for_hold()
    session.app._streaming_session = None
    assert not session._suspended_for_hold()


def test_multiline_partial_has_room_and_new_capture_drops_old_receipt(surface):
    c, _, _ = surface
    c.post(SurfaceEvent(EventKind.DELIVERY_FINISHED, text="Synthetic old receipt."))
    c.begin_capture(Lane.HOLD)
    c.post(SurfaceEvent(EventKind.PARTIAL, text="Synthetic partial words. " * 8))
    c.drain()
    assert c.view().text == ""
    assert "Synthetic partial words." in c.widget._transcript.toPlainText()
    assert c.widget._transcript.height() > 24
    assert c.widget.rect().contains(c.widget._transcript.geometry())


def test_reading_earlier_live_words_does_not_jump_to_latest(surface, qapp):
    c, _, _ = surface
    token = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, token)
    partials.partial("Synthetic text to scroll. " * 100)
    c.drain()
    qapp.processEvents()
    bar = c.widget._transcript.verticalScrollBar()
    assert bar.maximum() > 0
    c.widget._follow_latest = False
    bar.setValue(0)
    partials.partial("Synthetic text to scroll. " * 110)
    c.drain()
    qapp.processEvents()
    assert not c.widget._follow_latest and bar.value() == 0
    assert c.widget._latest.isVisible()
    c.widget._latest.click()
    assert bar.value() == bar.maximum()


def test_pause_vk_mapping_uses_real_polling_without_importing_dictation(monkeypatch):
    import ctypes
    tree = ast.parse((Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if (
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_VK_BY_NAME" for t in n.targets)
        or isinstance(n, ast.FunctionDef) and n.name == "_raw_key_pressed")]
    ns = {"logger": Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "dictation.py", "exec"), ns)
    ns["_raw_key_pressed"]._warned = set()
    ns["_raw_key_pressed"]._pressed_vks = {}
    poll = Mock(side_effect=lambda vk: 0x8000 if vk == 0x13 else 0)
    monkeypatch.setattr(ctypes.windll.user32, "GetAsyncKeyState", poll)
    for name in ("pause", "break", "pause/break"):
        assert ns["_raw_key_pressed"](name)
    assert not ns["_raw_key_pressed"]._warned


def test_pause_key_event_parks_once_even_when_async_poll_misses_the_pulse():
    from samsara.hotkeys import HotkeyCluster
    app = SimpleNamespace(
        config={"hotkey": "ctrl+shift", "ui": {"live_surface": {"enabled": True}}},
        get_key_name=lambda _: "pause", current_keys=set(), key_press_times={},
        _check_command_mode_key=Mock(), snoozed=False,
        check_hotkey_state=lambda _: False, _pause_active_hold=Mock(return_value=True))
    HotkeyCluster.on_key_press(app, object())
    app._pause_active_hold.assert_called_once()
    assert app._pause_hotkey_down


def test_command_results_and_ava_pending_are_not_silently_ignored(surface):
    c, indicator, clock = surface
    indicator.show_outcome("Synthetic command result", "success", 3000)
    c.drain()
    assert c.view().notice.message == "Synthetic command result"
    clock[0] += 1.1
    c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.NONE
    indicator.set_thinking(True)
    c.drain()
    assert c.view().notice.message == "Ava thinking"
    indicator.flash_error()
    indicator.clear_outcome()
    c.drain()
    assert c.view().notice.kind is NoticeKind.ERROR


def test_optional_ai_off_expires_without_further_input(surface):
    c, indicator, clock = surface
    indicator.show_outcome("Optional AI is off", "error", 1800)
    c.drain()
    assert c.widget._state.text() == "Optional AI is off"
    clock[0] += 1.9
    QTest.qWait(150)
    assert c.widget.view.notice.kind is NoticeKind.NONE
    assert c.widget.view.form is VisibleForm.MARK


def test_optional_ai_off_cannot_replace_live_words_or_leak_into_next_capture(surface):
    c, indicator, _ = surface
    capture = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, capture)
    partials.partial("Synthetic words remain visible.")
    indicator.show_outcome("Optional AI is off", "error", 1800)
    c.drain()
    assert c.widget.view.form is VisibleForm.LIVE
    assert c.widget._state.text() == "Recording"
    assert "Synthetic words remain visible." in c.widget._transcript.toPlainText()
    partials.stop()
    c.begin_capture(Lane.HOLD)
    c.drain()
    assert c.view().notice.kind is NoticeKind.NONE


def test_real_preview_loop_survives_hold_pause_and_resumes(surface):
    from samsara.streaming import DictatePreviewSession, _LiveSurfaceDictateOverlay
    c, _, _ = surface
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    partials = CapturePartials(c, Lane.HANDS_FREE_DICTATE, c.begin_capture(Lane.HANDS_FREE_DICTATE))
    preview._overlay = _LiveSurfaceDictateOverlay(c, partials)
    preview.app = SimpleNamespace(config={}, _hotkey_recording=True, _streaming_session=None)
    preview._closed, preview._generation, preview._finalized = False, 0, []
    preview._transcribe_partial = Mock(return_value="Synthetic resumed preview.")
    preview._is_control_phrase = lambda _: False
    preview._render = lambda text: preview._overlay.set_transcript([], text)
    class Stop:
        ticks = 0
        def is_set(self): return False
        def wait(self, timeout):
            self.ticks += 1
            preview.app._hotkey_recording = self.ticks == 1
            return self.ticks > 2
    preview._stop_event = Stop()
    preview._loop()
    c.drain()
    preview._transcribe_partial.assert_called_once()
    assert c.view().provisional_text == "Synthetic resumed preview."


def test_synthetic_visual_proofs(surface, qapp):
    import os
    from samsara.ui import theme
    destination = os.environ.get("SAMSARA_SURFACE_PROOF_DIR")
    if not destination:
        pytest.skip("Optional artifact destination is not configured")
    c, indicator, clock = surface
    old_theme = theme.active_theme()
    try:
        for palette in ("light", "dark"):
            theme.set_theme(palette, refresh=False)
            indicator.set_session_mode("COMMAND")
            c.drain()
            qapp.processEvents()
            assert c.widget.grab().save(str(Path(destination) / f"command-{palette}.png"))
            indicator.set_session_mode(None)
            capture = c.begin_capture(Lane.HOLD)
            partials = CapturePartials(c, Lane.HOLD, capture)
            partials.partial("This is a synthetic live preview. Words appear while recording, and the final text is inserted when the hold ends.")
            c.drain()
            qapp.processEvents()
            assert c.widget.grab().save(str(Path(destination) / f"recording-{palette}.png"))
            partials.final("Synthetic final.", delivered=True)
            partials.stop()
            c.drain()
            clock[0] += 1.1
            c.refresh_expired()
            assert c.widget.width() == 44
            assert c.widget.grab().save(str(Path(destination) / f"idle-{palette}.png"))
    finally:
        theme.set_theme(old_theme, refresh=False)
