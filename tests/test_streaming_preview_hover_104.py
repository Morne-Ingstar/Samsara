"""Queue 104: the idle DICTATE preview wakes when the pointer arrives over it.

The incident: the owner reached for "Clear draft" on a box that had gone idle
and click-through, the click went into the app underneath, and the only way
back in was to SPEAK -- which dictated the wake utterance into the draft he
was trying to clear.

The fix cannot be event-driven. An idle preview has WS_EX_TRANSPARENT set, so
the OS hit test skips the window and Qt is never sent an enter, leave, move or
hover event for it. What the box does instead is hit-test itself: read where
the pointer IS (QCursor.pos / GetCursorPos, a device-state query that no hit
test gates) and compare it with the box's own screen rect, on the single idle
timer queue 75 already runs.

Never imports dictation (Samsara may be running from this tree).
"""
import ctypes
import sys
import time

import pytest

from samsara import streaming as st
from samsara.streaming import (
    ALPHA,
    IdleSettings,
    StreamingOverlayQt,
    _StreamingWidget,
    window_is_click_through,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="click-through and foreground checks are Win32")

#: Windows' "this window is never activated" ex-style. The preview asks for it
#: via Qt.WindowDoesNotAcceptFocus; pinned here because it, not politeness, is
#: what makes waking on hover structurally unable to steal the foreground.
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20

IDLE_AT_MS = 5000 + st.IDLE_FADE_MS + 50


class _Clock:
    def __init__(self):
        self.ms = 0


class _Cursor:
    """Stands in for the pointer. Counts every query, so a test can assert
    the poll did NOT happen as easily as that it did."""

    def __init__(self):
        self.point = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.point is None:
            raise AssertionError("cursor polled before a test placed it")
        return self.point

    def inside(self, widget):
        from PySide6.QtCore import QPoint
        r = widget.frameGeometry()
        self.point = QPoint(r.left() + r.width() // 2, r.top() + r.height() // 2)

    def outside(self, widget):
        from PySide6.QtCore import QPoint
        r = widget.frameGeometry()
        self.point = QPoint(r.right() + 500, r.top() - 500)


def _count_timers(widget):
    from PySide6.QtCore import QTimer
    return len([c for c in widget.children() if isinstance(c, QTimer)])


def _widget(qapp, monkeypatch, *, delay=5.0, opacity=0.25, speaking=None):
    """An idle-enabled preview with a fake pointer wired in, parked outside.

    The real QTimer is stopped straight away: every tick in these tests is
    driven by hand off a fake clock, so a stray 50 ms firing can never make a
    call-count assertion flap. The one test that cares about the real timer
    starts it again itself."""
    speaking = speaking if speaking is not None else [False]
    clock = _Clock()
    cursor = _Cursor()
    w = _StreamingWidget(False, IdleSettings(delay, opacity), lambda: speaking[0], ())
    w._w._now = lambda: clock.ms
    w._w._reduced_motion = True          # opacity in one step: no fade arithmetic
    monkeypatch.setattr(st, "cursor_screen_pos", cursor)
    w.show_overlay()
    w._w._life_timer.stop()
    qapp.processEvents()
    cursor.outside(w._w)
    return w, clock, cursor, speaking


def _tick_to(w, clock, ms, step=50):
    while clock.ms < ms:
        clock.ms = min(ms, clock.ms + step)
        w._w._life_tick()


def _close(qapp, w):
    w.stop_life()
    w._w.hide()
    w._w.deleteLater()
    qapp.processEvents()


# -- the pointer reaches an unreachable box ----------------------------------

def test_cursor_inside_the_rect_wakes_an_idle_click_through_box(qapp, monkeypatch):
    """The reported incident, in the order it happened: draft on screen, box
    goes idle and click-through, the Clear button disappears -- and then the
    pointer arriving is enough to bring all of it back."""
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    try:
        w._w._on_update("clear this draft", StreamingOverlayQt.STATE_LISTENING, "rich")
        assert w._w._controls.isVisible()                 # "Clear draft" reachable

        _tick_to(w, clock, IDLE_AT_MS)
        assert w._w._faded
        assert w._w._click_through and window_is_click_through(w._w)
        assert not w._w._interactive
        assert not w._w._controls.isVisible()             # the defect: no way in
        assert w._w.windowOpacity() == pytest.approx(0.25, abs=0.01)

        cursor.inside(w._w)
        clock.ms += 50
        w._w._life_tick()                                 # ONE tick
        qapp.processEvents()

        assert not w._w._faded
        assert not w._w._click_through
        assert not window_is_click_through(w._w)          # the OS hit test again
        assert w._w._interactive
        assert w._w._controls.isVisible()                 # Clear draft is clickable
        assert w._w.windowOpacity() == pytest.approx(ALPHA, abs=0.01)
    finally:
        _close(qapp, w)


def test_the_box_stays_awake_for_as_long_as_the_pointer_is_on_it(qapp, monkeypatch):
    """Hover is activity on every tick it holds, not a one-shot: resting the
    mouse on the box while reading the draft must not fade it out again after
    the idle delay."""
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    try:
        _tick_to(w, clock, IDLE_AT_MS)
        assert w._w._faded

        cursor.inside(w._w)
        _tick_to(w, clock, clock.ms + 30_000)             # six idle delays of hovering
        assert not w._w._faded
        assert not w._w._click_through
    finally:
        _close(qapp, w)


def test_leaving_restarts_the_countdown_instead_of_idling_at_once(qapp, monkeypatch):
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    try:
        _tick_to(w, clock, IDLE_AT_MS)
        cursor.inside(w._w)
        _tick_to(w, clock, clock.ms + 1000)
        assert not w._w._faded

        cursor.outside(w._w)
        left_at = clock.ms
        _tick_to(w, clock, left_at + 4900)
        assert not w._w._faded, "idled early -- the countdown did not restart on leaving"
        _tick_to(w, clock, left_at + 5100 + st.IDLE_FADE_MS)
        assert w._w._faded
        assert w._w._click_through
    finally:
        _close(qapp, w)


# -- the two things waking must NOT do ---------------------------------------

def test_waking_by_hover_does_not_move_the_foreground_or_the_focused_control(qapp, monkeypatch):
    """The box sits over the user's work. Waking it must leave the keyboard
    exactly where it was -- verified against the OS's own idea of the
    foreground window, not just Qt's."""
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

    get_foreground = ctypes.WINFUNCTYPE(ctypes.c_void_p)(("GetForegroundWindow", ctypes.windll.user32))
    get_long = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int)(
        ("GetWindowLongPtrW", ctypes.windll.user32))

    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    host = QWidget()
    try:
        lay = QVBoxLayout(host)
        edit = QLineEdit()
        lay.addWidget(edit)
        host.show()
        host.activateWindow()
        edit.setFocus()
        qapp.processEvents()

        # WS_EX_NOACTIVATE is the structural reason this holds: a window with
        # it set is not activated by a click, a SetWindowPos or a style
        # change, so nothing on the wake path CAN take the foreground.
        assert get_long(int(w._w.winId()), GWL_EXSTYLE) & WS_EX_NOACTIVATE

        _tick_to(w, clock, IDLE_AT_MS)
        assert w._w._click_through
        foreground_before = get_foreground()
        focus_before = qapp.focusWidget()
        active_before = qapp.activeWindow()
        assert foreground_before != int(w._w.winId())

        cursor.inside(w._w)
        clock.ms += 50
        w._w._life_tick()
        qapp.processEvents()

        assert w._w._interactive and not w._w._click_through   # it really woke
        assert get_foreground() == foreground_before
        assert get_foreground() != int(w._w.winId())
        assert qapp.focusWidget() is focus_before
        assert qapp.activeWindow() is active_before
        assert not w._w.isActiveWindow()
    finally:
        host.close()
        _close(qapp, w)


