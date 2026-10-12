"""548: the Compact picture on the placement page paints; a failed paint draws nothing."""
import ast
import inspect
import logging
import sys
import textwrap

import pytest
from PySide6.QtCore import QRectF, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage, QPainter

from samsara.log import describe_text
from samsara.ui.widgets.indicator_demo import IndicatorDemo, compact_stand_in
from samsara.ui.widgets.indicator_place import IndicatorPlacePicker, POSITIONS

# What the demo binds itself, because it draws the demonstration's glyph and fonts.
BOUND_BY_THE_DEMO = {"_glyph_mark", "_font", "_chip_font"}
PAINT_METHODS = ("_resolve_colors", "_paint_pill", "_paint_chip", "_font", "_chip_font")


def _self_reads(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and isinstance(node.ctx, ast.Load)}


def test_stand_in_carries_every_field_the_pill_painters_read():
    # 548: _agent_waiting arrived on the real pill (539) and the stand-in did not follow.
    from samsara.ui.listening_indicator import ListeningIndicator
    provided = set(vars(compact_stand_in())) | BOUND_BY_THE_DEMO
    for name in PAINT_METHODS:
        missing = _self_reads(getattr(ListeningIndicator, name)) - provided
        assert not missing, f"{name} reads {sorted(missing)}, which the Compact stand-in lacks"


def test_stand_in_resolves_colours_in_every_demonstration_state():
    from samsara.ui.listening_indicator import ListeningIndicator
    for kwargs in ({}, {"thinking": True}, {"command_mode": True}, {"listening": True},
                   {"command_mode": True, "listening": True}):
        result = ListeningIndicator._resolve_colors(compact_stand_in(mode_text="Hold", **kwargs))
        assert len(result) == 4


class _QtMessages:
    def __init__(self):
        self.lines = []

    def __enter__(self):
        self.previous = qInstallMessageHandler(lambda kind, context, text: self.lines.append(text))
        return self

    def __exit__(self, *exc):
        qInstallMessageHandler(self.previous)


@pytest.fixture
def hook_errors(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: seen.append(value))
    return seen


