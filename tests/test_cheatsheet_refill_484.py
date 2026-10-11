"""Refills must survive signals, pending feedback and first-open requests."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem

from samsara.ui import command_cheatsheet_qt as cs, home_qt


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    rows = [dict(phrase=f"sample {i}", canonical_id=f"sample.{i}",
                 aliases=[f"sample {i}"], plugin="builtin", pack="core")
            for i in range(4)]
    monkeypatch.setattr(cs, "catalog_rows", lambda _: rows)
    monkeypatch.setattr(cs, "_disabled_packs", lambda: set())
    monkeypatch.setattr(cs, "_scope_context", lambda: None)
    win = cs._CheatSheetWindow(lambda _: None, lambda: [], tmp_path / "palette.json")
    yield app, win
    win.hide()
    win.deleteLater()
    app.processEvents()


def ids(win):
    return [win._list.item(i).data(Qt.ItemDataRole.UserRole + 1)
            for i in range(win._list.count())]


def test_model_signal_refill_is_coalesced_and_latest_query_wins(window):
    app, win = window
    depth = []
    fired = []

    def inserted(*_):
        depth.append(win._refill_running)
        if not fired:
            fired.append(True)
            win._filter.setText("sample 3")
            win._apply_filter("sample 2")

    win._list.model().rowsInserted.connect(inserted)
    win._apply_filter("")
    assert ids(win) == [f"sample.{i}" for i in range(4)]
    app.processEvents()
    assert ids(win) == ["sample.2"]
    assert depth and all(depth)
    assert not win._refill_running


def test_details_do_not_keep_item_wrapper_and_close_on_new_generation(window, monkeypatch):
    _, win = window
    callbacks = []
    monkeypatch.setattr(cs.QTimer, "singleShot", lambda *args: callbacks.append(args[-1]))
    win._on_item_clicked(win._list.item(0))
    assert not callbacks  # row activation no longer schedules flash callbacks
    assert not any(isinstance(value, QListWidgetItem) for value in vars(win._detail_card).values())
    win._apply_filter("")
    assert win._detail_card.phrase is None


def test_details_use_phrase_after_row_moves(window, monkeypatch):
    _, win = window
    callbacks = []
    monkeypatch.setattr(cs.QTimer, "singleShot", lambda *args: callbacks.append(args[-1]))
    win._on_item_clicked(win._list.item(0))
    item = win._list.takeItem(0)
    win._list.addItem(item)
    assert not callbacks
    assert win._detail_card.phrase == item.data(Qt.ItemDataRole.UserRole)
    assert item.background().style() == Qt.BrushStyle.NoBrush


def test_refill_restores_signals_and_guard_on_exception(window, monkeypatch):
    _, win = window
    monkeypatch.setattr(cs, "row_text", lambda _: (_ for _ in ()).throw(ValueError("probe")))
    with pytest.raises(ValueError, match="probe"):
        win._apply_filter("")
    assert not win._list.signalsBlocked()
    assert not win._refill_running


@pytest.mark.parametrize("open_kind", ["filtered", "scoped", "plain"])
def test_first_open_fills_once_with_requested_state(window, monkeypatch, tmp_path, open_kind):
    _, _ = window
    posts, fills = [], []
    monkeypatch.setattr(cs.qt_runtime, "post", posts.append)
    original = cs._CheatSheetWindow._refill_list

    def record(win, text):
        fills.append((win._pack_scope, win._active_category, text))
        return original(win, text)

    monkeypatch.setattr(cs._CheatSheetWindow, "_refill_list", record)
    sheet = cs.CommandCheatSheetQt(palette_path=tmp_path / "open.json")
    if open_kind == "filtered":
        assert home_qt.open_cheatsheet_filtered(SimpleNamespace(cheat_sheet=sheet), "sample 2")
    elif open_kind == "scoped":
        sheet.show_scoped({"core"})
    else:
        sheet.show()
    assert sheet._window is None
    while posts:
        posts.pop(0)()
    assert len(fills) == 1
    assert fills[0] == (frozenset({"core"}) if open_kind == "scoped" else None,
                        "All", "sample 2" if open_kind == "filtered" else "")
    sheet._window.hide()
    sheet._window.deleteLater()


def test_reopen_updates_scope_category_and_query_in_one_fill(window, monkeypatch):
    _, win = window
    win.set_pack_scope({"other"})
    win._active_category = "other"
    generation = win._item_generation
    win.set_view(None, ("All", "sample 2"))
    assert win._item_generation == generation + 1
    assert ids(win) == ["sample.2"]
    assert win._filter.text() == "sample 2"