def test_hover_never_wakes_a_fully_hidden_preview(qapp, monkeypatch):
    """Opacity 0 is an explicit "hide the preview completely" setting. The
    window is still a rectangle the OS would hit-test the moment
    WS_EX_TRANSPARENT came off, so waking it on hover would put an INVISIBLE
    click-eating box under the pointer. It stays click-through, and the
    pointer is not even polled."""
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch, opacity=0.0)
    try:
        _tick_to(w, clock, IDLE_AT_MS)
        assert w._w._faded and w._w._click_through
        polled = cursor.calls

        cursor.inside(w._w)
        # The guard itself, not just its effect: the pointer IS over the rect
        # and the box still declines to call that hovering.
        assert w._w.frameGeometry().contains(cursor.point)
        assert w._w._cursor_inside() is False
        _tick_to(w, clock, clock.ms + 5000)

        assert w._w._faded
        assert w._w._click_through and window_is_click_through(w._w)
        assert not w._w._interactive
        assert w._w.windowOpacity() == pytest.approx(0.0, abs=0.01)
        assert cursor.calls == polled, "fully hidden must not cost a cursor query"
    finally:
        _close(qapp, w)


# -- cost ---------------------------------------------------------------------

def test_the_poll_does_not_run_while_the_box_is_hidden(qapp, monkeypatch):
    """Both halves: _life_tick's own isVisible() gate, and the same thing end
    to end with the REAL 50 ms timer running against a hidden window."""
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    try:
        _tick_to(w, clock, 500)
        assert cursor.calls > 0                       # polled while shown

        w._w.hide()
        qapp.processEvents()
        polled = cursor.calls
        _tick_to(w, clock, clock.ms + 5000)
        assert cursor.calls == polled

        # Real timer, real wall clock, still hidden: many firings, no polls.
        w._w._life_timer.start()
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)
        w._w._life_timer.stop()
        assert cursor.calls == polled

        w._w.show()
        qapp.processEvents()
        polled = cursor.calls
        clock.ms += 50
        w._w._life_tick()
        assert cursor.calls == polled + 1
    finally:
        _close(qapp, w)


