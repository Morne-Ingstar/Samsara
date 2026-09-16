"""Queue 75: the hands-free DICTATE preview fades (never vanishes, by default)
when nobody is speaking, lets clicks through while faded, animates its
"Listening..." dots and shows catalog-generated hints -- all from one
Qt-thread timer that is stopped before the widget is torn down.

Never imports dictation (Samsara may be running from this tree); the one
dictation.py behaviour relied on (the listening indicator is force-shown for
the hands-free session) is pinned by source text, as queue 62's tests do.
"""
import gc
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import streaming as st
from samsara.streaming import (
    ALPHA,
    DictatePreviewSession,
    IdleSettings,
    StreamingOverlayQt,
    _LIVE_WIDGETS,
    _StreamingWidget,
    idle_hints_from_catalog,
    window_is_click_through,
)

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self):
        self.ms = 0


def _widget(qapp, *, delay=5.0, opacity=0.25, hints=(), reduced_motion=False, speaking=None):
    speaking = speaking if speaking is not None else [False]
    clock = _Clock()
    w = _StreamingWidget(False, IdleSettings(delay, opacity), lambda: speaking[0], hints)
    w._w._now = lambda: clock.ms
    w._w._reduced_motion = reduced_motion
    w.show_overlay()
    qapp.processEvents()
    return w, clock, speaking


def _tick_to(w, clock, ms, step=50):
    while clock.ms < ms:
        clock.ms = min(ms, clock.ms + step)
        w._w._life_tick()


def _close(qapp, w):
    w.stop_life()
    w._w.hide()
    w._w.deleteLater()
    qapp.processEvents()


# -- settings ----------------------------------------------------------------

def test_idle_settings_defaults_and_bad_values_fall_back():
    assert (IdleSettings.from_config({}).delay_s, IdleSettings.from_config({}).opacity) == (5.0, 0.25)
    cfg = {"command_mode": {"preview_idle_delay_s": 12, "preview_idle_opacity": 0.0}}
    s = IdleSettings.from_config(cfg)
    assert (s.delay_s, s.opacity, s.fully_hidden) == (12.0, 0.0, True)
    for bad in ({"preview_idle_delay_s": 0.1}, {"preview_idle_delay_s": "soon"},
                {"preview_idle_opacity": 1.5}, {"preview_idle_opacity": float("nan")}):
        s = IdleSettings.from_config({"command_mode": bad})
        assert (s.delay_s, s.opacity) == (5.0, 0.25), bad
    assert IdleSettings.from_config(None).opacity == 0.25


# -- fade ------------------------------------------------------------------------

def test_idle_for_the_delay_fades_and_speech_restores_in_one_step(qapp):
    w, clock, speaking = _widget(qapp)
    try:
        _tick_to(w, clock, 4950)
        assert w._w.windowOpacity() == pytest.approx(ALPHA, abs=0.01)
        assert not w._w._click_through
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS // 2)
        assert 0.25 < w._w.windowOpacity() < ALPHA            # gradual on the way out
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS + 50)
        assert w._w.windowOpacity() == pytest.approx(0.25, abs=0.01)
        assert w._w._click_through

        speaking[0] = True
        clock.ms += 50
        w._w._life_tick()                                        # ONE tick
        assert w._w.windowOpacity() == pytest.approx(ALPHA, abs=0.01)
        assert not w._w._click_through
    finally:
        _close(qapp, w)


def test_new_text_is_shown_at_full_opacity_with_no_hint(qapp):
    w, clock, _speaking = _widget(qapp, hints=["Say \"scratch that\" - undo"])
    try:
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS + st.HINT_FIRST_AFTER_MS + 100)
        assert w._w._hint.isVisible()
        seen = []
        real_set_text = w._w._label.setText

        def spy(text):
            seen.append((text, w._w.windowOpacity(), w._w._hint.isVisible()))
            real_set_text(text)
        w._w._label.setText = spy
        w._w._on_update("hello there", StreamingOverlayQt.STATE_LISTENING, "rich")
        assert seen[0][0] == "hello there"
        assert seen[0][1] == pytest.approx(ALPHA, abs=0.01)
        assert seen[0][2] is False
    finally:
        _close(qapp, w)


