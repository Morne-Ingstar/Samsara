"""Owner result wording, speech decisions, restore aliases and guarded chain recovery."""
from pathlib import Path
from unittest.mock import Mock
import ast
import logging
import time
import pytest

from samsara import config_schema
from samsara.session_modes import outcome_chip
from samsara.ava_action_feedback import action_decision, action_scope, scoped_decision, speak_result
from samsara.result_messages import CLEAR_RESTORE_PHRASES, AVA_INTERPRET_HELP_LONG
from tests.test_clear_draft_443 import make_manager, GOOD
from tests.test_several_commands_399 import rig
from tests.test_ava_plain_requests_400 import rig as ava_rig


def test_requested_result_sentences():
    done = ['opened Notepad', 'Windows volume 30%']
    detail = dict(state='completed', chain_count=2, chain_done=done)
    assert outcome_chip('command_chain', detail) == ('Done: opened Notepad · Windows volume 30%', 'success')
    assert outcome_chip('ava_command_chain', detail) == ('Completed (Ava): opened Notepad · Windows volume 30%', 'success')
    assert outcome_chip('command_executed', dict(origin='model', state='completed', ava_done='paused Spotify')) == ('paused Spotify', 'success')
    assert outcome_chip('dictate_draft_cleared', {'voice_clear': True}) == ('Draft cleared - say "bring it back"', 'success')


def test_refused_chain_runs_nothing_and_explains_the_needed_yes(rig, monkeypatch):
    from samsara import execution_policy as policy
    executor,app,calls=rig
    seen=[]
    app._show_dispatch_outcome_chip=lambda outcome:seen.append(outcome_chip(outcome.kind,outcome.detail))
    monkeypatch.setattr(policy,'authorize',lambda *_a,**_kw:policy.NeedsConfirmation('confirmation_required'))
    result=executor.process_text('new tab and scroll down',force_commands=True)
    assert result.state.value=='rejected' and calls==[]
    assert seen[0][0].startswith('Say "')
    assert seen[0][0].endswith('on its own; it needs your yes.')


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
@pytest.mark.parametrize('outcome,perceptible', [('completed', True), ('completed', False), ('completed_chain',False), ('question', True), ('failed', True), ('confirmation_required', True)])
@pytest.mark.parametrize('reader', [False, True])
def test_speech_table(level, outcome, perceptible, reader):
    expected = 'skip' if level in ('silent', 'brief') and outcome == 'completed' and perceptible and not reader else 'speak'
    if outcome == 'completed_chain' and not reader and level in ('silent','brief'):
        expected = 'sound' if level == 'brief' else 'skip'
    assert action_decision(level, outcome, perceptible=perceptible, screen_reader=reader) == expected


def test_scope_restores_and_protects_questions_failures_and_unknown_effects():
    assert scoped_decision('silent', 'command') is None
    with action_scope('pause'):
        assert scoped_decision('silent', 'command') == 'skip'
        assert scoped_decision('brief', 'command') == 'skip'
        assert scoped_decision('full', 'command') == 'speak'
        assert scoped_decision('silent', 'confirmation') == 'speak'
        assert scoped_decision('silent', 'error') == 'speak'
        with action_scope('set reminder'):
            assert scoped_decision('silent', 'command') == 'speak'
        assert scoped_decision('silent', 'command') == 'skip'
    assert scoped_decision('silent', 'command') is None


