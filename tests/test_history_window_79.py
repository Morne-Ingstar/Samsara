"""Queue 79: the History window's three reported defects, clipping, plain-text
rendering, contrast, and the recovery-first behaviour.

Never touches the owner's data: every store is a tmp SQLite file, QSettings is
replaced with an in-memory fake, and the clipboard is a fake (the running app
and the owner both use the real one). Does not import dictation.
"""

import io
import re
import sys
import time
import tokenize
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.ui import history_view as hv
from samsara.ui import theme


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeSettings:
    values = {}

    def __init__(self, *_args):
        pass

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


class _FakeClipboard:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    _FakeSettings.values = {}
    monkeypatch.setattr(hv, "QSettings", _FakeSettings)
    clipboard = _FakeClipboard()
    monkeypatch.setattr(hv.QApplication, "clipboard", staticmethod(lambda: clipboard))
    return clipboard


def _pump(app, ms=300):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _pump_until(app, condition, timeout_ms=4000):
    end = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


TRICKY = "Use the <b>bold</b> tag & it's caf\u00e9 \u2014 na\u00efve \u201cquotes\u201d &#x27; done"


def _seed(tmp_path, name="h.db"):
    mgr = HistoryManager(db_path=str(tmp_path / name))
    now = datetime.now()
    spec = [
        # minutes ago, raw, display, mode, status, entry_type, app_context
        (60 * 24 * 10, "old entry", "Old entry.", "hold", "success", "dictation", "Notepad"),
        (300, "", "(no speech detected)", "hold", "empty", "failed", "Slack"),
        (280, "", "(no speech detected)", "wake", "empty", "failed", "Slack"),
        (240, "snap left", "snap left", "command", "success", "command", "Chrome"),
        (200, "please add cooper netties", "Please add Kubernetes.", "hold", "success", "dictation",
         "Settings"),
        (150, TRICKY, TRICKY, "hold", "success", "dictation", "index.html - Code"),
        (60, "send the invoice", "Send the invoice?", "hold", "failed", "dictation", "PowerShell"),
        (30, "a long one " * 40, ("A long one " * 40).strip(), "dictate", "success", "dictation",
         "Release plan - Google Docs - Chrome"),
    ]
    ids = {}
    for minutes, raw, disp, mode, status, etype, ctx in sorted(spec, key=lambda s: -s[0]):
        rid = mgr.add(raw, display_text=disp, app_context=ctx, duration_ms=2500, mode=mode,
                      status=status, entry_type=etype)
        mgr._conn.execute("UPDATE history SET timestamp=? WHERE id=?",
                          ((now - timedelta(minutes=minutes)).isoformat(), rid))
        ids[disp] = rid
    mgr._conn.commit()
    return mgr, HistoryStore(mgr), ids


def _find(root, *types_):
    """findChildren for several widget types (PySide6 takes one type)."""
    found = []
    for type_ in types_:
        found.extend(root.findChildren(type_))
    return found


def _show(widget, w, h):
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.resize(w, h)
    widget.show()


def _rows(view):
    return [view._list.itemWidget(view._list.item(i)) for i in range(view._list.count())
            if isinstance(view._list.itemWidget(view._list.item(i)), hv._HistoryRow)]


@pytest.fixture
def main_window(qapp, tmp_path):
    from samsara.ui import main_window_qt
    mgr, store, ids = _seed(tmp_path)
    app = types.SimpleNamespace(
        config={"mode": "toggle", "wake_word_enabled": False, "microphone": None},
        available_mics=[], recording=False, continuous_active=False, wake_word_active=False,
        snoozed=False, history=[], history_store=store, history_db=mgr,
    )
    win = main_window_qt._MainWindow(app)
    win._poll_timer.stop()
    _show(win, 1024, 700)
    win._activate("History")
    view = win._panel_cache["History"]
    _pump_until(qapp, lambda: len(view._rows_by_item_id) > 0)
    _pump(qapp, 150)
    yield win, view, app, ids
    win._poll_timer.stop()
    win._header_mark.stop()
    win.hide()
    win.deleteLater()
    mgr.close()


