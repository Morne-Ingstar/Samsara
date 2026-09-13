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