def _paint_sketch(demo, beat):
    demo.set_frame(beat)
    image = QImage(640, 370, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    demo.desktop.paint_indicator(painter, QRectF(10, 60, 200, 100))
    painter.end()
    return image


def _anything_drawn(image):
    return any(image.pixelColor(x, y).alpha() for x in range(0, 640, 3) for y in range(0, 370, 3))


@pytest.mark.parametrize("display", ["compact", "full"])
def test_indicator_draws_on_every_beat(qapp, display):
    demo = IndicatorDemo(reduced_motion=True, display=display)
    demo.resize(640, 370)
    for beat in range(5):
        assert _anything_drawn(_paint_sketch(demo, beat)), f"{display} beat {beat} drew nothing"
    demo.deleteLater()


@pytest.mark.parametrize("display", ["compact", "full"])
@pytest.mark.parametrize("position", POSITIONS)
def test_placement_page_paints_without_an_error(qapp, hook_errors, display, position):
    picker = IndicatorPlacePicker(position, display=display, reduced_motion=True)
    picker.resize(620, 400)
    picker.show()
    with _QtMessages() as qt:
        picker.grab()
        picker.desktop.grab()
        for button in picker.targets.values():
            button.grab()
    picker.hide()
    picker.deleteLater()
    assert hook_errors == []
    assert [line for line in qt.lines if "Painter ended" in line or "endPaint" in line] == []


def _break_resolver(monkeypatch):
    from samsara.ui.listening_indicator import ListeningIndicator

    def broken(self):
        raise AttributeError("'SimpleNamespace' object has no attribute '_probe_548'")
    monkeypatch.setattr(ListeningIndicator, "_resolve_colors", broken)


def test_failed_paint_draws_nothing_and_logs_once(qapp, monkeypatch, caplog):
    _break_resolver(monkeypatch)
    demo = IndicatorDemo(reduced_motion=True, display="compact")
    demo.resize(640, 370)
    caplog.set_level(logging.DEBUG)
    with _QtMessages() as qt:
        images = [_paint_sketch(demo, beat) for beat in (0, 1, 2, 3, 0, 1)]
    for image in images:
        assert not _anything_drawn(image)
    records = [r for r in caplog.records if r.name == "Samsara.ui.indicator_demo"]
    assert len(records) == 1                       # once per widget, not once per frame
    assert "_probe_548" in caplog.text             # and it says which attribute
    assert qt.lines == []                          # no saved state left over
    other = IndicatorDemo(reduced_motion=True, display="compact")
    other.resize(640, 370)
    _paint_sketch(other, 0)
    assert len([r for r in caplog.records if r.name == "Samsara.ui.indicator_demo"]) == 2
    demo.deleteLater()
    other.deleteLater()


def test_failed_paint_on_the_page_leaves_no_open_painter(qapp, monkeypatch, hook_errors):
    _break_resolver(monkeypatch)
    picker = IndicatorPlacePicker("bottom-left", display="compact", reduced_motion=True)
    picker.resize(620, 400)
    picker.show()
    with _QtMessages() as qt:
        for _ in range(3):
            picker.grab()
    picker.hide()
    picker.deleteLater()
    assert hook_errors == []
    assert qt.lines == []


# ---- item 3: which attribute, never which words --------------------------------

def _raised(source):
    try:
        exec(source, {})
    except BaseException as error:
        return error
    raise AssertionError("did not raise")


SPEECH = "open the zebra door"
TABLE = [
    # (label, exception, printed)
    ("AttributeError on a namespace (this bug)", AttributeError("'types.SimpleNamespace' object has no attribute '_agent_waiting'"), True),
    ("AttributeError, None.x", _raised("None.x"), True),
    ("AttributeError, str has no attribute", _raised("'a'.nope"), True),
    ("AttributeError, module attribute", _raised("import os; os.nope"), True),
    ("AttributeError, class attribute", _raised("class K: pass\nK.nope"), True),
    ("AttributeError, read-only", _raised("(1).real = 2"), True),
    ("AttributeError carrying speech", AttributeError(SPEECH), False),
    ("AttributeError with speech as the attribute", AttributeError(f"'str' object has no attribute '{SPEECH}'"), False),
    ("NameError, undefined name", _raised("nope_548"), True),
    ("UnboundLocalError", _raised("def f():\n    x = y\n    y = 1\nf()"), True),
    ("ModuleNotFoundError", _raised("import nope_548_module"), True),
    ("ImportError, cannot import name (path dropped)", _raised("from os import nope_548"), "cannot import name 'nope_548' from 'os'"),
    ("TypeError, not callable", _raised("(1)()"), True),
    ("TypeError, not subscriptable", _raised("(1)[0]"), True),
    ("TypeError, wrong argument count", _raised("def f(a):\n    pass\nf(1, 2)"), True),
    ("TypeError, unexpected keyword", _raised("def f(a):\n    pass\nf(a=1, zz=2)"), True),
    ("TypeError, missing argument", _raised("def f(a, b):\n    pass\nf()"), True),
    ("TypeError, operands", _raised("1 + None"), True),
    ("TypeError carrying speech", TypeError(f"cannot join {SPEECH}"), False),
    ("KeyError, a key can be a dictated word", _raised("{}['zebra']"), False),
    ("KeyError, repo raise site shape (path[2])", KeyError(SPEECH), False),
    ("ValueError, repo raise site (Hermes effort)", ValueError("Hermes effort must be one of the listed levels."), False),
    ("ValueError carrying speech", ValueError(SPEECH), False),
    ("OSError keeps errno and file name only", _raised("open(r'C:\\Users\\someone\\private_548_dir\\x.txt')"), False),
]


@pytest.mark.parametrize("label,error,printed", TABLE, ids=[row[0] for row in TABLE])
def test_exception_message_table(label, error, printed):
    described = describe_text(error)
    if isinstance(printed, str):
        assert described == f"{type(error).__name__}: {printed}", label
        assert "(" not in described, label
    elif printed:
        assert described == f"{type(error).__name__}: {error}", label
    else:
        assert str(error) not in described, label
        assert "zebra" not in described and "someone" not in described, label
        assert " chars" in described, label
