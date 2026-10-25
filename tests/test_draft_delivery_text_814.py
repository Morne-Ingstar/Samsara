"""Synthetic text through the real draft, delivery adapter and search filter."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import delivery_text
from samsara.dictionary_store import DictionaryStore
from samsara.session_modes import SessionMode, SessionModeManager


def make_session(monkeypatch, *, single_line=None, config=None, correction=None):
    app = SimpleNamespace(
        config=dict(cleanup_mode='clean', add_trailing_space=True,
                    smart_corrections={'modes': {'wake': True}}),
        _apply_verbatim_if_active=Mock(return_value=None),
        process_transcription=Mock(side_effect=lambda text: text),
        _apply_formatting_tokens=Mock(side_effect=lambda text: text),
        _paste_preserving_clipboard=Mock(return_value=(True, True)),
        add_to_history=Mock(), _log_history=Mock(), _notify_main_window=Mock(),
    )
    app.config.update(config or {})
    smart = Mock(side_effect=correction or (lambda text, app: text))
    monkeypatch.setattr(delivery_text, 'smart_correct', smart)
    adapter = delivery_text.SessionTextDelivery(app, single_line_probe=lambda: single_line)
    manager = SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: 'fixture.exe',
        foreground_hwnd_resolver=lambda: 814,
        inject_fn=adapter.inject, prepare_dictate_fn=adapter.prepare,
        inject_prepared_fn=adapter.inject_prepared,
        remove_chars_fn=Mock(), command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        commit_redecode_fn=Mock(return_value='THIS MUST NEVER REPLACE THE PREVIEW.'),
    )
    app._session_mode_manager = manager
    app._ensure_session_mode_manager = lambda: manager
    manager.reset(SessionMode.DICTATE)
    return app, manager, adapter, smart


@pytest.mark.parametrize('decoded,expected', [
    ('calculator', 'calculator'), ('calculator.', 'calculator'),
    ('open calculator now.', 'open calculator now'),
    ('Is this working?', 'Is this working?'), ('Stop!', 'Stop!'),
    ('Wait...', 'Wait...'),
])
def test_fragment_preview_and_delivered_text_are_identical(monkeypatch, decoded, expected, record_property):
    app, manager, _, smart = make_session(monkeypatch)
    manager._stage_dictate_chunk(decoded)
    preview = manager.draft_document.text
    assert preview == expected
    outcome = manager.commit_pending_dictation()
    assert outcome.kind == 'dictate_committed'
    assert outcome.detail['text'] == preview == app._paste_preserving_clipboard.call_args.args[0]
    manager._commit_redecode_fn.assert_not_called()
    assert smart.call_count == app.process_transcription.call_count == 1
    assert manager.stage_buffer == preview
    assert manager._stack.peek().payload == preview
    record_property('synthetic_final_decode', decoded)
    record_property('dictionary_identity', decoded)
    record_property('draft_display', preview)
    record_property('delivered', app._paste_preserving_clipboard.call_args.args[0])


def test_dictionary_applies_once_before_preparation_and_display(monkeypatch, record_property):
    app, manager, _, _ = make_session(monkeypatch)
    # Build the real in-memory matching engine without loading any user file.
    store = DictionaryStore.__new__(DictionaryStore)
    store.corrections_dict = {'calcu later': 'calculator', 'calculator': 'WRONG SECOND PASS'}
    store._rebuild_corrections_pattern()
    apply = Mock(wraps=store.apply_corrections)
    decoded = 'calcu later.'
    corrected = apply(decoded)  # the unified decode boundary
    assert corrected == 'calculator.'
    manager._stage_dictate_chunk(corrected)
    assert manager.draft_document.text == 'calculator'
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == 'calculator'
    apply.assert_called_once_with(decoded)
    record_property('synthetic_final_decode', decoded)
    record_property('dictionary_corrected', corrected)
    record_property('draft_display', 'calculator')
    record_property('delivered', app._paste_preserving_clipboard.call_args.args[0])


def test_smart_correction_is_visible_once_and_not_reapplied_on_finish(monkeypatch):
    app, manager, _, smart = make_session(
        monkeypatch, correction=lambda text, app: text.replace('wrong', 'correct'))
    manager._stage_dictate_chunk('the wrong search term')
    preview = manager.draft_document.text
    assert preview == 'The correct search term.'
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == preview
    assert smart.call_count == 1


@pytest.mark.parametrize('single_line,expected', [
    (True, 'find all calculator commands'),
    (False, 'Find all calculator commands.'),
    (None, 'Find all calculator commands.'),
])
def test_long_query_only_loses_period_for_known_single_line(monkeypatch, single_line, expected):
    app, manager, _, _ = make_session(monkeypatch, single_line=single_line)
    manager._stage_dictate_chunk('find all calculator commands')
    assert manager.draft_document.text == expected
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == expected


def test_full_sentence_in_multiline_obsidian_keeps_period(monkeypatch):
    app, manager, _, _ = make_session(monkeypatch, single_line=False)
    manager._foreground_exe_resolver = lambda: 'obsidian.exe'
    manager._stage_dictate_chunk('This is a complete sentence')
    preview = manager.draft_document.text
    assert preview == 'This is a complete sentence.'
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == preview


def test_failed_paste_retry_and_manual_edits_do_not_reprepare(monkeypatch):
    app, manager, _, smart = make_session(monkeypatch)
    manager._stage_dictate_chunk('calculator')
    doc = manager.draft_document
    assert doc.edit(doc.document_id, doc.revision, doc.segments[0].id,
                    0, len(doc.text), 'calculator  revised').accepted
    preview = manager.draft_document.text
    app._paste_preserving_clipboard.return_value = (False, False)
    assert manager.commit_pending_dictation().kind == 'dictate_commit_failed'
    assert manager.draft_document.text == preview
    app._paste_preserving_clipboard.return_value = (True, True)
    assert manager.commit_pending_dictation().detail['text'] == preview
    assert app._paste_preserving_clipboard.call_args.args[0] == 'calculator  revised'
    assert smart.call_count == 1


def test_focus_guard_still_reaches_paste(monkeypatch):
    app, manager, _, _ = make_session(monkeypatch)
    manager._stage_dictate_chunk('calculator')
    manager.commit_pending_dictation()
    guard = app._paste_preserving_clipboard.call_args.kwargs['before_paste']
    assert guard()
    manager._foreground_hwnd_resolver = lambda: 815
    assert not guard()


def test_finish_dispatch_and_both_preview_models_use_prepared_draft(monkeypatch):
    from samsara import session_modes
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.streaming import DictatePreviewSession
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'finish')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'finish'}))
    app, manager, _, _ = make_session(monkeypatch)
    signals = session_modes.UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
    outcome = manager.dispatch_utterance('calculator.', signals)
    assert outcome.kind == 'dictate_staged'
    full = SimpleNamespace(post=Mock(), _voice_review_action=Mock())
    LiveSurfaceController.sync_draft(full, manager)
    assert full.post.call_args_list[0].args[0].text == 'calculator'
    compact = DictatePreviewSession.__new__(DictatePreviewSession)
    compact.app = app
    compact._closed, compact._generation, compact._finalized = False, 0, []
    compact._refresh_prompt, compact._render = Mock(), Mock()
    compact.on_utterance_final('calculator.', draft_recovered=True)
    assert compact._finalized == ['calculator']
    committed = manager.dispatch_utterance('finish', signals)
    assert committed.kind == 'dictate_committed'
    assert app._paste_preserving_clipboard.call_args.args[0] == 'calculator'


def test_multiple_chunks_and_sleep_restore_deliver_the_visible_document(monkeypatch):
    app, manager, _, smart = make_session(monkeypatch)
    manager._stage_dictate_chunk('This is a complete sentence.')
    manager._stage_dictate_chunk('and this follows it.')
    preview = manager.draft_document.text
    assert preview == 'This is a complete sentence and this follows it.'
    manager.retain_draft()
    manager.clear_pending_draft()
    manager._restore_retained_draft()
    assert manager.draft_document.text == preview
    assert manager.commit_pending_dictation().detail['text'] == preview
    assert app._paste_preserving_clipboard.call_args.args[0] == preview
    assert smart.call_count == 2


def test_sentence_growing_across_short_chunks_gets_period_before_display(monkeypatch):
    app, manager, _, _ = make_session(monkeypatch, single_line=False)
    manager._stage_dictate_chunk('This is')
    assert manager.draft_document.text == 'This is'
    manager._stage_dictate_chunk('a complete sentence')
    preview = manager.draft_document.text
    assert preview == 'This is a complete sentence.'
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == preview


def test_clipboard_image_consent_retry_keeps_the_exact_draft(monkeypatch):
    from samsara import clipboard_recovery
    app, manager, _, smart = make_session(monkeypatch)
    manager._stage_dictate_chunk('calculator')

    def blocked(*args, **kwargs):
        clipboard_recovery.fail('synthetic image', image=True, sequence=814)
        return False, False

    app._paste_preserving_clipboard.side_effect = blocked
    assert manager.commit_pending_dictation().kind == 'dictate_clipboard_choice'
    app._paste_preserving_clipboard.side_effect = None
    assert clipboard_recovery.answer(manager, 'type it').kind == 'dictate_committed'
    assert app._paste_preserving_clipboard.call_args.args[0] == 'calculator'
    assert smart.call_count == 1


def test_preparation_failure_keeps_previous_draft(monkeypatch):
    app, manager, _, _ = make_session(monkeypatch)
    manager._stage_dictate_chunk('calculator')
    app.process_transcription.side_effect = RuntimeError('synthetic preparation failure')
    result = manager._stage_dictate_chunk('second fragment')
    assert result.kind == 'dictate_commit_failed'
    assert manager.draft_document.text == 'calculator'
    app._paste_preserving_clipboard.assert_not_called()


def test_verbatim_and_legacy_automatic_send_keep_their_pipeline(monkeypatch):
    app, manager, adapter, smart = make_session(monkeypatch)
    adapter.inject('calculator')
    assert app._paste_preserving_clipboard.call_args.args[0] == 'Calculator. '
    app._apply_verbatim_if_active.return_value = 'x.y.'
    manager._stage_dictate_chunk('x.y.')
    manager.commit_pending_dictation()
    assert app._paste_preserving_clipboard.call_args.args[0] == 'x.y.'
    assert smart.call_count == 1


@pytest.mark.parametrize('aria,cls,expected', [
    ('multiline=false', '', True), ('multiline=true', '', False),
    ('required=true; multiline=false;readonly=false', '', True),
    ('', 'QLineEdit', True), ('', 'Chrome_RenderWidgetHostHWND', None),
])
def test_uia_single_line_requires_positive_evidence(aria, cls, expected):
    control = SimpleNamespace(ControlTypeName='EditControl', GetPropertyValue=lambda _: aria,
                              ClassName=cls, NativeWindowHandle=0)
    assert delivery_text.edit_is_single_line(control) is expected


def test_uia_unavailable_falls_back_to_word_count(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'uiautomation', None)
    assert delivery_text.focused_single_line() is None


@pytest.mark.parametrize('style,expected', [(0, True), (0x0004, False)])
def test_uia_native_edit_uses_multiline_style(monkeypatch, style, expected):
    import sys
    native = SimpleNamespace(GetClassName=lambda _: 'Edit', GetWindowLong=lambda *args: style)
    monkeypatch.setitem(sys.modules, 'win32gui', native)
    control = SimpleNamespace(ControlTypeName='EditControl', GetPropertyValue=lambda _: '',
                              ClassName='Edit', NativeWindowHandle=814)
    assert delivery_text.edit_is_single_line(control) is expected


@pytest.mark.parametrize('query,expected', [
    (' calculator. ', 'calculator'), ('“calculator!”', 'calculator'),
    ('... CALCULATOR ...', 'calculator'), ('  ...  ', ''),
    ('find x.y', 'find x.y'),
])
def test_search_query_boundary_normalization(query, expected):
    assert delivery_text.normalize_search_query(query) == expected


def test_real_command_reference_search_matches_punctuated_query(qapp, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt
    from samsara.ui import command_cheatsheet_qt as cs
    rows = [dict(phrase='open calculator', canonical_id='fixture.calculator',
                 aliases=['calculator'], description='Open the calculator.', plugin='builtin', pack='core')]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: rows)
    monkeypatch.setattr(cs, '_disabled_packs', lambda: set())
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    window = cs._CheatSheetWindow(lambda _: None, lambda: [], tmp_path / 'palette.json')
    try:
        window._filter.setText('“calculator.”')
        assert window._list.count() == 1
        assert window._list.item(0).data(Qt.ItemDataRole.UserRole) == 'open calculator'
    finally:
        window.close()
        window.deleteLater()