def test_placeholder_and_repeated_text_are_not_activity(qapp):
    w, clock, _speaking = _widget(qapp)
    try:
        w._w._on_update("hello", StreamingOverlayQt.STATE_LISTENING, "rich")
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS + 50)
        assert w._w._faded
        w._w._on_update("Listening...", StreamingOverlayQt.STATE_PLACEHOLDER, "rich")
        w._w._on_update("hello", StreamingOverlayQt.STATE_LISTENING, "rich")    # same text again
        assert w._w._faded
    finally:
        _close(qapp, w)


def test_reduced_motion_switches_opacity_without_animating(qapp):
    w, clock, _speaking = _widget(qapp, reduced_motion=True)
    try:
        texts = set()
        for ms in range(50, 5001, 50):
            clock.ms = ms
            w._w._life_tick()
            texts.add(w._w._label.text())
            assert w._w._fade_started_ms is None
        assert w._w.windowOpacity() == pytest.approx(0.25, abs=0.01)
        assert texts == {"Listening..."}                          # static dots
    finally:
        _close(qapp, w)


def test_listening_dots_cycle_one_two_three_on_the_same_timer(qapp):
    w, clock, _speaking = _widget(qapp)
    try:
        seen = []
        for ms in (0, 400, 800, 1200, 1600):
            clock.ms = ms
            w._w._life_tick()
            seen.append(w._w._label.text())
        assert seen == ["Listening.", "Listening..", "Listening...", "Listening.", "Listening.."]
        from PySide6.QtCore import QTimer
        timers = w._w.findChildren(QTimer)
        # the hold-to-stream flash timer plus exactly one idle timer
        assert len([t for t in timers if t is not w._w._fade_timer]) == 1
    finally:
        _close(qapp, w)


def test_dots_stop_when_text_arrives(qapp):
    w, clock, _speaking = _widget(qapp)
    try:
        w._w._on_update("real words", StreamingOverlayQt.STATE_LISTENING, "rich")
        for ms in (400, 800, 1200):
            clock.ms = ms
            w._w._life_tick()
        assert w._w._label.text() == "real words"
    finally:
        _close(qapp, w)


# -- hints -------------------------------------------------------------------------

def test_hints_come_from_the_catalog_and_skip_missing_commands():
    from samsara.command_catalog import load_catalog_json
    records = load_catalog_json()
    assert records, "commands_catalog.json must load"
    hints = idle_hints_from_catalog(records)
    assert hints
    phrases = {a for r in records for a in r.get("aliases", [])}
    for hint in hints:
        spoken = hint.split('"')[1]
        assert spoken in phrases, hint
    only = [{"canonical_id": "builtin.scratch_that", "aliases": ["scratch that"],
             "description": "Remove the last pasted dictation"}]
    assert idle_hints_from_catalog(only) == ['Say "scratch that" - remove the last pasted dictation']
    assert idle_hints_from_catalog(None) == []


def test_hint_waits_for_idle_rotates_and_vanishes_on_speech(qapp):
    hints = ["hint A", "hint B"]
    w, clock, speaking = _widget(qapp, hints=hints)
    try:
        faded_at = 5000 + st.IDLE_FADE_MS
        _tick_to(w, clock, faded_at + st.HINT_FIRST_AFTER_MS - 100)
        assert not w._w._hint.isVisible()
        _tick_to(w, clock, faded_at + st.HINT_FIRST_AFTER_MS + 100)
        assert w._w._hint.isVisible() and w._w._hint.text() == "hint A"
        _tick_to(w, clock, clock.ms + st.HINT_ROTATE_MS)
        assert w._w._hint.text() == "hint B"
        speaking[0] = True
        clock.ms += 50
        w._w._life_tick()
        assert not w._w._hint.isVisible()
    finally:
        _close(qapp, w)


