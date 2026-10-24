"""809: accepted phrase alternates teach words, while cosmetic edits explain the no-op."""
import json
import logging

import pytest

from samsara.dictionary_store import DictionaryStore
from tests.test_draft_phrase_correction_779 import (
    click_full, compact_surface, full_surface,
)
from tests.test_live_surface_review import surface_owner
from tests.test_word_menu_753 import host


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('right,cosmetic', [
    ('alpha better gamma delta', False),
    ('ALPHA BETA GAMMA DELTA', True),
    ('alpha beta gamma delta.', True),
    ('ALPHA BETA GAMMA DELTA.', True),
])
def test_four_word_alternate_pair_save_and_cosmetic_feedback(
        qapp, host, surface_owner, monkeypatch, tmp_path, caplog, display, right, cosmetic):
    wrong = 'alpha beta gamma delta'
    compact = None
    caplog.set_level(logging.DEBUG)
    try:
        if display == 'Full':
            subject, controller = full_surface(host, surface_owner, (wrong,))
            widget = controller.widget
            click_full(widget, 0, monkeypatch)
            click_full(widget, 3, monkeypatch, shift=True)
            selection = dict(widget._selection)
            assert selection['word'] == wrong
            widget.show_candidates(selection, [right])
            menu = widget._word_menu
        else:
            subject, compact, preview = compact_surface(host, monkeypatch, (wrong,))
            preview._on_word_clicked(0); preview._on_word_clicked(3, True)
            assert subject.pending_word_correction()['word'] == wrong
            menu = compact._w._choices
            menu.open(wrong, [right])
        assert menu.wrong == wrong
        pairs = []
        original = menu.show_learning
        def accepted(old, new):
            pairs.append((old, new))
            return original(old, new)
        monkeypatch.setattr(menu, 'show_learning', accepted)
        before = (tmp_path / 'training_data.json').read_bytes()
        menu.buttons[0].click(); qapp.processEvents()
        assert pairs == [(wrong, right)]
        assert subject.dictate_pending_buffer == right
        assert menu.learning and menu.dismiss.text() == 'Done'
        assert 'Could not save' not in menu.status.text()
        if cosmetic:
            assert menu.status.text() == 'Nothing to save - only the capitals or punctuation changed.'
            assert menu.always.isHidden()
            assert (tmp_path / 'training_data.json').read_bytes() == before
            assert not DictionaryStore(host).corrections_dict
            if right.isupper() and not right.endswith('.'):
                assert menu.window().grab().save(str(tmp_path / f'{display}-cosmetic.png'))
        else:
            assert 'Saved.' in menu.status.text()
            saved = json.loads((tmp_path / 'training_data.json').read_text())
            assert saved['corrections'] == {wrong: right}
            assert DictionaryStore(host).apply_corrections(wrong) == right
        messages = [record.getMessage() for record in caplog.records if '[DRAFT-LEARN]' in record.getMessage()]
        assert any('phrase=4 result=learning-offered' in message for message in messages)
        assert any('phrase=4 result=edit-accepted' in message for message in messages)
        assert all(wrong not in message and right not in message for message in messages)
        menu.dismiss.click()
        assert not menu.isVisible()
    finally:
        if compact is not None:
            compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


@pytest.mark.parametrize('wrong,right,vocabulary,reason', [
    ('private source', '', False, 'empty-right'),
    ('', 'private target', False, 'empty-wrong'),
    ('private source', 'PRIVATE SOURCE', False, 'cosmetic'),
    ('private source', 'private source.', False, 'cosmetic'),
    ('private source', 'private target', False, 'save-failed'),
    ('', 'private target', True, 'save-failed'),
])
def test_each_false_store_result_logs_reason_without_text(host, monkeypatch, caplog,
                                                        wrong, right, vocabulary, reason):
    caplog.set_level(logging.INFO)
    before = (host.config_path.parent / 'training_data.json').read_bytes()
    monkeypatch.setattr(host.dictionary_store, 'save_training_data', lambda: False)
    assert not host.dictionary_store.learn_draft_correction(wrong, right, vocabulary=vocabulary)
    messages = [r.getMessage() for r in caplog.records if '[DRAFT-LEARN]' in r.getMessage()]
    assert messages == [f'[DRAFT-LEARN] phrase={len(wrong.split())} result={reason}']
    assert all('private' not in message for message in messages)
    assert not host.dictionary_store.corrections_dict
    assert not host.dictionary_store.custom_vocab
    assert (host.config_path.parent / 'training_data.json').read_bytes() == before


def test_word_boundaries_and_vocabulary_remain_meaningful(host):
    store = host.dictionary_store
    assert store.learn_draft_correction('a part', 'apart')
    assert store.learn_draft_correction('scrawl', 'scroll')
    assert store.learn_draft_correction('', 'TensorFlow', vocabulary=True)
    assert store.learn_draft_correction('', 'TensorFlow', vocabulary=True)
    assert DictionaryStore(host).apply_corrections('a part scrawl') == 'apart scroll'
