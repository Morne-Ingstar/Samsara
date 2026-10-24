"""854: approved memo cards with the selected transcript editor."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLineEdit, QPlainTextEdit, QSizePolicy
from samsara import quick_memo
from samsara.ui.memos_qt import MemosPage


@pytest.fixture
def page(qapp, tmp_path):
    quick_memo.add_memo("First full transcript", home=tmp_path, category="Ideas")
    quick_memo.add_memo("Second full transcript", home=tmp_path, category="Work")
    widget = MemosPage(SimpleNamespace(config={}), home=tmp_path)
    widget.resize(1500, 900)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def test_selection_shows_right_editor_and_deselection_removes_it(page, qapp):
    page._list.setCurrentRow(1)
    qapp.processEvents()
    assert page._editor_pane.isVisible()
    assert isinstance(page._detail, QPlainTextEdit)
    assert page._detail.toPlainText() == "First full transcript"
    assert page._editor_pane.x() > page._list.x() + page._list.width()
    assert page._list.width() == 430
    page._list.clearSelection()
    qapp.processEvents()
    assert page.selected is None
    assert not page._editor_pane.isVisible()
    assert page._list.width() == 700


def test_no_unbounded_expanding_input(page):
    for control in page.findChildren(QLineEdit) + page.findChildren(QComboBox) + page.findChildren(QPlainTextEdit):
        if control.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding:
            assert control.maximumWidth() < 16777215, control.accessibleName()


def test_edit_persists_both_stores_and_preserves_identity(page, tmp_path):
    original = dict(page.selected)
    page._detail.setPlainText("An edited thought")
    page._save_btn.click()
    stored = next(r for r in quick_memo.load_memos(tmp_path) if r["id"] == original["id"])
    assert stored == dict(original, text="An edited thought")
    assert "An edited thought" in quick_memo.memo_file(tmp_path).read_text(encoding="utf-8")
    assert page.selected["id"] == original["id"]
    assert page.detail_text == "An edited thought"


def test_empty_edit_is_not_saved(page, tmp_path):
    before = quick_memo.load_memos(tmp_path)
    page._detail.clear()
    assert page.save_selected() is False
    assert quick_memo.load_memos(tmp_path) == before


def test_existing_action_buttons_remain_wired(page, tmp_path, qapp):
    calls = []
    page._app.start_memo_capture = lambda: calls.append("voice") or True
    page._app.command_executor = SimpleNamespace(process_text=lambda *a, **k: calls.append("video") or True)
    page._record_voice_btn.click()
    page._record_video_btn.click()
    assert calls == ["voice", "video"]
    page._copy_btn.click()
    assert qapp.clipboard().text() == page.selected["text"]
    page._category.setCurrentText("Projects")
    page._category.lineEdit().returnPressed.emit()
    assert page.selected["category"] == "Projects"
    page._delete_btn.click()
    assert page._confirm_btn.isVisible()
    page._confirm_btn.click()
    assert len(quick_memo.load_memos(tmp_path)) == 1


def test_category_filter_and_no_matches_hide_editor(page, qapp):
    page._filter_category.setCurrentText("Ideas")
    assert len(page.rows) == 1
    assert page.selected["category"] == "Ideas"
    page._search.setText("no such phrase")
    qapp.processEvents()
    assert page.rows == []
    assert not page._editor_pane.isVisible()


def test_empty_store_has_guidance_and_no_blank_editor(qapp, tmp_path):
    page = MemosPage(SimpleNamespace(config={}), home=tmp_path)
    page.show()
    qapp.processEvents()
    assert page._empty.isVisible()
    assert not page._editor_pane.isVisible()
    page.close()


def test_resizing_cards_hides_replaced_widgets(page, qapp):
    old = page._list.itemWidget(page._list.item(0))
    page._list.setCurrentRow(-1)
    assert old.isHidden()
    qapp.processEvents()
    assert page.status_text == ""
    assert page._list.itemWidget(page._list.item(0)).width() == 680
