"""997a: synthetic text, targets and offscreen Settings; no app bootstrap."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import session_modes as sm
from samsara.cleanup import clean_text


GOOD = sm.UtteranceSignals(True, (1.0,), True)


@pytest.mark.parametrize('word', ['Actually', 'So', 'Well', 'Like', 'Basically', 'You know', 'I mean'])
@pytest.mark.parametrize('position', ['leading', 'middle'])
def test_clean_keeps_discourse_words_with_decoder_commas(word, position):
    middle = word if word == 'I mean' else word.lower()
    text = f'{word}, this is synthetic.' if position == 'leading' else f'This is, {middle}, synthetic.'
    assert clean_text(text) == text


@pytest.mark.parametrize('text,expected', [
    ('um this is synthetic', 'This is synthetic.'),
    ('this uh is synthetic', 'This is synthetic.'),
    ('er this is synthetic', 'This is synthetic.'),
    ('I I think this is synthetic', 'I think this is synthetic.'),
    ('I I I think this is synthetic', 'I think this is synthetic.'),
    ('This is very very synthetic', 'This is very very synthetic.'),
    ('I. I think this is synthetic', 'I. I think this is synthetic.'),
])
def test_clean_removes_disfluencies_but_keeps_emphasis_and_sentence_boundaries(text, expected):
    assert clean_text(text) == expected
    assert clean_text(text, mode='verbatim') == text


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(sm, 'DICTATE_COMMIT_PHRASE', 'finish')
    monkeypatch.setattr(sm, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'finish'}))
    result = sm.SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: 'synthetic.exe',
        foreground_hwnd_resolver=lambda: 202,
        inject_fn=Mock(side_effect=lambda text, guard: text if guard() else False),
        remove_chars_fn=Mock(return_value=True),
        command_dispatch_fn=Mock(return_value=sm.CommandDispatchResult(False, None)),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
        live_surface_enabled=True,
    )
    result.reset(sm.SessionMode.DICTATE)
    return result


@pytest.mark.parametrize('parked', [False, True])
def test_scratch_draft_with_missing_hwnd_is_local_even_after_focus_changes(manager, parked):
    if parked:
        manager.stage_parked_hold('Synthetic draft.')
    else:
        manager.dispatch_utterance('Synthetic draft.', GOOD)
    manager._stack.peek().extra['hwnd'] = None
    manager._foreground_exe_resolver = Mock(side_effect=AssertionError('local undo must not query focus'))
    manager._foreground_hwnd_resolver = Mock(side_effect=AssertionError('local undo must not query focus'))
    assert manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert manager.dictate_pending_buffer == ''
    assert manager._dictate_pending_audio == []
    assert manager._stack.peek() is None
    manager._inject_fn.assert_not_called()
    manager._remove_chars_fn.assert_not_called()


def test_scratch_latest_draft_chunk_keeps_previous_chunk_and_audio(manager):
    manager._stage_dictate_chunk('Synthetic first.', audio_ref='first')
    manager._stage_dictate_chunk('Synthetic second.', audio_ref='second')
    assert manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert manager.dictate_pending_buffer == 'Synthetic first.'
    assert manager._dictate_pending_audio == ['first']
    manager._remove_chars_fn.assert_not_called()


@pytest.mark.parametrize('recorded,current,expected', [(None, 202, True), (202, 202, True), (201, 202, False), (202, None, False)])
def test_external_scratch_rejects_only_known_window_mismatch(manager, recorded, current, expected):
    receipt = Mock()
    receipt.change.return_value = True
    item = sm.StackItem('dictation_chunk', 'Synthetic delivery.', sm.SessionMode.DICTATE, 0,
                        {'target_process': 'synthetic.exe', 'hwnd': recorded, 'undo_unit': receipt})
    manager._stack.push(item)
    manager._require_unit_undo = True
    manager._foreground_hwnd_resolver = lambda: current
    assert manager._do_scratch_that() is expected
    assert receipt.change.call_count == int(expected)
    manager._remove_chars_fn.assert_not_called()
    assert (manager._stack.peek() is None) is expected


def test_missing_hwnd_does_not_bypass_production_receipt_requirement(manager):
    manager._stack.push(sm.StackItem('dictation_chunk', 'Synthetic delivery.', sm.SessionMode.DICTATE, 0,
                                     {'target_process': 'synthetic.exe', 'hwnd': None}))
    manager._require_unit_undo = True
    assert not manager._do_scratch_that()
    manager._remove_chars_fn.assert_not_called()
    assert manager._stack.peek() is not None


@pytest.mark.parametrize('word', ['send', 'Send.', 'SEND!', 'finish'])
def test_whole_send_or_configured_word_commits_once_without_typing_control(manager, word):
    manager.dispatch_utterance('Synthetic draft.', GOOD)
    outcome = manager.dispatch_utterance(word, GOOD)
    assert outcome.kind == 'dictate_committed'
    assert outcome.detail['text'] == 'Synthetic draft.'
    assert manager.mode is sm.SessionMode.DICTATE
    assert manager.dictate_pending_buffer == ''
    manager._inject_fn.assert_called_once()
    assert manager._inject_fn.call_args.args[0] == 'Synthetic draft.'
    assert manager.dispatch_utterance('send', GOOD).detail['empty']
    manager._inject_fn.assert_called_once()


def test_send_alias_survives_custom_commit_word(monkeypatch, manager):
    sm.set_commit_phrase('done')
    assert sm.is_dictate_commit('done')
    assert sm.is_dictate_commit('send')
    assert not sm.is_dictate_commit('finish')
    assert not sm.is_dictate_commit('and')


@pytest.mark.parametrize('text', ['send the file', 'Please send', 'We should send.', 'Synthetic sentence. Send.'])
def test_send_inside_a_larger_utterance_stays_draft_text(manager, text):
    assert manager.dispatch_utterance(text, GOOD).kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == text
    manager._inject_fn.assert_not_called()


def test_send_failed_gate_never_enters_draft(manager):
    manager.dispatch_utterance('Synthetic draft.', GOOD)
    assert manager.dispatch_utterance('send', sm.UtteranceSignals(None, (1.0,), True)).kind == 'dictate_commit_refused'
    assert manager.dictate_pending_buffer == 'Synthetic draft.'
    manager._inject_fn.assert_not_called()


def test_short_send_uses_existing_corroborated_commit_gate(manager):
    manager.dispatch_utterance('Synthetic draft.', GOOD)
    assert manager.dispatch_utterance('send', sm.UtteranceSignals(False, (1.0,), True)).kind == 'dictate_committed'
    manager._inject_fn.assert_called_once()


@pytest.mark.parametrize('failure', ['focus', 'paste'])
def test_failed_send_keeps_draft_and_never_appends_control(manager, failure):
    manager.dispatch_utterance('Synthetic draft.', GOOD)
    if failure == 'focus':
        manager._foreground_hwnd_resolver = lambda: None
    else:
        manager._inject_fn.return_value = False
        manager._inject_fn.side_effect = None
    assert manager.dispatch_utterance('send', GOOD).kind != 'dictate_committed'
    assert manager.dictate_pending_buffer == 'Synthetic draft.'


@pytest.mark.parametrize('trigger', ['silence', 'key'])
@pytest.mark.parametrize('send_mode,label', [('staged', 'When I say send'), ('as_i_speak', 'As I speak')])
def test_settings_label_uses_actual_hands_free_mode_and_preserves_continuous_trigger(qapp, trigger, send_mode, label):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    config = _schema_defaults()
    config['continuous_commit_trigger'] = trigger
    config['hands_free'] = {'send_mode': send_mode, 'exact_commands': False}
    window = _SettingsWindow(_StubApp(config))
    try:
        combo = window._widgets['hands_free_send_mode']
        assert combo.currentText() == label
        assert combo.currentData() == send_mode
        updates = window._collect_destination_updates({})
        assert updates['continuous_commit_trigger'] == trigger
        assert updates['hands_free'] == config['hands_free']
        combo.setCurrentIndex(1 - combo.currentIndex())
        updates = window._collect_destination_updates({})
        assert updates['hands_free']['send_mode'] == combo.currentData()
        assert updates['continuous_commit_trigger'] == trigger
    finally:
        window.deleteLater()
