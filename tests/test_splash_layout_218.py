"""Astra 216 geometry, timing, and non-colour splash cues."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from samsara.ui.splash_qt import (_DETAIL_DELAY_MS, _REASSURANCE_DELAY_MS,
                                  _SplashWidget, _TRAY_LOGICAL)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_normal_layout_keeps_text_out_of_ring_and_reserves_tray(app):
    widget = _SplashWidget(clock_ms=lambda: 0)
    layout = widget._layout()
    assert not layout["ring"].intersects(layout["status"])
    assert not layout["ring"].intersects(layout["strip"])
    assert _TRAY_LOGICAL == (24.0, 322.0, 712.0, 94.0)
    widget.close()


def test_compact_layout_is_readable_at_442_by_250(app):
    widget = _SplashWidget(clock_ms=lambda: 0)
    widget.setFixedSize(442, 250)
    layout = widget._layout()
    assert widget._compact()
    assert layout["word"].top() == 8 and layout["credit"].bottom() <= 244
    assert not layout["ring"].intersects(layout["status"])
    widget.close()


def test_detail_and_reassurance_use_injected_clock(app):
    now = [0]
    widget = _SplashWidget(clock_ms=lambda: now[0])
    widget._set_detail("Loading model files")
    assert widget._support_text() == ""
    now[0] = _DETAIL_DELAY_MS
    assert widget._support_text() == "Loading model files"
    widget._set_detail("")
    now[0] = _REASSURANCE_DELAY_MS
    assert "Still starting" in widget._support_text()
    widget.close()


def test_capability_states_have_non_colour_glyphs(app):
    widget = _SplashWidget(clock_ms=lambda: 0)
    assert "✓" in widget._segment("hotkey: ready", "ready")[0]
    assert "◌" in widget._segment("wake: loading", "loading")[0]
    assert "×" in widget._segment("Ava: offline", "offline")[0]
    assert "Ready" not in widget._segment("hotkey: ready", "ready")[0]
    assert "Unavailable" in widget._segment("Ava: offline", "offline", detailed=True)[0]
    widget.close()
