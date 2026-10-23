"""753: both draft displays share an explicit, tap-to-teach word menu."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from samsara.dictionary_store import DictionaryStore
from samsara.ui import qt_runtime
from samsara.ui.correction_picker_qt import WordCorrectionMenu, offer_correction_learning
from samsara.live_surface.controller import LiveSurfaceController
from tests.test_live_surface_review import manager, surface_owner


@pytest.fixture
def host(tmp_path, monkeypatch):
    (tmp_path / 'training_data.json').write_text('{"vocabulary": [], "corrections": {}}')
    host = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    host.dictionary_store = DictionaryStore(host)
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    return host


def test_full_pick_keeps_learning_in_same_menu_without_automatic_save(qapp, host, surface_owner):
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold('scrawl up ')
    host._session_mode_manager = subject
    controller = surface_owner(LiveSurfaceController(host))
    controller.sync_draft(subject); controller.drain(); controller.start()
    widget = controller.widget
    widget._anchor_clicked(__import__('PySide6').QtCore.QUrl('word:0'))
    selection = dict(widget._selection)
    widget.show_candidates(selection, [SimpleNamespace(replacement='scroll')])
    menu = widget._word_menu
    before = (host.config_path.parent / 'training_data.json').read_bytes()
    menu.buttons[0].click(); qapp.processEvents()
    assert subject.dictate_pending_buffer == 'scroll up '
    assert host._correction_learning_offer is menu
    assert menu.learning and menu.always.isVisible() and menu.vocabulary.isVisible()
    assert (host.config_path.parent / 'training_data.json').read_bytes() == before
    menu.always.click()
    assert host.dictionary_store.apply_corrections('scrawl') == 'scroll'
    assert not host.dictionary_store.custom_vocab
    menu.vocabulary.click()
    assert host.dictionary_store.custom_vocab == ['scroll']
    menu.dismiss.click()
    assert not menu.isVisible() and host._draft_correction_menu is None


def compact_preview(host, monkeypatch):
    from samsara import streaming
    subject = manager(live=False); subject._stage_dictate_chunk('scrawl up')
    host._session_mode_manager = subject
    host._ensure_session_mode_manager = lambda: subject
    compact = streaming._StreamingWidget(False, streaming.IdleSettings(5, .25), lambda: True)
    preview = object.__new__(streaming.DictatePreviewSession)
    preview.app, preview._overlay, preview._finalized = host, compact, []
    preview._closed, preview._words = False, [('scrawl', 0), ('up', 0)]
    monkeypatch.setattr(preview, '_render', lambda _: None)
    monkeypatch.setattr(preview, '_correction_candidates', lambda _: ['scroll', 'crawl'])
    subject.set_correction_capture_fn(lambda wrong, right, *_: offer_correction_learning(host, wrong, right))
    compact.set_correction_choice_callbacks(preview._on_candidate_chosen, preview._on_candidates_dismissed)
    compact.enable_interaction(preview._on_word_clicked, lambda: None)
    compact.show_overlay()
    # Production click callback and wrapper forwarding, with synthetic candidates.
    preview._on_word_clicked(0)
    return subject, compact, preview


@pytest.mark.parametrize('typed', [False, True])
def test_compact_choice_and_typed_fix_replace_then_teach_only_on_tap(qapp, host, monkeypatch, typed):
    subject, compact, preview = compact_preview(host, monkeypatch)
    try:
        menu = compact._w._choices
        assert isinstance(menu, WordCorrectionMenu) and menu.isVisible()
        assert not any(b.isVisible() and b.text() in {'Spell', 'Edit', 'More'}
                       for b in compact._w.findChildren(QPushButton))
        if typed:
            menu._editor.setText('scroll'); menu.apply.click()
        else:
            menu.buttons[0].click()
        assert subject.dictate_pending_buffer == 'scroll up'
        assert menu.learning and menu.isVisible()
        assert not host.dictionary_store.corrections_dict
        assert not host.dictionary_store.custom_vocab
        menu.vocabulary.click()
        assert host.dictionary_store.custom_vocab == ['scroll']
        assert not host.dictionary_store.corrections_dict
        menu.dismiss.click()
        assert not menu.isVisible()
        assert compact._w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    finally:
        compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


def test_compact_stale_choice_and_cancel_do_not_teach(qapp, host, monkeypatch):
    subject, compact, preview = compact_preview(host, monkeypatch)
    try:
        menu = compact._w._choices
        subject.cancel_word_correction('expired')
        menu.buttons[0].click()
        assert not menu.learning and not menu.isVisible()
        assert not host.dictionary_store.corrections_dict
        preview._on_word_clicked(0)
        menu._editor.setFocus(); qapp.processEvents()
        menu.dismiss.click()
        assert subject.pending_word_correction() is None
        assert getattr(host, '_draft_spelling_input', None) is None
    finally:
        compact.stop_life(); compact._w.hide(); compact._w.deleteLater()


def test_same_field_supports_paste_and_scoped_nato_then_invalidates_queued_speech(qapp, host, monkeypatch):
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from tests.test_live_surface_review import Source, review
    source = Source(review('scrawl')); source.app = host
    widget = LiveSurfaceWidget(source); widget.show()
    try:
        widget.show_candidates({'document_id': 'd', 'revision': 1, 'word': 'scrawl'}, [])
        widget._open_editor(); qapp.processEvents()
        menu = widget._word_menu
        mailbox = host._draft_spelling_input
        assert mailbox is not None
        menu._editor.setText('NewTerm2')
        assert menu._editor.text() == 'NewTerm2'
        menu._editor.clear(); mailbox.consume('sierra charlie romeo oscar lima lima')
        assert menu._editor.text() == 'scroll'
        assert mailbox.consume('delete file')
        assert menu._editor.text() == 'scroll'
        pending = []; monkeypatch.setattr(qt_runtime, 'post', pending.append)
        mailbox.consume('alpha'); menu.dismiss.click(); pending.pop()()
        assert menu._editor.text() == 'scroll'
        assert host._draft_spelling_input is None
    finally:
        widget.hide(); widget.deleteLater()


def test_unchanged_typed_word_dismisses_without_awaiting_learning(qapp, host):
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from tests.test_live_surface_review import Source, review
    source = Source(review('scrawl')); source.app = host
    widget = LiveSurfaceWidget(source); widget.show()
    applied = []; widget.correction_apply_requested.connect(lambda *_: applied.append(True))
    try:
        widget.show_candidates({'document_id': 'd', 'revision': 1, 'word': 'scrawl'}, [])
        menu = widget._word_menu
        menu._editor.setText('scrawl'); menu.apply.click()
        assert not applied and not menu.awaiting and not menu.isVisible()
        assert widget._selection is None
        assert not host.dictionary_store.corrections_dict
    finally:
        widget.hide(); widget.deleteLater()