def test_hold_to_stream_has_no_cursor_poll_at_all(qapp, monkeypatch):
    """The hold-to-stream box (idle is None) has no life timer, so the poll
    does not exist for it -- and _cursor_inside says so if ever called."""
    cursor = _Cursor()
    monkeypatch.setattr(st, "cursor_screen_pos", cursor)
    w = _StreamingWidget(False)
    try:
        w.show_overlay()
        qapp.processEvents()
        assert w._w._life_timer is None
        assert w._w._cursor_inside() is False
        assert cursor.calls == 0
    finally:
        w._w.hide()
        w._w.deleteLater()
        qapp.processEvents()


def test_one_tick_costs_one_cursor_query_and_adds_no_second_timer(qapp, monkeypatch):
    """The whole per-tick cost of queue 104: a single cursor query plus a
    rectangle test. The rect comes from Qt's cached crect, so frameGeometry()
    is not a syscall; and enabling idle still adds exactly ONE timer to the
    plain box -- queue 75's "the ONE timer" rule survives this change."""
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch)
    plain = _StreamingWidget(False)
    try:
        assert _count_timers(w._w) == _count_timers(plain._w) + 1

        before = cursor.calls
        clock.ms += 50
        w._w._life_tick()
        assert cursor.calls == before + 1
    finally:
        plain._w.deleteLater()
        _close(qapp, w)


def test_speech_short_circuits_the_cursor_query(qapp, monkeypatch):
    """While the user is talking the box is awake for that reason; there is
    nothing for the pointer to decide, so it is not asked."""
    speaking = [True]
    w, clock, cursor, _speaking = _widget(qapp, monkeypatch, speaking=speaking)
    try:
        before = cursor.calls
        clock.ms += 50
        w._w._life_tick()
        assert cursor.calls == before
        speaking[0] = False
        clock.ms += 50
        w._w._life_tick()
        assert cursor.calls == before + 1
    finally:
        _close(qapp, w)


# -- the real pointer, not a stand-in ----------------------------------------

def _spot_away_from(point, size):
    """A top-left for a size-sized box that certainly does not cover point,
    inside the screen the pointer is on."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication
    screen = QApplication.screenAt(point) or QApplication.primaryScreen()
    area = screen.availableGeometry()
    for corner in (area.topLeft(),
                   QPoint(area.right() - size.width(), area.top()),
                   QPoint(area.left(), area.bottom() - size.height()),
                   QPoint(area.right() - size.width(), area.bottom() - size.height())):
        if not QRect(corner, size).contains(point):
            return corner
    pytest.skip("no corner of this screen is clear of the pointer")


def test_the_real_cursor_and_the_window_rect_are_in_the_same_space(qapp):
    """cursor_screen_pos() and frameGeometry() must be the same coordinate
    space or the rectangle test is nonsense. Proved against the pointer where
    it actually is -- the BOX is moved onto and off the pointer, never the
    other way round, because moving the owner's mouse from a test is not on.
    """
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication

    point = st.cursor_screen_pos()
    assert point == QCursor.pos()
    assert any(s.geometry().contains(point) for s in QApplication.screens()), point

    clock = _Clock()
    w = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, ())
    w._w._now = lambda: clock.ms
    w._w._reduced_motion = True
    try:
        w.show_overlay()
        w._w._life_timer.stop()
        qapp.processEvents()
        _tick_to(w, clock, IDLE_AT_MS)
        assert w._w._faded and w._w._click_through

        # Park the box so its rect is centred on the real pointer. No draft
        # and no hint are showing, so nothing on the wake path calls
        # _position() and moves it back to the bottom of the screen.
        size = w._w.frameGeometry().size()
        w._w.move(point.x() - size.width() // 2, point.y() - size.height() // 2)
        qapp.processEvents()
        assert w._w.frameGeometry().contains(point)
        assert w._w._cursor_inside() is True

        clock.ms += 50
        w._w._life_tick()
        qapp.processEvents()
        assert not w._w._faded and not window_is_click_through(w._w)

        # And off it again: somewhere on the desktop the pointer is not.
        w._w.move(_spot_away_from(point, size))
        qapp.processEvents()
        assert not w._w.frameGeometry().contains(point)
        assert w._w._cursor_inside() is False
        _tick_to(w, clock, clock.ms + 5100 + st.IDLE_FADE_MS)
        assert w._w._faded and window_is_click_through(w._w)
    finally:
        _close(qapp, w)