@pytest.mark.parametrize('command', ['pause Spotify', 'pause music', 'open calculator', 'open settings'])
def test_visible_named_command_families_are_quiet(command):
    with action_scope(command):
        assert scoped_decision('brief', 'command') == 'skip'


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
@pytest.mark.parametrize('command', ['pause', 'open', 'set reminder'])
def test_real_ava_result_speech(ava_rig, level, command):
    from tests.test_ava_plain_requests_400 import proposal
    root = Path(__file__).resolve().parents[1]
    source = ast.parse((root/'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_resolve_command_miss')
    ns = dict(time=time, logger=logging.getLogger('463.production'))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),str(root/'dictation.py'),'exec'),ns)
    ava_rig.app.config = {'tts': {'feedback_level': level}}
    ava_rig.resolver.return_value = proposal(command)
    result = ns['_resolve_command_miss'](ava_rig.app, ava_rig.miss, 'please do this')
    assert result.kind == 'command_executed'
    assert bool(ava_rig.speech) == (level == 'full' or command == 'set reminder')
    assert outcome_chip(result.kind, result.detail)[0].startswith('Ava:') is False


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
@pytest.mark.parametrize('category,expected_low', [('command',False), ('error',True), ('confirmation',True), ('agent_response',False)])
def test_coordinator_applies_the_scope_before_its_length_cap(level, category, expected_low):
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, app, engine, _ = _make_coordinator()
    app.config.update(tts={'feedback_level':level,'enabled':True},command_mode={'tts_char_limit':1})
    app.command_mode_active=True
    with action_scope('pause'):
        coordinator.speak('Synthetic result that exceeds the length cap',category=category)
    assert engine.speak.called == (expected_low or level == 'full')


@pytest.mark.parametrize('level', ['silent','brief','full'])
def test_finished_ava_chain_preserves_brief_sound_and_full_speech(level):
    from tests.test_audio_coordinator import _make_coordinator
    coordinator,app,engine,_=_make_coordinator()
    app.config.update(tts={'feedback_level':level,'enabled':True})
    app.command_mode_active=False
    app._speak_session_notice=coordinator.speak
    speak_result(app,'Completed (Ava): opened Notepad · Windows volume 30%',
                 {'state':'completed','chain_count':2})
    assert engine.speak.called == (level=='full')
    if level=='brief':app.play_sound.assert_called_once_with('action_complete')
    elif level=='silent':app.play_sound.assert_not_called()


def test_explicit_tts_off_still_mutes_important_results():
    from tests.test_audio_coordinator import _make_coordinator
    coordinator,app,engine,_=_make_coordinator()
    app.config.update(tts={'feedback_level':'silent','enabled':False})
    app._speak_session_notice=coordinator.speak
    speak_result(app,'Synthetic failure',{'state':'failed'},'pause')
    engine.speak.assert_not_called()


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_real_music_plugin_waits_for_success_or_failure(monkeypatch, level, state):
    from types import SimpleNamespace
    from plugins.commands import music
    from samsara.commands import DispatchResult, DispatchState
    from samsara.ava_action_feedback import execute_action
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, app, engine, _ = _make_coordinator()
    app.config.update(tts={'feedback_level': level, 'enabled': True}, command_mode={'tts_char_limit': 1})
    app.command_mode_active = True
    app.audio_coordinator = coordinator
    handler = music._named_transport_handler(('synthetic.exe', 'Synthetic', 'Synthetic'), 'pause')
    monkeypatch.setattr(music, '_media_transport', lambda *_: DispatchResult(
        DispatchState(state), detail={'reason': 'Synthetic playback failed'}))
    executor = SimpleNamespace(_registry_entry=lambda _: SimpleNamespace(handler=handler, cmd_type='plugin'),
                               execute_canonical=lambda _cmd, host, **_kw: handler(host, ''))
    result = execute_action(executor, 'pause synthetic', app)
    assert result.state.value == state
    assert engine.speak.called == (state == 'failed' or level == 'full')
    if state == 'failed':
        assert engine.speak.call_args.args[0] == 'Synthetic playback failed'
    assert scoped_decision(level, 'command') is None


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_real_open_plugin_failure_is_spoken_despite_legacy_true(monkeypatch, level):
    from types import SimpleNamespace
    from plugins.commands import app_verbs
    from samsara.ava_action_feedback import execute_action
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, app, engine, _ = _make_coordinator()
    app.config.update(tts={'feedback_level': level, 'enabled': True})
    app.command_mode_active = False
    app.audio_coordinator = coordinator
    monkeypatch.setattr(app_verbs, 'do_open', lambda _: app_verbs.ActionResult.NOT_FOUND)
    executor = SimpleNamespace(_registry_entry=lambda _: SimpleNamespace(handler=app_verbs.handle_open, cmd_type='plugin'),
                               execute_canonical=lambda _cmd, host, **_kw: app_verbs.handle_open(host, 'Synthetic'))
    assert execute_action(executor, 'open', app) is True
    engine.speak.assert_called_once()
    assert engine.speak.call_args.args[0] == 'No app called Synthetic.'


