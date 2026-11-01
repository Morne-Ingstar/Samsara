"""Actual Qt controls over a synthetic memo home; no app/microphone launch."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox

from samsara import quick_memo
from samsara.ui.memos_qt import MemosPage
from plugins.commands.cockpit import selection_args
from tests.test_cockpit_memos_975c import app, backend, runner, policy_window


@pytest.fixture
def page(qapp, tmp_path):
    quick_memo.add_memo('Synthetic green', home=tmp_path)
    quick_memo.add_memo('Synthetic blue', home=tmp_path)
    widget = MemosPage(SimpleNamespace(config={}), home=tmp_path,
                       player=SimpleNamespace(stop=lambda: None, play=lambda path: True))
    widget.resize(1500, 1000)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()


def test_checks_survive_filter(page, qapp):
    page._batch_mode.setChecked(True)
    card = page._list.itemWidget(page._list.item(0))
    check = card.findChild(QCheckBox)
    assert check.width() >= 40 and check.height() >= 40
    original_editor = page.selected['id']
    QTest.mouseClick(check, Qt.MouseButton.LeftButton)
    assert len(page._checked_ids) == 1 and page.selected['id'] == original_editor
    page._search.setText('green')
    qapp.processEvents()
    assert page._batch_total.text() == '1 checked; 1 hidden'
    page._search.clear()
    assert len(page._checked_ids) == 1
    assert page._batch_total.text() == '1 checked; 0 hidden'


def test_dirty_editor_requires_explicit_save(page, tmp_path, monkeypatch):
    from plugins.commands import cockpit
    original = page.selected['id']
    page._check_memo(original, True)
    page._detail.setPlainText('Edited synthetic blue')
    page._search.setText('green')
    page._search.clear()
    assert page._drafts[original] == 'Edited synthetic blue'
    assert not page.preview_batch()
    assert 'Save' in page.status_text
    assert all(r['text'] != 'Edited synthetic blue' for r in quick_memo.load_memos(tmp_path))
    index = next(i for i, r in enumerate(page._filtered) if r['id'] == original)
    page._list.setCurrentRow(index)
    assert page._detail.toPlainText() == 'Edited synthetic blue'
    assert page.save_selected()
    queued = []
    monkeypatch.setattr(cockpit, 'start_preview', lambda *a, **k: queued.append((a, k)) or True)
    assert page.preview_batch()
    assert queued and queued[0][0][1]['checked_ids'] == (original,)


def test_single_record_actions_keep_target(page, tmp_path, monkeypatch):
    from pathlib import Path
    from samsara.ui import memos_qt
    checked, other = page._filtered
    page._check_memo(checked['id'], True)
    page._list.setCurrentRow(1)
    assert page.selected['id'] == other['id']
    played = []
    page._player.play = lambda path: played.append(path) or True
    monkeypatch.setattr(memos_qt, 'playable', lambda record, home=None: Path(record['id'] + '.wav'))
    monkeypatch.setattr(memos_qt.QTimer, 'singleShot', lambda *args: None)
    assert page.play_selected()
    assert played == [Path(other['id'] + '.wav')]
    page.request_delete()
    assert page._pending_delete == other['id']
    assert page.confirm_delete()
    assert [r['id'] for r in quick_memo.load_memos(tmp_path)] == [checked['id']]
    assert checked['id'] in page._checked_ids


def test_count_selection_needs_no_modifiers():
    assert selection_args('last three') == dict(mode='last', count=3)
    assert selection_args('three memos for cockpit') == dict(mode='last', count=3)
    assert selection_args('today') == dict(mode='today')
    assert selection_args('on 2026-11-01') == dict(mode='date', on_date='2026-11-01')
    with pytest.raises(ValueError):
        selection_args('last twenty-one')


def test_select_today_and_clear_are_large_buttons(page):
    assert page._batch_mode.accessibleName() == page._batch_mode.text()
    page._today_btn.click()
    assert len(page._checked_ids) == 2
    assert page._batch_mode.isChecked()
    assert page._preview_batch_btn.text().endswith('(2)')
    assert page._preview_batch_btn.accessibleName() == page._preview_batch_btn.text()
    page._clear_batch_btn.click()
    assert page._checked_ids == set()
    assert page._preview_batch_btn.accessibleName() == page._preview_batch_btn.text()
    assert page._preview_batch_btn.text().endswith('(0)')


def test_batch_controls_follow_search_and_list_in_keyboard_navigation(page):
    controls = [page._search, page._list, page._batch_mode, page._today_btn,
                page._clear_batch_btn, page._preview_batch_btn, page._detail]
    for batch_mode in (False, True):
        page._batch_mode.setChecked(batch_mode)
        page._search.setFocus()
        for control in controls:
            assert control.hasFocus(), control.accessibleName()
            page.focusNextPrevChild(True)
        page._detail.setFocus()
        for control in reversed(controls):
            assert control.hasFocus(), control.accessibleName()
            page.focusNextPrevChild(False)


def test_semantic_choice_and_single_ui_approval(qapp, tmp_path, runner, monkeypatch):
    from copy import deepcopy
    import json
    import os
    from pathlib import Path
    from samsara import cockpit_memos as cm
    from samsara.ava_structured_request import StructuredRequest
    from samsara.runtime import thread_registry
    from tests.test_cockpit_memos_975c import FIXTURE
    draft = deepcopy(FIXTURE['extraction_cases'][0]['expected'])
    draft['items'][2]['match'].update(method='semantic', needs_resolution=True)
    runner.extractor = StructuredRequest(runner.app, transport=lambda *a: json.dumps(draft))
    runner.app._cockpit_memo_runner = runner
    runner.start(cm.select_memos(runner.test_data, count=3))
    for source in runner.test_data:
        quick_memo.add_memo(source['text'], home=tmp_path)
    page = MemosPage(runner.app, home=tmp_path)
    page.resize(1500, 900)
    page.show()
    page._batch_mode.setChecked(True)
    page._list.itemWidget(page._list.item(0)).findChild(QCheckBox).click()
    page._show_batch_preview(runner, '')
    for _key, check, _choice in page._batch_choice_controls:
        assert check.accessibleName() == check.text()
    assert runner.proposal['counts']['selected'] == 3
    _key, _check, choice = page._batch_choice_controls[2]
    choice.setCurrentIndex(1)
    assert not page._approve_batch_btn.isEnabled()
    monkeypatch.setattr(thread_registry, 'spawn', lambda _name, work, **k: work())
    page._revise_batch_btn.click()
    assert runner.proposal['counts']['selected'] == 4
    assert runner.proposal['target_choices'] == {'i3': 'task:p1'}
    assert page._approve_batch_btn.isEnabled()
    assert 'Today Board' in page._batch_preview.toPlainText()
    qapp.processEvents()
    directory = Path(os.environ.get('SAMSARA_975C_EVIDENCE_DIR', tmp_path / 'evidence'))
    directory.mkdir(parents=True, exist_ok=True)
    assert page._batch_review.grab().save(str(directory / 'synthetic-batch-preview.png'))
    assert page.grab().save(str(directory / 'synthetic-memos-batch.png'))
    assert page._batch_review.height() <= 900
    (directory / 'synthetic-voice-readback.txt').write_text(page._batch_preview.toPlainText(), encoding='utf-8')
    page._approve_batch_btn.click()
    assert runner.last_result['status'] == 'applied'
    assert runner.test_calls.count('confirm') == runner.test_calls.count('apply') == 1
    assert 'applied' in page.status_text
    page.close()
