"""621: local corrections teach only on approval; bounded command repair."""
import os
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.dictionary_store import DictionaryStore
from samsara.phonetic_wash import repair_command_near_miss
from samsara.ui import qt_runtime
from samsara.ui.correction_picker_qt import (CorrectionLearningOffer,
    offer_correction_learning, spelling_chunk)
from samsara.live_surface.controller import LiveSurfaceController
from tests.test_live_surface_review import manager, review, Source
from tests.test_several_commands_399 import rig


@pytest.fixture
def app(tmp_path):
    (tmp_path / 'training_data.json').write_text(
        '{"vocabulary": [], "corrections": {}}', encoding='utf-8')
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    app.dictionary_store = DictionaryStore(app)
    return app


@pytest.fixture
def on_qt(monkeypatch):
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())


def select(controller, subject):
    segment = subject.draft_document.segments[0]
    selection = {'document_id': subject.draft_document.document_id,
        'revision': subject.draft_document.revision, 'segment_id': segment.id,
        'start': 0, 'end': 6}
    controller._correct(selection)
    return dict(controller.widget._selection)


def test_full_correction_then_independent_opt_in_saves(qapp, app, on_qt):
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold('scrawl up ')
    app._session_mode_manager = subject
    controller = LiveSurfaceController(app)
    controller.sync_draft(subject); controller.drain(); controller.start()
    try:
        selection = select(controller, subject)
        before = (app.config_path.parent / 'training_data.json').read_bytes()
        controller._apply_correction(selection, 'scroll')
        assert subject.dictate_pending_buffer == 'scroll up '
        offer = app._correction_learning_offer
        assert offer.isVisible()
        assert (app.config_path.parent / 'training_data.json').read_bytes() == before
        assert app.dictionary_store.apply_corrections('scrawl up') == 'scrawl up'
        offer.always.click()
        assert app.dictionary_store.apply_corrections('scrawl up') == 'scroll up'
        assert app.dictionary_store.custom_vocab == []
        offer.vocabulary.click()
        assert app.dictionary_store.custom_vocab == ['scroll']
        loaded = DictionaryStore(app)
        assert loaded.apply_corrections('scrawl up') == 'scroll up'
        assert loaded.custom_vocab == ['scroll']
        assert not offer.always.isEnabled() and not offer.vocabulary.isEnabled()
        assert subject._do_scratch_that()
        assert subject.dictate_pending_buffer == 'scrawl up '
    finally:
        controller.close()
        getattr(app, '_correction_learning_offer', None).hide()


def test_rejected_stale_edit_does_not_offer_teaching(qapp, app, on_qt):
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold('scrawl up ')
    app._session_mode_manager = subject
    controller = LiveSurfaceController(app)
    controller.sync_draft(subject); controller.drain(); controller.start()
    try:
        selection = select(controller, subject)
        subject.stage_parked_hold('later ')
        controller._apply_correction(selection, 'scroll')
        assert not hasattr(app, '_correction_learning_offer')
        assert not app.dictionary_store.corrections_dict
    finally:
        controller.close()


def test_learning_dismiss_and_failed_save_are_not_training(qapp, app, monkeypatch):
    offer = CorrectionLearningOffer(app, 'scrawl', 'scroll')
    offer.show(); offer.dismiss.click()
    assert not app.dictionary_store.corrections_dict
    monkeypatch.setattr(app.dictionary_store, 'save_training_data', lambda: False)
    for button in (offer.always, offer.vocabulary):
        button.click()
        assert button.isEnabled()
        assert offer.status.text() == 'Could not save. Try again.'
        assert not app.dictionary_store.corrections_dict
        assert not app.dictionary_store.custom_vocab
        assert app.dictionary_store.apply_corrections('scrawl') == 'scrawl'
    offer.close()


def test_vocabulary_tap_does_not_save_correction_and_offer_replaces(qapp, app, on_qt):
    offer_correction_learning(app, 'inventedtoken', 'NewTerm')
    first = app._correction_learning_offer
    first.vocabulary.click()
    assert app.dictionary_store.custom_vocab == ['NewTerm']
    assert app.dictionary_store.corrections_dict == {}
    offer_correction_learning(app, 'scrawl', 'scroll')
    assert not first.isVisible()
    app._correction_learning_offer.hide()


@pytest.mark.parametrize('text,expected', [('sierra charlie romeo oscar lima lima', 'scroll'),
    ('ess see are oh el el', 'scroll'), ('alpha space bravo hyphen charlie apostrophe delta', "a b-c'd"),
    ('capital alpha bravo', 'Ab'), ('delete file', None)])
def test_closed_letter_and_nato_parser(text, expected):
    assert spelling_chunk(text) == expected


def test_spell_owns_command_speech_and_closing_invalidates_queued_input(qapp, app, rig, monkeypatch):
    executor, host, calls = rig
    source = Source(review('scrawl up'), [('s', 'scrawl up')]); source.app = host
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    widget = LiveSurfaceWidget(source)
    widget.show_candidates({'document_id': 'd', 'revision': 1, 'segment_id': 's',
        'start': 0, 'end': 6, 'word': 'scrawl'}, [])
    widget.show()
    posted = []; monkeypatch.setattr(qt_runtime, 'post', posted.append)
    widget._open_editor('spell')
    assert 'Command-only key' in widget._editor.placeholderText()
    assert executor.process_text('sierra charlie romeo oscar lima lima', force_commands=True).claimed
    posted.pop(0)()
    assert widget._editor.text() == 'scroll' and not calls
    executor.process_text('delete file', force_commands=True); posted.pop(0)()
    assert widget._editor.text() == 'scroll' and not calls
    executor.process_text('backspace', force_commands=True); posted.pop(0)()
    assert widget._editor.text() == 'scrol'
    executor.process_text('lima', force_commands=True)
    widget._cancel_editor(); posted.pop(0)()
    assert widget._editor.text() == 'scrol' and host._draft_spelling_input is None
    widget._open_editor('edit')
    assert widget._editor.placeholderText() == 'Type or paste a replacement'
    assert host._draft_spelling_input is None
    widget.hide()


