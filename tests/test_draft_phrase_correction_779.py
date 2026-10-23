"""779: both real draft menus select and teach revision-bound word runs."""
import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication

from samsara.dictionary_store import DictionaryStore
from samsara.draft_selection import select_range, word_targets
from samsara.live_surface.controller import LiveSurfaceController
from samsara.ui.correction_picker_qt import offer_correction_learning
from tests.test_live_surface_review import manager, surface_owner
from tests.test_word_menu_753 import host


def full_surface(host, own, chunks=('please enhance ', 'free today')):
    subject = manager(); subject.pause_draft()
    for chunk in chunks:
        subject.stage_parked_hold(chunk)
    host._session_mode_manager = subject
    controller = own(LiveSurfaceController(host))
    controller.sync_draft(subject); controller.drain(); controller.start()
    return subject, controller


def compact_surface(host, monkeypatch, chunks=('please enhance ', 'free today')):
    from samsara import streaming
    subject = manager(live=False)
    for chunk in chunks:
        subject._stage_dictate_chunk(chunk)
    host._session_mode_manager = subject
    host._ensure_session_mode_manager = lambda: subject
    compact = streaming._StreamingWidget(False, streaming.IdleSettings(5, .25), lambda: True)
    preview = object.__new__(streaming.DictatePreviewSession)
    preview.app, preview._overlay = host, compact
    preview._closed = False
    preview._finalized = [subject.dictate_pending_buffer]
    preview._words = streaming.word_targets(preview._finalized)
    monkeypatch.setattr(preview, '_render', lambda _: None)
    monkeypatch.setattr(preview, '_correction_candidates', lambda _: [])
    subject.set_correction_capture_fn(lambda wrong, right, *_: offer_correction_learning(host, wrong, right))
    compact.set_correction_choice_callbacks(preview._on_candidate_chosen, preview._on_candidates_dismissed)
    compact.enable_interaction(preview._on_word_clicked, lambda: None)
    compact._w._on_update(streaming._join_dictate_fragments(preview._finalized, True), 'listening', 'rich')
    compact.show_overlay()
    return subject, compact, preview


def click_full(widget, index, monkeypatch, *, shift=False):
    monkeypatch.setattr(QApplication, 'keyboardModifiers', lambda: (
        Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier))
    widget._anchor_clicked(QUrl(f'word:{index}'))


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('replacement', ['in hands free', 'handsfree'])
def test_full_cross_segment_select_save_future_and_undo(qapp, host, surface_owner, monkeypatch, reverse, replacement):
    subject, controller = full_surface(host, surface_owner)
    widget = controller.widget
    first, last = (2, 1) if reverse else (1, 2)
    click_full(widget, first, monkeypatch)
    click_full(widget, last, monkeypatch, shift=True)
    assert widget._selection['word'] == 'enhance free'
    assert widget._transcript.isVisible()
    assert widget._transcript.toHtml().count('background-color:') >= 2
    menu = widget._word_menu
    assert menu.apply.text() == 'Save correction'
    assert not menu.phrase_warning.isVisible()
    menu._editor.setText(replacement); menu.apply.click(); qapp.processEvents()
    assert subject.dictate_pending_buffer == f'please {replacement} today'
    assert menu.learning and 'Saved.' in menu.status.text()
    reloaded = DictionaryStore(host)
    assert reloaded.apply_corrections('please enhance free tomorrow') == f'please {replacement} tomorrow'
    assert subject.draft_document.undo()
    assert subject.dictate_pending_buffer == 'please enhance free today'
    assert reloaded.remove_correction('enhance free')
    assert DictionaryStore(host).apply_corrections('enhance free') == 'enhance free'


@pytest.mark.parametrize('reverse', [False, True])
def test_compact_shift_link_selects_exact_span_and_saves(qapp, host, monkeypatch, reverse):
    subject, compact, preview = compact_surface(host, monkeypatch)
    try:
        first, last = (2, 1) if reverse else (1, 2)
        monkeypatch.setattr(QApplication, 'keyboardModifiers', lambda: Qt.KeyboardModifier.NoModifier)
        compact._w._on_link(f'w:{first}')
        monkeypatch.setattr(QApplication, 'keyboardModifiers', lambda: Qt.KeyboardModifier.ShiftModifier)
        compact._w._on_link(f'w:{last}')
        assert subject.pending_word_correction()['word'] == 'enhance free'
        assert compact._w._label.text().count('background-color:') == 2
        menu = compact._w._choices
        menu._editor.setText('in hands free'); menu.apply.click(); qapp.processEvents()
        assert subject.dictate_pending_buffer == 'please in hands free today'
        assert DictionaryStore(host).apply_corrections('enhance free') == 'in hands free'
        assert menu.learning and 'Saved.' in menu.status.text()
        assert subject.draft_document.undo()
        assert subject.dictate_pending_buffer == 'please enhance free today'
    finally:
        compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_changed_revision_never_edits_or_teaches(qapp, host, surface_owner, monkeypatch, display):
    compact = None
    try:
        if display == 'Full':
            subject, controller = full_surface(host, surface_owner)
            widget = controller.widget
            click_full(widget, 1, monkeypatch); click_full(widget, 2, monkeypatch, shift=True)
            menu = widget._word_menu
        else:
            subject, compact, preview = compact_surface(host, monkeypatch)
            preview._on_word_clicked(1); preview._on_word_clicked(2, True)
            menu = compact._w._choices
        subject.draft_document.append(' later')
        before = subject.dictate_pending_buffer
        menu._editor.setText('in hands free'); menu.apply.click(); qapp.processEvents()
        assert subject.dictate_pending_buffer == before
        assert not host.dictionary_store.corrections_dict
        assert not menu.learning
    finally:
        if compact:
            compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


