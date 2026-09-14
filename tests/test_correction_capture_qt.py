"""40: the correction-capture window stays open after being shown.

Before, CorrectionCaptureQt._open_window held the window only in a local
(WA_DeleteOnClose, no parent, no reference) and it was collected the moment
the function returned -- the owner's window that flashed and vanished on
every press. The wrapper now holds it; these tests spin the event loop and a
garbage-collection pass and assert the window is still there.
"""

import gc
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from samsara.ui import correction_capture_qt as cc


def _app():
    return types.SimpleNamespace(config={'correction_capture': {}}, history_store=None)


def _alive():
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, cc.CorrectionCaptureWindow)]


@pytest.fixture
def opener(qapp):
    o = cc.CorrectionCaptureQt(_app())
    yield o
    if o.window is not None:
        o.window.close()
    qapp.processEvents()


def test_window_stays_open_after_the_event_loop_spins(opener, qapp):
    opener._open_window("hello world")
    gc.collect()
    for _ in range(20):
        qapp.processEvents()
    assert opener.window is not None and opener.window.isVisible()
    assert [w.isVisible() for w in _alive()] == [True]


def test_open_posts_the_construction_and_the_wrapper_holds_it(opener, qapp, monkeypatch):
    from samsara.ui import qt_runtime

    posted = []
    monkeypatch.setattr(qt_runtime, "post", lambda fn: posted.append(fn))
    opener.open("hello world")
    assert opener.window is None and len(posted) == 1
    posted[0]()
    gc.collect()
    qapp.processEvents()
    assert opener.window is not None and opener.window.isVisible()


def test_second_press_replaces_the_first_window(opener, qapp):
    first = opener._open_window("one")
    second = opener._open_window("two")
    gc.collect()
    for _ in range(20):
        qapp.processEvents()
    assert opener.window is second and second.isVisible()
    assert first is not second and first not in _alive()


def test_closing_releases_the_reference(opener, qapp):
    win = opener._open_window("hello")
    win.close()
    for _ in range(20):
        qapp.processEvents()
    assert opener.window is None


def test_no_last_dictation_says_so_in_the_window(opener, qapp):
    win = opener._open_window("")
    qapp.processEvents()
    assert win.isVisible()
    assert win._text_edit.toPlainText() == ""
    assert win._text_edit.placeholderText() == "No recent dictation found."