def test_more_only_exists_with_another_page_and_wraps(qapp):
    from samsara.correction_candidates import CorrectionCandidate
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    widget = LiveSurfaceWidget(Source(review('scrawl up'), [('s', 'scrawl up')]))
    selection = {'document_id': 'd', 'revision': 1, 'word': 'scrawl'}
    candidates = [CorrectionCandidate(f'choice{i}', (0, 6), 'test', 1) for i in range(9)]
    widget.show(); widget.show_candidates(selection, candidates[:2])
    assert not widget._more.isVisible()
    widget.show_candidates(selection, candidates)
    assert widget._more.isVisible()
    for page in (1, 2, 0):
        widget._more.click()
        assert widget._candidate_page == page
        assert f'choice{page * 4}' in widget._candidate_buttons[0].text()
    widget.hide()


@pytest.mark.parametrize('lane', ['command', 'hands-free', 'ordinary'])
def test_scrawl_repair_runs_only_whole_commands_in_allowed_lanes(rig, lane):
    from samsara.command_registry import DispatchState
    executor, app, calls = rig
    app.config['command_mode'] = {'dictation_literal_commands_only': True}
    if lane == 'hands-free':
        cls = sys.modules['dictation'].DictationApp
        for name in ('_probe_media_transport', '_probe_hands_free_command'):
            setattr(app, name, MethodType(getattr(cls, name), app))
        match = app._probe_hands_free_command('scrawl up')
        assert match is not None and match.dispatch_text == 'scroll up'
        result = executor.process_text(match.dispatch_text, force_commands=True)
    else:
        result = executor.process_text('scrawl up', force_commands=lane == 'command')
    if lane == 'ordinary':
        assert result.state is DispatchState.MISS and not calls
        assert result.result == 'scrawl up'
    else:
        assert result.state is DispatchState.COMPLETED
        assert calls == [('scroll up', '')]


@pytest.mark.parametrize('text', ['scrawl up the page', 'please scrawl up', 'I scrawl up notes',
    'scrawl down in my notebook', 'scrawl', 'scrawl up and erase', 'crawl up', 'stroll up'])
def test_near_miss_threshold_refuses_prose_and_unobserved_sounds(rig, text):
    executor, app, calls = rig
    assert repair_command_near_miss(text, executor) is None
    assert tuple(executor.process_text(text)) == (text, False)
    assert not calls


def test_disabled_catalogue_literal_and_argument_commands_cannot_be_repaired(rig, monkeypatch):
    executor, app, calls = rig
    assert repair_command_near_miss('SCRAWL UP!', executor) == 'scroll up'
    with monkeypatch.context() as changes:
        changes.setattr(executor, '_takes_argument', lambda entry: True)
        assert repair_command_near_miss('scrawl up', executor) is None
    executor._matcher.set_enabled_packs(set())
    assert repair_command_near_miss('scrawl up', executor) is None


def test_compact_success_capture_offers_same_opt_in_picker(qapp, app, monkeypatch, on_qt):
    from samsara import streaming
    from samsara import correction_queue
    preview = object.__new__(streaming.DictatePreviewSession); preview.app = app
    queue = SimpleNamespace(capture=Mock(return_value={'stored': False, 'reason': 'test'}))
    monkeypatch.setattr(correction_queue, 'get_queue', lambda: queue)
    preview._capture_correction('scrawl', 'scroll')
    assert app._correction_learning_offer.isVisible()
    assert not app.dictionary_store.corrections_dict
    app._correction_learning_offer.always.click()
    assert app.dictionary_store.apply_corrections('scrawl') == 'scroll'
    app._correction_learning_offer.hide()


def test_proof_full_and_compact(qapp, app, on_qt):
    """Optional output only: synthetic evidence, never an exported-test input."""
    result = os.environ.get('GATE_RESULT')
    if not result:
        return
    output = Path(result).parent
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    source = Source(review('scrawl up'), [('s', 'scrawl up')]); source.app = app
    widget = LiveSurfaceWidget(source); widget.show()
    widget.show_candidates({'document_id': 'd', 'revision': 1, 'word': 'scrawl'}, [])
    widget._open_editor('spell'); qapp.processEvents()
    widget.grab().save(str(output / 'Full-spell.png'))
    widget._cancel_editor(); widget.hide()
    for display in ('Full', 'Compact'):
        offer = CorrectionLearningOffer(app, 'scrawl', 'scroll'); offer.show(); qapp.processEvents()
        assert offer.grab().save(str(output / f'{display}-teaching.png'))
        offer.hide(); offer.deleteLater()
    # The two display implementations are separate proof frames, captioned by
    # name. Only synthetic text is rendered, never a live app or real dictation.
    from samsara import streaming
    compact = streaming._StreamingWidget(False, streaming.IdleSettings(5.0, 0.25), lambda: False)
    compact.show_overlay(); qapp.processEvents()
    compact._w.set_prompt('Correction applied')
    compact._w.grab().save(str(output / 'Compact-preview.png'))
    compact.stop_life(); compact._w.hide(); compact._w.deleteLater()
    from samsara.ui.listening_indicator import ListeningIndicator
    indicator = ListeningIndicator(); indicator.show(); qapp.processEvents()
    indicator.grab().save(str(output / 'Compact-indicator.png'))
    indicator.hide(); indicator.deleteLater()
