"""Astra 216 geometry, timing, and non-colour splash cues."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtWidgets import QApplication
from samsara.ui.splash_qt import (_DETAIL_DELAY_MS, _REASSURANCE_DELAY_MS, _SplashWidget)
from samsara.ui import theme


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _ink_rect(font, box, text, alignment):
    ink = QFontMetricsF(font).tightBoundingRect(text)
    x = box.left() if alignment & Qt.AlignmentFlag.AlignLeft else box.right() - ink.width()
    if alignment & Qt.AlignmentFlag.AlignHCenter:
        x = box.center().x() - ink.width() / 2
    y = box.top() if alignment & Qt.AlignmentFlag.AlignTop else box.bottom() - ink.height()
    if alignment & Qt.AlignmentFlag.AlignVCenter:
        y = box.center().y() - ink.height() / 2
    return ink.translated(x - ink.left(), y - ink.top())


def _text_rects(widget):
    layout = widget._layout()
    return [
        _ink_rect(theme.qfont(theme.TYPE_TITLE if widget._compact() else theme.TYPE_DISPLAY, "Segoe UI Variable Display", QFont.Weight.DemiBold), layout["word"], "Samsara", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_BODY), layout["tag"], "De-articulating Splines.", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_BODY, weight=QFont.Weight.DemiBold), layout["status"], "Starting Samsara", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
    ]


@pytest.mark.parametrize("size", [(440, 270), (340, 220)])
def test_measured_text_stays_clear_of_mark_and_inside_window(app, size):
    widget = _SplashWidget(clock_ms=lambda: 0)
    widget.setFixedSize(*size)
    mark = widget._layout()["mark"]
    rects = _text_rects(widget)
    assert all(widget.rect().contains(rect.toRect()) for rect in rects)
    assert all(not rect.intersects(mark) for rect in rects)
    assert all(not first.intersects(second) for index, first in enumerate(rects) for second in rects[index + 1:])
    widget.close()


def test_detail_and_reassurance_use_injected_clock(app):
    now = [0]
    widget = _SplashWidget(clock_ms=lambda: now[0])
    widget._set_detail("Loading model files")
    assert widget._support_text() == ""
    now[0] = _DETAIL_DELAY_MS
    assert widget._support_text() == "Loading model files"
    now[0] = _REASSURANCE_DELAY_MS
    assert widget._support_text() == "Still starting — Loading model files"
    widget._set_detail("")
    assert "Still starting" in widget._support_text()
    widget.close()


def test_warning_reveals_detail_without_waiting(app):
    widget = _SplashWidget(clock_ms=lambda: 0)
    widget._set_detail("Wake microphone is offline")
    widget._set_ready_ladder((("hotkey: ready", "ready"), ("wake: offline", "offline"), ("Ava: checking", "checking")))
    assert widget._support_text() == "Wake microphone is offline"
    widget.close()


def test_capability_states_have_non_colour_glyphs(app):
    widget = _SplashWidget(clock_ms=lambda: 0)
    assert "✓" in widget._segment("hotkey: ready", "ready")[0]
    assert "◌" in widget._segment("wake: loading", "loading")[0]
    assert "×" in widget._segment("Ava: offline", "offline")[0]
    assert "Ready" not in widget._segment("hotkey: ready", "ready")[0]
    assert "Unavailable" in widget._segment("Ava: offline", "offline", detailed=True)[0]
    widget.close()