def test_failed_command_acknowledgement_is_replayed_and_scope_restored():
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, app, engine, _ = _make_coordinator()
    app.config.update(tts={'feedback_level': 'silent', 'enabled': True})
    app.command_mode_active = False
    with pytest.raises(RuntimeError):
        with action_scope('pause'):
            coordinator.speak('Synthetic failure', category='command')
            raise RuntimeError('synthetic')
    engine.speak.assert_called_once()
    assert scoped_decision('silent', 'command') is None


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_failed_ava_action_without_plugin_speech_is_still_important(level):
    from types import SimpleNamespace
    from samsara.commands import DispatchResult, DispatchState
    from samsara.ava_action_feedback import execute_action
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, app, engine, _ = _make_coordinator()
    app.config.update(tts={'feedback_level': level, 'enabled': True})
    app.command_mode_active = False
    app._speak_session_notice = coordinator.speak
    executor = SimpleNamespace(_registry_entry=lambda _: None,
        execute_canonical=lambda *_a, **_kw: DispatchResult(DispatchState.FAILED, detail={'reason': 'Synthetic failure'}))
    execute_action(executor, 'pause', app)
    engine.speak.assert_called_once()


@pytest.mark.parametrize('phrase', CLEAR_RESTORE_PHRASES)
@pytest.mark.parametrize('seconds,expected', [(29.9, 'dictate_draft_recovered'), (30.0, 'dictate_recover_nothing')])
def test_all_restore_aliases_share_the_thirty_second_snapshot(make_manager, phrase, seconds, expected):
    manager, clock, _, injected = make_manager()
    manager._dictate_pending_buffer = 'Synthetic draft.'
    manager.dispatch_utterance('scratch everything', GOOD)
    clock.now += seconds
    assert manager.dispatch_utterance(phrase, GOOD).kind == expected
    if expected == 'dictate_draft_recovered':
        assert manager.dictate_pending_buffer == 'Synthetic draft.'
    injected.assert_not_called()


@pytest.mark.parametrize('phrase', CLEAR_RESTORE_PHRASES)
def test_restore_words_inside_prose_stay_dictation(make_manager, phrase):
    manager, _, _, _ = make_manager()
    assert manager.dispatch_utterance('She asked me to '+phrase+' tomorrow', GOOD).kind not in ('dictate_draft_recovered', 'dictate_recover_nothing')


def test_shared_restore_phrase_keeps_abort_recovery_and_prefers_current_voice_clear(make_manager):
    manager, _, _, _ = make_manager()
    manager._dictate_pending_buffer='Synthetic older draft.'
    manager._stash_recoverable_draft('cancel')
    manager.clear_pending_draft()
    outcome=manager.dispatch_utterance('restore the draft',GOOD)
    assert outcome.kind=='dictate_draft_recovered'
    assert outcome.detail['source']=='cancel'
    manager.dispatch_utterance('scratch everything',GOOD)
    outcome=manager.dispatch_utterance('restore the draft',GOOD)
    assert outcome.kind=='dictate_draft_recovered'
    assert outcome.detail['source']=='voice clear'


def test_restore_aliases_do_not_claim_paste_undo_or_command_lane(rig):
    executor, app, calls = rig
    for phrase in CLEAR_RESTORE_PHRASES:
        assert executor._registry_entry(phrase) is None
    assert 'undo' not in CLEAR_RESTORE_PHRASES


def test_help_is_in_the_existing_row_help_source():
    assert config_schema.SETTINGS_SCHEMA['ava_command_session.interpret_misses']['help']['why'] == AVA_INTERPRET_HELP_LONG
    short = (Path(__file__).resolve().parents[1]/'samsara/ui/settings/ava_page.py').read_text(encoding='utf-8')
    assert 'After a normal command miss, Ava may choose one available command.' in short
    assert 'make the music quieter' in AVA_INTERPRET_HELP_LONG