def test_common_phrase_warning_allows_save_and_cancel_does_not_teach(qapp, host, surface_owner, monkeypatch):
    subject, controller = full_surface(host, surface_owner, ('and the kettle',))
    widget = controller.widget
    click_full(widget, 0, monkeypatch); click_full(widget, 1, monkeypatch, shift=True)
    menu = widget._word_menu
    assert menu.phrase_warning.isVisible()
    assert menu.phrase_warning.text() == 'Common words: this correction may change other sentences too.'
    menu.dismiss.click()
    assert subject.dictate_pending_buffer == 'and the kettle'
    assert not host.dictionary_store.corrections_dict
    click_full(widget, 0, monkeypatch); click_full(widget, 1, monkeypatch, shift=True)
    menu._editor.setText('in a'); menu.apply.click(); qapp.processEvents()
    assert subject.dictate_pending_buffer == 'in a kettle'
    assert host.dictionary_store.apply_corrections('and the cup') == 'in a cup'


def test_failed_save_keeps_accepted_edit_and_retries_without_reediting(qapp, host, surface_owner, monkeypatch):
    subject, controller = full_surface(host, surface_owner)
    widget = controller.widget
    click_full(widget, 1, monkeypatch); click_full(widget, 2, monkeypatch, shift=True)
    menu = widget._word_menu
    original_save = host.dictionary_store.save_training_data
    monkeypatch.setattr(host.dictionary_store, 'save_training_data', lambda: False)
    menu._editor.setText('in hands free'); menu.apply.click(); qapp.processEvents()
    assert subject.dictate_pending_buffer == 'please in hands free today'
    revision = subject.draft_document.revision
    assert menu.status.text() == 'Could not save. Try again.'
    assert not host.dictionary_store.corrections_dict
    assert menu.always.isVisible() and menu.always.isEnabled()
    monkeypatch.setattr(host.dictionary_store, 'save_training_data', original_save)
    menu.always.click()
    assert subject.draft_document.revision == revision
    assert DictionaryStore(host).apply_corrections('enhance free') == 'in hands free'


def test_repeated_phrase_keeps_original_anchor_and_stale_anchor_is_reset():
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold('enhance free enhance free')
    doc = subject.draft_document
    segments = [(s.id, s.text) for s in doc.segments]
    targets = word_targets(doc)
    selection = select_range(targets, 2, segments)
    selection = select_range(targets, 3, segments, selection, extend=True)
    assert selection['start'] == 13 and selection['word'] == 'enhance free'
    result = subject.apply_live_surface_correction(selection, 'in hands free')
    assert result['ok'] and doc.text == 'enhance free in hands free'
    targets = word_targets(doc)
    fresh = select_range(targets, 0, [(s.id, s.text) for s in doc.segments], selection, extend=True)
    assert fresh['word'] == 'enhance' and fresh['anchor_index'] == 0


def test_invalid_cross_segment_coordinates_refused_and_undo_preserves_ids():
    subject = manager(); subject.pause_draft()
    for chunk in ('before ', 'enhance ', 'free', ' after'):
        subject.stage_parked_hold(chunk)
    doc = subject.draft_document
    before = doc.segments
    selection = select_range(word_targets(doc), 1, [(s.id, s.text) for s in before])
    selection = select_range(word_targets(doc), 2, [(s.id, s.text) for s in before], selection, extend=True)
    bad = dict(selection, end=999)
    assert not subject.select_live_surface_word(bad)['ok']
    assert not subject.apply_live_surface_correction(bad, 'x')['ok']
    assert doc.segments == before
    assert subject.apply_live_surface_correction(selection, 'in hands free')['ok']
    doc.append(' later')
    assert doc.undo() and doc.text == 'before in hands free after'
    assert doc.undo() and doc.segments == before
    assert doc.undo() and doc.text == 'before enhance free'


def test_compact_cancel_clears_highlight_and_anchor(qapp, host, monkeypatch):
    subject, compact, preview = compact_surface(host, monkeypatch)
    try:
        preview._on_word_clicked(1); preview._on_word_clicked(2, True)
        menu = compact._w._choices
        assert compact._w._label.text().count('background-color:') == 2
        menu.dismiss.click()
        assert not subject.pending_word_correction()
        assert 'background-color:' not in compact._w._label.text()
        preview._on_word_clicked(3, True)
        assert subject.pending_word_correction()['word'] == 'today'
        assert not host.dictionary_store.corrections_dict
    finally:
        compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


def test_full_numbered_picker_can_extend_by_shift_click(qapp, host, surface_owner, monkeypatch):
    subject, controller = full_surface(host, surface_owner)
    widget = controller.widget
    widget.show_word_picker(); widget._choose_picker(1)
    click_full(widget, 2, monkeypatch, shift=True)
    assert widget._selection['word'] == 'enhance free'