def test_no_hint_when_fully_hidden(qapp):
    w, clock, _speaking = _widget(qapp, opacity=0.0, hints=["hint A"])
    try:
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS + st.HINT_FIRST_AFTER_MS + 1000)
        assert w._w.windowOpacity() == pytest.approx(0.0, abs=0.01)
        assert w._w.isVisible()                    # faded out, not closed
        assert not w._w._hint.isVisible()
    finally:
        _close(qapp, w)


# -- click-through ---------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows hit test")
def test_faded_preview_lets_the_click_reach_the_window_underneath(qapp):
    """The OS's own hit test, not a Qt attribute: WindowFromPoint at the
    centre of the preview returns the preview while active and the window
    underneath while faded."""
    import ctypes
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QWidget

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    window_from_point = ctypes.WINFUNCTYPE(ctypes.c_void_p, POINT)(("WindowFromPoint", ctypes.windll.user32))
    get_ancestor = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)(
        ("GetAncestor", ctypes.windll.user32))

    w, clock, speaking = _widget(qapp)
    under = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.Tool)
    try:
        under.setGeometry(w._w.geometry().adjusted(-40, -40, 40, 40))
        under.show()
        w._w.raise_()
        qapp.processEvents()

        def top_window_at_preview_centre():
            c = w._w.mapToGlobal(QPoint(w._w.width() // 2, w._w.height() // 2))
            ratio = w._w.devicePixelRatioF()
            hwnd = window_from_point(POINT(int(c.x() * ratio), int(c.y() * ratio)))
            return get_ancestor(hwnd, 2) if hwnd else None

        assert top_window_at_preview_centre() == int(w._w.winId())
        assert not window_is_click_through(w._w)
        _tick_to(w, clock, 5000 + st.IDLE_FADE_MS + 50)
        qapp.processEvents()
        assert window_is_click_through(w._w)
        assert w._w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert top_window_at_preview_centre() == int(under.winId())

        speaking[0] = True
        clock.ms += 50
        w._w._life_tick()
        qapp.processEvents()
        assert not window_is_click_through(w._w)
        assert not w._w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert top_window_at_preview_centre() == int(w._w.winId())
    finally:
        under.close()
        _close(qapp, w)


# -- teardown (queue 60 crash class) -----------------------------------------

def _drain(qapp, seconds=0.3):
    from PySide6.QtCore import QCoreApplication, QEvent
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


def test_teardown_mid_fade_from_a_worker_thread_leaves_no_timer_or_reference(qapp):
    import shiboken6
    from PySide6.QtCore import QThread

    class _Session:
        def __init__(self):
            self.calls = 0

        def probe(self):
            self.calls += 1
            return False

    session = _Session()
    session_ref = weakref.ref(session)
    overlay = StreamingOverlayQt(dim=False, idle=IdleSettings(1.0, 0.25), activity_probe=session.probe)
    overlay.idle_hints = ["hint"]
    overlay.show()
    _drain(qapp, 0.05)
    widget = overlay._widget
    assert widget is not None and widget in _LIVE_WIDGETS
    timer = widget._w._life_timer
    assert timer.isActive()
    # Put it mid-fade.
    widget._w._begin_idle(widget._w._now())
    assert widget._w._fade_started_ms is not None

    destroyed_on = []
    widget._w.destroyed.connect(lambda *_: destroyed_on.append(QThread.currentThread() is qapp.thread()))
    widget_ref = weakref.ref(widget)
    del widget

    def worker():
        overlay.close()          # the utterance thread's teardown in queue 60
    t = threading.Thread(target=worker, name="cmd-utt-queue")
    t.start()
    t.join()
    del overlay
    del session
    _drain(qapp)
    gc.collect()

    assert destroyed_on == [True]                   # deleted on the Qt thread
    assert not shiboken6.isValid(timer)             # the timer went with its widget
    assert widget_ref() is None and not any(True for _ in _LIVE_WIDGETS if _ is widget_ref())
    assert session_ref() is None                    # no probe reference left behind


def test_close_stops_the_timer_before_the_widget_is_deleted(qapp):
    calls = []
    overlay = StreamingOverlayQt(dim=False, idle=IdleSettings(1.0, 0.25),
                                 activity_probe=lambda: calls.append(1) or False)
    overlay.show()
    _drain(qapp, 0.2)
    assert calls, "the idle timer polls while shown"
    widget = overlay._widget
    timer = widget._w._life_timer
    overlay._dispose_on_qt()                        # the Qt-thread half of close()
    assert not timer.isActive()                     # stopped before deleteLater runs
    assert widget._w._activity_probe is None
    n = len(calls)
    _drain(qapp, 0.2)
    assert len(calls) == n


def test_hold_to_stream_overlay_has_no_idle_timer(qapp):
    w = _StreamingWidget(True)
    try:
        assert w._w._life_timer is None
        w._w._on_update("partial", "listening", "auto")
        assert w._w._idle is None
    finally:
        w._w.deleteLater()
        qapp.processEvents()


# -- session wiring ------------------------------------------------------------

def test_preview_session_probe_reads_speech_onset_and_config():
    app = SimpleNamespace(config={"command_mode": {"preview_idle_delay_s": 9, "preview_idle_opacity": 0.4}},
                          is_speaking=False)
    session = DictatePreviewSession(app)
    assert (session.idle.delay_s, session.idle.opacity) == (9.0, 0.4)
    assert session._overlay._idle is session.idle
    assert session._speech_active() is False
    app.is_speaking = True
    assert session._speech_active() is True
    session._closed = True
    assert session._speech_active() is False


def test_fully_hidden_still_leaves_the_listening_indicator_showing_state():
    """The preview never touches the indicator, and dictation.py force-shows
    the indicator for the whole hands-free session regardless of the user's
    indicator setting -- so a fully hidden idle preview is never the only
    signal, and never the absence of one."""
    streaming_src = (ROOT / "samsara" / "streaming.py").read_text(encoding="utf-8")
    assert "listening_indicator" not in streaming_src
    dictation_src = (ROOT / "dictation.py").read_text(encoding="utf-8")
    start = dictation_src.index("self._ensure_wake_consumer('toggle_session')")
    window = dictation_src[start:start + 900]
    assert "Force-visible for the session's duration regardless of" in window
    assert "listening_indicator.show" in window
    assert "set_session_mode" in dictation_src[start:start + 2500] or "_update_mode_overlay" in dictation_src


# -- Settings controls ----------------------------------------------------------

def test_modes_tab_controls_round_trip_and_fully_hidden_is_labelled(qapp):
    from tests.test_modes_config_keys_62 import _win
    app, win, save = _win({"command_mode": {"preview_idle_delay_s": 7.5, "preview_idle_opacity": 0.3}})
    delay, opacity = win._widgets['cmd_preview_idle_delay'], win._widgets['cmd_preview_idle_opacity']
    warn = win._widgets['cmd_preview_idle_opacity_warn']
    assert delay.value() == pytest.approx(7.5) and opacity.value() == 30
    assert warn.isHidden()
    opacity.setValue(0)
    assert opacity.text() == "Fully hidden"
    assert not warn.isHidden()
    delay.setValue(12.0)
    updates = save(None)
    assert updates["command_mode"]["preview_idle_delay_s"] == 12.0
    assert updates["command_mode"]["preview_idle_opacity"] == 0.0
    settings = IdleSettings.from_config(updates)
    assert settings.fully_hidden and settings.delay_s == 12.0


def test_modes_tab_defaults_match_the_schema(qapp):
    from tests.test_modes_config_keys_62 import _win
    _app, win, _save = _win({"command_mode": {}})
    assert win._widgets['cmd_preview_idle_delay'].value() == pytest.approx(st.IDLE_DELAY_S_DEFAULT)
    assert win._widgets['cmd_preview_idle_opacity'].value() == round(st.IDLE_OPACITY_DEFAULT * 100)
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA[st.IDLE_DELAY_KEY]["default"] == st.IDLE_DELAY_S_DEFAULT
    assert SETTINGS_SCHEMA[st.IDLE_OPACITY_KEY]["default"] == st.IDLE_OPACITY_DEFAULT