@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_connectors_escape_arguments_and_both_displays_use_ava_token(qapp, palette):
    from PySide6.QtGui import QColor, QFontMetrics
    from samsara.ui import theme
    from samsara.ui.result_message_rendering import result_html, result_width
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import _FeedbackBand
    from samsara.live_surface.model import Notice, NoticeKind
    previous = theme.active_theme()
    theme.set_theme(palette)
    indicator = ListeningIndicator()
    band = _FeedbackBand(None, 'proofFeedback')
    try:
        label = 'Completed (Ava): opened <Notepad> · Windows volume 30%'
        html = result_html(label)
        assert '&lt;Notepad&gt;' in html and '125%' in html and 'font-weight:900' in html
        assert '&amp;' in result_html(label, connector='ampersand')
        band.set_notice(Notice(NoticeKind.SUCCESS, label, None))
        band.restyle()
        assert band.fill == QColor(theme.AVA)
        assert band.label.text() == result_html(label, ink=band.ink.name())
        assert indicator._chip_width(label) >= QFontMetrics(indicator._chip_font()).horizontalAdvance(label) + 24
        band.set_notice(Notice(NoticeKind.SUCCESS, '<plain & literal>', None))
        assert band.label.text() == '<plain & literal>'
    finally:
        indicator.destroy(); band.hide(); theme.set_theme(previous)


@pytest.mark.parametrize('joiner', ['and', 'then', 'and then', 'in', 'an', 'end', 'n', 'than'])
def test_heard_joiner_runs_two_complete_commands(rig, joiner):
    executor, _, calls = rig
    result = executor.process_text(f'open notepad {joiner} volume 30', force_commands=True)
    assert result.state.value == 'completed'
    assert [name for name, _ in calls] in (['open', 'set volume'], ['open notepad', 'set volume'])
    assert calls[0][1] in ('', 'notepad')
    assert result.detail['chain_count'] == 2


@pytest.mark.parametrize('text', ['please open imaginary app then purple monkey', 'search for cats and dogs'])
def test_original_joiner_misses_do_not_build_recovery_candidates(monkeypatch, text):
    from samsara import grammar
    from samsara.chain_recovery import parse_command_chain
    parser = Mock(return_value=[])
    monkeypatch.setattr(grammar, 'parse', parser)
    monkeypatch.setattr(grammar, '_catalog_index', Mock(side_effect=AssertionError('recovery must not run')))
    assert parse_command_chain(text, []) == []
    parser.assert_called_once_with(text, [])


@pytest.mark.parametrize('text', ['open notepad in volume 30', 'new tab in scroll down'])
def test_unforced_dictation_never_recovers_a_joiner(rig, monkeypatch, text):
    executor, app, calls = rig
    from samsara.chain_recovery import parse_command_chain
    spy = Mock(wraps=parse_command_chain)
    monkeypatch.setattr('samsara.chain_recovery.parse_command_chain', spy)
    executor.process_text(text, force_commands=False)
    spy.assert_not_called()
    assert not any(name == 'set volume' for name, _ in calls)


@pytest.mark.parametrize('text', ['new tab in purple monkey', 'volume down in the street', 'search for cats in volume 30', 'open Cats In volume 30'])
def test_no_chain_from_incomplete_steps_or_argument_words(rig, text):
    executor, _, calls = rig
    result = executor.process_text(text, force_commands=True)
    assert 'chain_count' not in result.detail
    assert not any(name == 'set volume' for name, _ in calls)


def test_rejected_prefix_cannot_reopen_its_ignored_words(rig, monkeypatch):
    executor, _, calls = rig
    entry = next(e for e in executor._matcher._sorted if e.phrase == 'open notepad')
    monkeypatch.setattr(executor._matcher, 'match', lambda *_: (entry, 'in purple monkey'))
    result = executor.process_text('open notepad in purple monkey', force_commands=True)
    assert result.state.value == 'miss'
    assert not calls