# ---------------------------------------------------------------------------
# Defect 1: "ready" badge painted a black box over the header divider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["ready", "listening", "recording", "snoozed"])
def test_header_badge_never_paints_over_the_header_divider(qapp, main_window, state):
    win, _view, app, _ids = main_window
    app.recording = state == "recording"
    app.wake_word_active = state == "listening"
    app.snoozed = False
    win._refresh_status()
    badge = win._badge
    if state == "snoozed":
        # The badge is hidden while snoozed; its sheet must still be safe.
        app.snoozed = True
        win._refresh_status()
        assert "background: transparent" in badge.styleSheet()
        return
    _pump(qapp, 50)
    assert badge.text() == state
    assert "background: transparent" in badge.styleSheet()
    header = badge.parentWidget()
    image = header.grab().toImage()
    y = header.height() - 1                           # the 1 px BORDER line
    under_badge = image.pixelColor(badge.geometry().center().x(), y).name()
    elsewhere = image.pixelColor(header.width() // 2, y).name()
    assert under_badge == elsewhere


def test_ready_badge_uses_the_secondary_text_token(qapp, main_window):
    win, _view, _app, _ids = main_window
    win._refresh_status()
    assert f"color: {theme.TEXT_SECONDARY};" in win._badge.styleSheet()


# ---------------------------------------------------------------------------
# Defect 2: "7 entries loaded" sat on a black strip
# ---------------------------------------------------------------------------

def _pixel_in_view(view, widget, x, y):
    point = widget.mapTo(view, widget.rect().topLeft())
    return view.grab().toImage().pixelColor(point.x() + x, point.y() + y).name()


def test_status_line_has_no_background_strip(qapp, main_window):
    _win, view, _app, _ids = main_window
    status = view._status_lbl
    assert status.text().startswith(f"{len(view._rows_by_item_id)} entries")
    bg = theme.BG1.lower()
    assert _pixel_in_view(view, status, 1, 1) == bg
    assert _pixel_in_view(view, status, status.width() - 2, status.height() - 2) == bg


def test_no_label_or_checkbox_in_the_view_paints_its_own_box(qapp, main_window):
    _win, view, _app, _ids = main_window
    image = view.grab().toImage()
    bg = theme.BG1.lower()
    checked = 0
    for widget in _find(view, QLabel, QCheckBox):
        if not widget.isVisibleTo(view) or widget.objectName() == "historyPill":
            continue
        if widget.parentWidget() is not None and widget.parentWidget().objectName() == "historyDetail":
            continue    # the detail card is a BG2 surface on purpose
        if isinstance(widget.parentWidget(), hv._HistoryRow) or widget.parent() is None:
            pass
        top_left = widget.mapTo(view, widget.rect().topLeft())
        if not view.rect().contains(top_left):
            continue
        if view._list.isAncestorOf(widget) and not view._list.viewport().rect().contains(
                widget.mapTo(view._list.viewport(), widget.rect().topLeft())):
            continue
        assert image.pixelColor(top_left.x(), top_left.y()).name() == bg, widget.objectName() or widget.text()
        checked += 1
    assert checked >= 6


def test_standalone_window_surface_is_the_view_surface(qapp, tmp_path):
    from samsara.ui.history_qt import _HistoryWindow
    mgr, store, _ids = _seed(tmp_path)
    win = _HistoryWindow(types.SimpleNamespace(config={}, history=[], history_store=store))
    _show(win, 860, 620)
    _pump_until(qapp, lambda: len(win._view._rows_by_item_id) > 0)
    image = win.grab().toImage()
    assert image.pixelColor(2, 2).name() == theme.BG1.lower()
    assert image.pixelColor(2, win.height() - 3).name() == theme.BG1.lower()
    win.hide()
    mgr.close()


# ---------------------------------------------------------------------------
# Defect 3: "Show empty wake..." was cut off -- nothing clips, at any size
# ---------------------------------------------------------------------------

def _assert_nothing_clipped(view):
    problems = []
    for widget in _find(view, QPushButton, QCheckBox, QComboBox):
        if not widget.isVisibleTo(view):
            continue
        if widget.width() < widget.sizeHint().width():
            problems.append(f"{type(widget).__name__} {widget.text() if hasattr(widget, 'text') else ''!r}: "
                            f"{widget.width()} < {widget.sizeHint().width()}")
    for label in view.findChildren(QLabel):
        if not label.isVisibleTo(view) or label.wordWrap():
            continue
        fm = label.fontMetrics()
        available = label.contentsRect().width()
        for line in label.text().split("\n"):
            if fm.horizontalAdvance(line) > available + 1:
                problems.append(f"label {label.objectName()} {line!r}: {fm.horizontalAdvance(line)} > {available}")
    search = view._search
    needed = search.fontMetrics().horizontalAdvance(hv.SEARCH_PLACEHOLDER) + 24
    if search.width() < needed:
        problems.append(f"search {search.width()} < {needed}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("size", [(1024, 700), (700, 500)], ids=["default", "narrow"])
def test_main_window_history_page_clips_nothing(qapp, main_window, size):
    win, view, _app, _ids = main_window
    win.resize(*size)
    _pump(qapp, 250)
    assert view._show_empty_wake.text() == hv.NO_SPEECH_TOGGLE_LABEL
    _assert_nothing_clipped(view)


@pytest.mark.parametrize("size", [(860, 620), (520, 400)], ids=["default", "minimum"])
def test_standalone_window_clips_nothing(qapp, tmp_path, size):
    from samsara.ui.history_qt import _HistoryWindow
    mgr, store, _ids = _seed(tmp_path)
    win = _HistoryWindow(types.SimpleNamespace(config={}, history=[], history_store=store))
    _show(win, *size)
    _pump_until(qapp, lambda: len(win._view._rows_by_item_id) > 0)
    _pump(qapp, 250)
    _assert_nothing_clipped(win._view)
    win.hide()
    mgr.close()


def test_filter_row_wraps_instead_of_squeezing(qapp, tmp_path):
    mgr, store, _ids = _seed(tmp_path)
    view = hv.HistoryView(store)
    _show(view, 520, 500)
    _pump(qapp, 250)
    toggle, chips = view._show_empty_wake, view._filter
    assert toggle.width() >= toggle.sizeHint().width()
    assert toggle.geometry().top() > chips.geometry().top()       # moved to its own line
    view.resize(900, 500)
    _pump(qapp, 250)
    assert toggle.geometry().top() == chips.geometry().top()      # back beside the chips
    view.hide()
    mgr.close()


def test_rows_get_their_real_height(qapp, main_window):
    """Pre-79 size hints were QSize(-1, h): invalid, so silently ignored."""
    _win, view, _app, _ids = main_window
    heights = [view._list.visualItemRect(view._list.item(i)).height()
               for i in range(view._list.count())
               if isinstance(view._list.itemWidget(view._list.item(i)), hv._HistoryRow)]
    assert heights and min(heights) >= hv._ROW_MIN_HEIGHT
    assert hv._size(50).isValid()


# ---------------------------------------------------------------------------
# Plain text: apostrophes, markup characters and non-ASCII render literally
# ---------------------------------------------------------------------------

def test_markup_apostrophes_and_non_ascii_render_literally(qapp, main_window):
    _win, view, _app, ids = main_window
    row = next(r for r in _rows(view) if r.row.get('id') == ids[TRICKY])
    assert row.text_label.textFormat() == Qt.TextFormat.PlainText
    assert row.text_label.text().replace("\n", " ") == TRICKY
    assert "<b>" in row.text_label.text() and "it's" in row.text_label.text()
    assert "&#x27;" in row.text_label.text() and "&amp;" not in row.text_label.text()
    tooltip = row.text_label.toolTip()
    assert "&lt;b&gt;" in tooltip and "<b>" not in tooltip.replace("<qt>", "")
    for i in range(view._list.count()):
        if view._list.itemWidget(view._list.item(i)) is row:
            view._list.setCurrentRow(i)
    assert view._detail.toPlainText() == TRICKY
    for label in view.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()


def test_copy_puts_the_exact_text_on_the_clipboard(qapp, main_window, _isolated):
    _win, view, _app, ids = main_window
    row = next(r for r in _rows(view) if r.row.get('id') == ids[TRICKY])
    row.copy_button.click()
    assert _isolated.text == TRICKY
    assert row.copy_button.text() == "Copied"
    assert view._status_lbl.text() == "Copied to clipboard"


# ---------------------------------------------------------------------------
# Contrast and tokens
# ---------------------------------------------------------------------------

def test_every_text_pair_meets_wcag_aa():
    table = hv.contrast_table()
    assert len(table) >= 40
    failing = [(role, round(ratio, 2)) for role, _px, ratio in table if ratio < 4.5]
    assert not failing


def test_no_text_below_the_minimum_readable_size():
    assert all(px >= theme.TYPE_MIN for _role, px, _ratio in hv.contrast_table())
    sizes = {int(m) for m in re.findall(r"font-size:\s*(\d+)px", hv.build_stylesheet())}
    assert sizes and min(sizes) >= theme.TYPE_MIN


def _string_colour_literals(relpath):
    source = (REPO / relpath).read_text(encoding="utf-8")
    pattern = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(\s*\d")
    return [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.STRING and pattern.search(tok.string)]


@pytest.mark.parametrize("relpath", ["samsara/ui/history_view.py", "samsara/ui/history_qt.py"])
def test_history_modules_use_tokens_not_colour_literals(relpath):
    literals = [s for s in _string_colour_literals(relpath) if "'#rrggbb'" not in s and "'rgba(r,g,b,a)'" not in s]
    assert literals == []


# ---------------------------------------------------------------------------
# Recovery-first behaviour, from data already recorded
# ---------------------------------------------------------------------------

def test_no_speech_attempts_are_hidden_from_every_capture_path(qapp, tmp_path):
    mgr, store, ids = _seed(tmp_path)
    view = hv.HistoryView(store)
    shown = {r['id'] for r in view._fetch_rows("", "All", None, hv._SCOPE_ALL, False)}
    assert shown and not any(hv.is_no_speech_attempt(r)
                             for r in view._fetch_rows("", "All", None, hv._SCOPE_ALL, False))
    everything = view._fetch_rows("", "All", None, hv._SCOPE_ALL, True)
    modes = {r['mode'] for r in everything if hv.is_no_speech_attempt(r)}
    assert modes == {"hold", "wake"}        # pre-79 only hid "wake"
    mgr.close()


def test_search_covers_all_history_even_when_browsing_seven_days(qapp, tmp_path):
    mgr, store, ids = _seed(tmp_path)
    view = hv.HistoryView(store)
    assert view._fetch_rows("", "All", None, hv._SCOPE_LAST_7_DAYS) and \
        ids["Old entry."] not in {r['id'] for r in view._fetch_rows("", "All", None, hv._SCOPE_LAST_7_DAYS)}
    found = view._fetch_rows("old entry", "All", None, hv._SCOPE_LAST_7_DAYS)
    assert [r['id'] for r in found] == [ids["Old entry."]]
    view._search.setText("old")
    assert not view._scope.isEnabled()
    assert hv.summary_text(1, hv._SCOPE_LAST_7_DAYS, "old", False) == "1 match for \u201cold\u201d in all history"
    mgr.close()


def test_outcomes_and_corrections_come_from_recorded_columns():
    assert hv.row_outcome({"entry_type": "dictation", "status": "success"}) is None
    assert hv.row_outcome({"entry_type": "dictation", "status": "failed",
                           "display_text": "x"}).kind == hv.KIND_NOT_TYPED
    assert hv.row_outcome({"entry_type": "failed", "status": "failed",
                           "display_text": "[FAILED] boom"}).kind == hv.KIND_FAILED
    assert hv.row_outcome({"entry_type": "failed", "status": "empty", "mode": "hold",
                           "display_text": "(no speech detected)"}).kind == hv.KIND_NO_SPEECH
    assert hv.row_outcome({"entry_type": "command", "status": "success"}).label == "Command"
    assert hv.row_outcome({"entry_type": "command", "status": "awaiting_confirmation"}).label == \
        "Awaiting confirmation"
    corrected = {"entry_type": "dictation", "raw_text": "add cooper netties", "display_text": "Add Kubernetes."}
    punctuation_only = {"entry_type": "dictation", "raw_text": "hello there", "display_text": "Hello, there."}
    assert hv.is_corrected(corrected) and not hv.is_corrected(punctuation_only)
    assert [p.label for p in hv.row_pills(dict(corrected, status="failed"))] == ["Not typed", "Corrected"]
    assert hv.list_text({"entry_type": "failed", "display_text": "[FAILED] boom"}) == \
        "Transcription failed: boom"
    assert hv.copy_text({"entry_type": "failed", "status": "empty", "display_text": "(no speech detected)"}) == ""
    assert hv.meta_text({"app_context": "Slack", "mode": "hold"}) == "Slack \u00b7 Hotkey"
    assert hv.meta_text({"app_context": "Unknown", "mode": "command"}) == ""


def test_corrected_entry_offers_the_original_words(qapp, main_window, _isolated):
    _win, view, _app, ids = main_window
    for i in range(view._list.count()):
        data = view._list.item(i).data(Qt.ItemDataRole.UserRole)
        if data and data.get('id') == ids["Please add Kubernetes."]:
            view._list.setCurrentRow(i)
    card = view._detail
    assert card.isVisible() and card.heard_as.isVisibleTo(card)
    assert card.heard_as.text() == "Heard as: please add cooper netties"
    card.copy_original_button.click()
    assert _isolated.text == "please add cooper netties"
    card.copy_button.click()
    assert _isolated.text == "Please add Kubernetes."


def test_not_typed_entry_says_what_to_do(qapp, main_window):
    _win, view, _app, ids = main_window
    for i in range(view._list.count()):
        data = view._list.item(i).data(Qt.ItemDataRole.UserRole)
        if data and data.get('id') == ids["Send the invoice?"]:
            view._list.setCurrentRow(i)
    assert view._detail.note.isVisibleTo(view._detail)
    assert "wasn't typed" in view._detail.note.text()
    assert view._detail.copy_button.isVisibleTo(view._detail)


def test_saved_pre_79_choices_still_apply(qapp):
    _FakeSettings.values = {"history/date_scope": "All", "history/show_empty_wake_attempts": True}
    view = hv.HistoryView(None)
    assert view._scope.currentText() == hv._SCOPE_ALL
    assert view._show_empty_wake.isChecked()
    view._filter.setCurrentText("Failed")
    assert view._filter.currentText() == hv.FILTER_NOT_TYPED


def test_empty_states_say_what_is_true(qapp, tmp_path):
    assert hv.empty_state_text("", hv._SCOPE_LAST_7_DAYS, "All", True, has_any_history=False).startswith(
        "Nothing here yet")
    assert hv.empty_state_text("", hv._SCOPE_LAST_7_DAYS, "All", True) == "Nothing dictated in the last 7 days."
    assert hv.empty_state_text("zebra", hv._SCOPE_ALL, "All", True) == \
        "No matches for \u201czebra\u201d in all history."
    mgr = HistoryManager(db_path=str(tmp_path / "empty.db"))
    view = hv.HistoryView(HistoryStore(mgr))
    _pump_until(qapp, lambda: view._empty_item is not None)
    widget = view._list.itemWidget(view._empty_item)
    assert widget._message_label.text().startswith("Nothing here yet")
    mgr.close()
