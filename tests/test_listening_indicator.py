"""ListeningIndicator: placement contract, and that the outcome chip never
moves the pill.

The chip (queue 41) made the widget taller and sometimes wider, and required
reworking _apply_static_position. The contract that must survive: the PILL
lands on exactly the same screen pixels it always did, whether or not a chip
is showing, for every preset corner and for a custom drag position. The chip
grows away from the edge the pill is anchored to.

Offscreen Qt; the session-scoped `qapp` fixture from conftest.py.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from samsara.ui.listening_indicator import (
    VALID_POSITIONS,
    ListeningIndicator,
    _CHIP_GAP,
    _CHIP_H,
    _PILL_H,
)


def _pump(qapp, ms=30):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


@pytest.fixture
def indicator(qapp):
    widget = ListeningIndicator()
    yield widget
    widget.destroy()


def _pill_screen_rect(widget):
    """Where the pill is actually drawn, in global screen coordinates."""
    _, _, pill_rect, _ = widget._layout()
    return (widget.x() + int(round(pill_rect.x())),
            widget.y() + int(round(pill_rect.y())),
            int(round(pill_rect.width())),
            int(round(pill_rect.height())))


# ---------------------------------------------------------------------------
# The pill must not move when a chip appears
# ---------------------------------------------------------------------------

class TestPillStaysPut:
    @pytest.mark.parametrize("corner", VALID_POSITIONS)
    def test_preset_corners(self, qapp, indicator, corner):
        indicator.set_position(corner)
        indicator.show()
        _pump(qapp)
        before = _pill_screen_rect(indicator)

        indicator.show_outcome("refused: focus lock", "warning", ttl_ms=None)
        _pump(qapp)
        after = _pill_screen_rect(indicator)

        assert after == before, f"pill moved at {corner}: {before} -> {after}"

    @pytest.mark.parametrize("cx,cy", [(0.5, 0.2), (0.5, 0.8), (0.1, 0.5), (0.9, 0.5)])
    def test_custom_positions(self, qapp, indicator, cx, cy):
        from PySide6.QtWidgets import QApplication

        indicator.set_custom_position(QApplication.primaryScreen().name(), cx, cy)
        indicator.show()
        _pump(qapp)
        before = _pill_screen_rect(indicator)

        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _pump(qapp)

        assert _pill_screen_rect(indicator) == before

    def test_pill_returns_to_its_size_after_the_chip_clears(self, qapp, indicator):
        indicator.set_position("bottom-center")
        indicator.show()
        _pump(qapp)
        size = (indicator.width(), indicator.height())

        indicator.show_outcome("MISS", "error", ttl_ms=None)
        indicator.clear_outcome()
        _pump(qapp)

        assert (indicator.width(), indicator.height()) == size


# ---------------------------------------------------------------------------
# Chip grows away from the anchored edge
# ---------------------------------------------------------------------------

class TestChipDirection:
    @pytest.mark.parametrize("corner", ["top-left", "top-center", "top-right"])
    def test_below_a_top_anchored_pill(self, indicator, corner):
        indicator.set_position(corner)
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _, _, pill, chip = indicator._layout()
        assert chip.y() > pill.y()

    @pytest.mark.parametrize("corner", ["bottom-left", "bottom-center", "bottom-right"])
    def test_above_a_bottom_anchored_pill(self, indicator, corner):
        indicator.set_position(corner)
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _, _, pill, chip = indicator._layout()
        assert chip.y() < pill.y()

    def test_widget_height_is_pill_gap_chip(self, indicator):
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        total_w, total_h, _, _ = indicator._layout()
        assert total_h == _PILL_H + _CHIP_GAP + _CHIP_H

    def test_a_wide_chip_widens_the_widget_and_centres_the_pill(self, indicator):
        indicator.show()
        indicator.show_outcome("refused: nothing staged", "warning", ttl_ms=None)
        total_w, _, pill, chip = indicator._layout()
        assert total_w >= chip.width()
        assert abs((pill.x() + pill.width() / 2) - total_w / 2) <= 1


# ---------------------------------------------------------------------------
# Existing indicator contract (unchanged by the chip)
# ---------------------------------------------------------------------------

class TestExistingContract:
    def test_invalid_corner_falls_back_to_bottom_center(self, indicator):
        indicator.set_position("nowhere")
        assert indicator._corner == "bottom-center"

    @pytest.mark.parametrize("bad", [None, "x", float("nan"), float("inf")])
    def test_malformed_custom_position_is_ignored(self, indicator, bad):
        indicator.set_custom_position("screen", bad, 0.5)
        assert indicator._custom_position is None

    def test_custom_position_is_clamped(self, indicator):
        indicator.set_custom_position("screen", 5.0, -3.0)
        assert indicator._custom_position["cx"] == 1.0
        assert indicator._custom_position["cy"] == 0.0

    def test_session_mode_label_wins(self, indicator):
        indicator.set_session_mode("DICTATE", "#00ff00")
        assert indicator._resolve_colors()[2] == "DICTATE"

    def test_hide_stops_timers(self, qapp, indicator):
        indicator.set_listening(True)
        indicator.show()
        indicator.hide()
        assert not indicator._pulse_timer.isActive()
        assert not indicator.isVisible()

    def test_hide_clears_a_transient_chip(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        indicator.hide()
        assert indicator._active_chip() is None
        assert not indicator.isVisible()


# ---------------------------------------------------------------------------
# Move mode dominates the chip
# ---------------------------------------------------------------------------

class TestMoveMode:
    def test_chip_is_not_drawn_while_dragging(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        indicator.enter_move_mode()
        try:
            assert indicator._active_chip() is None
            _, _, pill, chip = indicator._layout()
            assert chip is None
        finally:
            indicator.exit_move_mode(cancel=True)

    def test_chip_returns_after_move_mode(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("staged", "pending", ttl_ms=None)
        indicator.enter_move_mode()
        indicator.exit_move_mode(cancel=True)
        assert indicator._active_chip() == ("staged", "pending")


# ---------------------------------------------------------------------------
# 09b-2: the state glyph is the shared mark; the palette is theme tokens
# ---------------------------------------------------------------------------

class TestStateGlyphIsTheMark:
    def _painted_marks(self, qapp, indicator, monkeypatch):
        from PySide6.QtGui import QImage
        import samsara.ui.listening_indicator as li

        calls = []
        real = li.paint_mark
        monkeypatch.setattr(li, "paint_mark",
                            lambda painter, rect, capture, eye, rotation=0.0, opacity=1.0:
                            calls.append((capture, eye)) or real(painter, rect, capture, eye, rotation, opacity))
        indicator.show()
        _pump(qapp)
        image = QImage(indicator.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        indicator.render(image)
        return calls

    @pytest.mark.parametrize("setup, expected", [
        (lambda w: None,                         ("idle", "off")),
        (lambda w: w.set_snoozed(True),          ("idle", "asleep")),
        (lambda w: w.set_listening(True),        ("listening", "off")),
        (lambda w: w.set_command_mode(True),     ("listening", "off")),
        (lambda w: w.set_thinking(True),         ("ava", "off")),
        (lambda w: w.set_session_mode("AVA", "#a78bfa"), ("ava", "off")),
    ])
    def test_each_state_paints_the_shared_mark(self, qapp, indicator, monkeypatch, setup, expected):
        setup(indicator)
        calls = self._painted_marks(qapp, indicator, monkeypatch)
        assert calls and set(calls) == {expected}   # one mark per paint pass

    def test_wake_flash_paints_the_heard_frame(self, qapp, indicator, monkeypatch):
        indicator.show()
        indicator.flash_wake()
        assert ("listening", "heard") in self._painted_marks(qapp, indicator, monkeypatch)

    def test_armed_paints_the_open_eye(self, qapp, indicator, monkeypatch):
        indicator.set_wake_armed(True)
        assert set(self._painted_marks(qapp, indicator, monkeypatch)) == {("listening", "armed")}

    def test_palette_is_theme_tokens(self):
        from samsara.ui import theme
        import samsara.ui.listening_indicator as li

        allowed = {theme.ACCENT, theme.ACCENT_HOVER, theme.BG1, theme.ICON_IDLE, theme.AVA,
                   theme.SUCCESS, theme.ERROR}
        mixes = {theme._mix(token, theme.BG0, t)
                 for token in (theme.ACCENT, theme.SUCCESS, theme.ERROR, theme.AVA)
                 for t in (0.62, 0.70, 0.80)}
        for name in ("_TEAL", "_TEAL_DIM", "_TEAL_BRIGHT", "_IDLE_BG", "_IDLE_FG", "_LISTENING_FG",
                     "_SNOOZE_BG", "_SNOOZE_FG", "_CMD_BG", "_CMD_FG", "_CMD_ACTIVE_BG",
                     "_CMD_ACTIVE_FG", "_FLASH_SUCCESS_BG", "_FLASH_SUCCESS_FG", "_FLASH_ERROR_BG",
                     "_FLASH_ERROR_FG", "_VISION_BG", "_VISION_BG_BRIGHT", "_VISION_FG"):
            assert getattr(li, name) in allowed | mixes, name


# ---------------------------------------------------------------------------
# 09b3: idle life -- blink and glance
# ---------------------------------------------------------------------------

class TestIdleLife:
    @pytest.fixture
    def armed(self, qapp, indicator, monkeypatch):
        import samsara.ui.listening_indicator as li

        monkeypatch.setattr(li, "_reduced_motion", lambda: False)
        clock = {"ms": 1000}
        monkeypatch.setattr(indicator, "_now_ms", lambda: clock["ms"])
        indicator.show()
        _pump(qapp)
        indicator.set_wake_armed(True)
        return indicator, clock, li

    def test_armed_schedules_blink_at_random_intervals_and_no_glance(self, armed):
        """19: armed turns, so there is no rest pose to glance from."""
        indicator, _clock, li = armed
        assert indicator._blink_timer.isActive() and not indicator._glance_timer.isActive()
        lo, hi = li._BLINK_INTERVAL_S
        assert lo * 1000 <= indicator._blink_timer.interval() <= hi * 1000
        indicator._start_glance()
        assert indicator._glance_started_ms is None
        assert not indicator._idle_frame_timer.isActive()      # nothing runs between events

    def test_blink_closes_the_lid_for_140ms_then_rests(self, armed):
        indicator, clock, li = armed
        indicator._start_blink()
        assert indicator._glyph_mark()[1] == "asleep"
        clock["ms"] += li._BLINK_MS
        indicator._idle_frame()
        assert indicator._glyph_mark() == ("listening", "armed", indicator._spin("armed"), 1.0)
        assert indicator._blink_timer.isActive()                  # next one rescheduled
        assert li._BLINK_MS < 1000

    def test_glance_eases_out_and_returns_to_rest_under_a_second(self, armed, monkeypatch):
        indicator, clock, li = armed
        # The tilt itself, on a ring that is not turning (no live state is).
        monkeypatch.setattr(indicator, "_spinning", lambda: False)
        monkeypatch.setattr(indicator, "_capture_pace", lambda: None)
        indicator._start_glance()
        clock["ms"] += li._GLANCE_MS // 2
        assert indicator._glyph_mark()[2] == pytest.approx(li._GLANCE_DEG, abs=0.5)
        clock["ms"] += li._GLANCE_MS // 2
        indicator._idle_frame()
        assert indicator._glyph_mark()[2] == 0.0
        assert indicator._glyph_mark()[1] == "armed"               # the eye never turns or closes
        assert li._GLANCE_MS < 1000 and li._GLANCE_DEG < 360

    @pytest.mark.parametrize("setup", [
        lambda w: w.set_wake_armed(False),                         # off
        lambda w: w.set_snoozed(True),                             # asleep
        lambda w: w.show_outcome("REC", "live", ttl_ms=None),      # recording
        lambda w: w.flash_wake(),                                  # heard flash
    ])
    def test_never_in_off_asleep_recording_or_heard(self, armed, setup):
        indicator, _clock, _li = armed
        setup(indicator)
        assert not indicator._idle_allowed()
        assert not indicator._blink_timer.isActive() and not indicator._glance_timer.isActive()
        indicator._start_blink()
        indicator._start_glance()
        assert indicator._blink_started_ms is None and indicator._glance_started_ms is None

    def test_state_change_cancels_in_flight_idle_immediately(self, armed):
        indicator, _clock, _li = armed
        indicator._start_blink()
        indicator._start_glance()
        indicator.set_snoozed(True)
        assert indicator._blink_started_ms is None and indicator._glance_started_ms is None
        assert not indicator._idle_frame_timer.isActive()
        assert indicator._glyph_mark()[:3] == ("idle", "asleep", 0.0)

    def test_config_off_stops_blink_and_glance_but_not_spin(self, armed, qapp):
        indicator, clock, _li = armed
        indicator._start_blink()
        indicator.set_idle_animation(False)
        assert indicator._blink_started_ms is None
        assert not indicator._blink_timer.isActive() and not indicator._glance_timer.isActive()
        indicator.set_wake_armed(False)
        indicator.set_thinking(True)
        first = indicator._glyph_mark()[2]
        clock["ms"] += 600
        assert indicator._glyph_mark()[2] != first                 # thinking still spins
        assert indicator._pulse_timer.isActive()

    def test_thinking_spins_at_2_4_seconds_per_turn(self, armed):
        indicator, clock, _li = armed
        indicator.set_thinking(True)
        clock["ms"] = 0
        assert indicator._glyph_mark()[2] == pytest.approx(0.0)
        clock["ms"] = 600
        assert indicator._glyph_mark()[2] == pytest.approx(90.0)

    def test_armed_turns_once_per_3_seconds(self, armed):
        indicator, clock, _li = armed
        clock["ms"] = 0
        assert indicator._glyph_mark() == ("listening", "armed", pytest.approx(0.0), 1.0)
        clock["ms"] = 750
        assert indicator._glyph_mark()[2] == pytest.approx(90.0)
        assert indicator._pulse_timer.isActive()

    @pytest.mark.parametrize("setup", [
        lambda w: w.set_listening(True),
        lambda w: w.show_outcome("REC", "live", ttl_ms=None),
    ])
    def test_recording_turns_once_per_1_5_seconds_with_no_pulse(self, armed, setup):
        indicator, clock, _li = armed
        indicator.set_wake_armed(False)
        setup(indicator)
        clock["ms"] = 0
        first = indicator._glyph_mark()
        clock["ms"] = 375
        capture, _eye, rotation, opacity = indicator._glyph_mark()
        assert rotation == pytest.approx(90.0) and first[2] == pytest.approx(0.0)
        assert opacity == first[3] == 1.0
        assert indicator._pulse_timer.isActive()

    @pytest.mark.parametrize("setup", [
        lambda w: None,                                            # idle
        lambda w: w.set_snoozed(True),                             # asleep
    ])
    def test_rest_is_still(self, armed, setup):
        indicator, clock, _li = armed
        indicator.set_wake_armed(False)
        setup(indicator)
        marks = set()
        for ms in (0, 400, 900, 1700):
            clock["ms"] = ms
            marks.add(indicator._glyph_mark())
        assert len(marks) == 1 and next(iter(marks))[2:] == (0.0, 1.0)
        assert not indicator._spinning()

    def test_glyph_stays_20px_without_reflow(self):
        """19: 20 px is the heavy small drawing; 24 px would widen the dot
        reserve (reflowing every pill) and fall back to the thin drawing."""
        import samsara.ui.listening_indicator as li
        from samsara.ui import tray_qt

        assert li._GLYPH_PX == 20 and li._GLYPH_PX <= li._DOT_SPACE
        assert li._GLYPH_PX <= tray_qt._SMALL_MAX

    def test_reduced_motion_and_hidden_pause_idle(self, armed, monkeypatch):
        indicator, _clock, li = armed
        monkeypatch.setattr(li, "_reduced_motion", lambda: True)
        indicator._state_changed()
        assert not indicator._idle_allowed() and not indicator._blink_timer.isActive()
        monkeypatch.setattr(li, "_reduced_motion", lambda: False)
        indicator.hide()
        assert not indicator._idle_allowed() and not indicator._blink_timer.isActive()


# ---------------------------------------------------------------------------
# 42: arming the wake word is not recording
# ---------------------------------------------------------------------------

class TestArmedIsNotRecording:
    """Owner evidence: enabling wake word from the tray lit the pill as if the
    record hotkey were held, until a real capture's release cleared it. Root
    cause: DictationApp.start_wake_word_mode called set_listening(True)
    beside set_wake_armed(True). Here the REAL start/stop_wake_word_mode run
    against a real ListeningIndicator; the capture is the indicator call the
    hotkey capture path makes (set_listening True on press, False on release)."""

    @pytest.fixture
    def wired(self, qapp, indicator, monkeypatch):
        from unittest.mock import Mock

        import dictation
        import samsara.ui.listening_indicator as li

        monkeypatch.setattr(li, "_reduced_motion", lambda: False)
        monkeypatch.setattr(dictation.time, "sleep", lambda _s: None)
        app = Mock()
        app.model_loaded = True
        app._request_wake_models.return_value = True
        app.config = {"wake_word_config": {"phrase": "samsara"}}
        app.recording = False
        app.listening_indicator = indicator

        def set_app_state(**kw):
            for name, value in kw.items():
                setattr(app, name, value)

        app.set_app_state = set_app_state
        app._schedule_ui = lambda fn, *args: fn(*args)
        indicator.show()
        _pump(qapp)

        def arm():
            dictation.DictationApp.start_wake_word_mode(app)

        def disarm():
            dictation.DictationApp.stop_wake_word_mode(app)

        def press():                        # hotkey capture starts
            app.recording = True
            indicator.set_listening(True)

        def release():                      # hotkey capture ends
            app.recording = False
            indicator.set_listening(False)

        return indicator, app, arm, disarm, press, release

    @staticmethod
    def _assert_armed(indicator):
        import samsara.ui.listening_indicator as li

        assert indicator.display_state() == li.STATE_ARMED
        assert indicator.display_state() not in li.CAPTURE_STATES
        assert not indicator._listening
        # quiet pill: the idle colours at every pulse step, never the lit teal
        for step in range(li._PULSE_STEPS + 1):
            indicator._pulse_step = step
            bg, fg, _label, _dot = indicator._resolve_colors()
            assert (bg, fg) == (li._IDLE_BG, li._IDLE_FG)
        capture, eye, _rotation, opacity = indicator._glyph_mark()
        assert (capture, eye, opacity) == ("listening", "armed", 1.0)   # eye open
        assert indicator._capture_pace() == "armed"                     # slow turn, not recording

    @staticmethod
    def _assert_capture(indicator):
        import samsara.ui.listening_indicator as li

        assert indicator.display_state() in li.CAPTURE_STATES
        assert indicator._capture_pace() == "recording"
        indicator._pulse_step = li._PULSE_STEPS
        assert indicator._resolve_colors()[0] != li._IDLE_BG

    def test_after_arm_the_indicator_is_armed(self, wired):
        indicator, _app, arm, _disarm, _press, _release = wired
        arm()
        self._assert_armed(indicator)

    def test_after_disarm_the_indicator_is_idle(self, wired):
        indicator, _app, arm, disarm, _press, _release = wired
        arm()
        disarm()
        assert indicator.display_state() == "idle"
        assert not indicator._listening and not indicator._wake_armed
        assert indicator._capture_pace() is None

    def test_a_real_capture_shows_capture_styling_then_clears(self, wired):
        indicator, _app, _arm, _disarm, press, release = wired
        press()
        self._assert_capture(indicator)
        release()
        assert indicator.display_state() == "idle"

    def test_arm_capture_release_returns_to_armed(self, wired):
        indicator, _app, arm, _disarm, press, release = wired
        arm()
        self._assert_armed(indicator)
        press()
        self._assert_capture(indicator)           # capturing while armed is capturing
        assert indicator._glyph_mark()[1] == "armed"
        release()
        self._assert_armed(indicator)

    def test_disarm_during_a_hotkey_capture_leaves_the_capture_alone(self, wired):
        indicator, _app, arm, disarm, press, release = wired
        arm()
        press()
        disarm()
        self._assert_capture(indicator)
        release()
        assert indicator.display_state() == "idle"

    def test_disarm_clears_a_wake_owned_capture(self, wired):
        """Wake phrase heard / wake session set listening; stopping wake mode
        ends them, so nothing is left looking like capture."""
        indicator, _app, arm, disarm, _press, _release = wired
        arm()
        indicator.set_listening(True)             # wake phrase heard
        disarm()
        assert indicator.display_state() == "idle"

    def test_no_path_leaves_capture_styling_stuck(self, wired):
        import itertools

        import samsara.ui.listening_indicator as li

        indicator, app, arm, disarm, press, release = wired
        steps = {"arm": arm, "disarm": disarm, "press": press, "release": release}
        for order in itertools.permutations(steps, 4):
            for name in order:
                if name == "press" and app.recording:
                    continue
                if name == "release" and not app.recording:
                    continue
                steps[name]()
            if app.recording:
                release()
            assert indicator.display_state() not in li.CAPTURE_STATES, order
            disarm()

    def test_start_wake_word_mode_never_sets_listening(self):
        import inspect

        import dictation

        source = inspect.getsource(dictation.DictationApp.start_wake_word_mode)
        code = "\n".join(line.split("#")[0] for line in source.splitlines())
        assert "set_wake_armed, True" in code
        assert "set_listening" not in code
