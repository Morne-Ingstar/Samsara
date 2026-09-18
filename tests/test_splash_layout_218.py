"""Astra 216 geometry, timing, and non-colour splash cues."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMargins, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath
from PySide6.QtWidgets import QApplication
from samsara.ui.splash_qt import (_DETAIL_DELAY_MS, _REASSURANCE_DELAY_MS,
                                  _SIGNATURE_TEXT, _SplashWidget)
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
        _ink_rect(theme.qfont(28 if widget._compact() else theme.TYPE_HERO, "Segoe UI Variable Display", QFont.Weight.DemiBold), layout["word"], "Samsara", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_BODY, italic=True), layout["tag"], "De-articulating Splines.", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_HEADING, weight=QFont.Weight.DemiBold), layout["status"], "Starting Samsara", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_MIN, weight=QFont.Weight.DemiBold), layout["strip"], "Hotkey ✓", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_BODY), layout["support"], "Still starting — preparing voice services.", Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
        _ink_rect(theme.qfont(theme.TYPE_MIN), layout["credit"], _SIGNATURE_TEXT, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom),
    ]


@pytest.mark.parametrize("size", [(646, 366), (442, 250)])
def test_measured_text_stays_clear_of_ring_and_inside_panel(app, size):
    widget = _SplashWidget(clock_ms=lambda: 0)
    widget.setFixedSize(*size)
    panel = QPainterPath()
    panel.addRoundedRect(widget.rect().marginsRemoved(QMargins(7, 7, 7, 7)), 38, 38)
    ring = widget._layout()["ring"].adjusted(-5, -5, 5, 5)
    rects = _text_rects(widget)
    assert all(panel.contains(rect) for rect in rects)
    assert all(not rect.intersects(ring) for rect in rects)
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
