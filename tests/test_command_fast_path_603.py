"""603: bounded final-audio recognition, literal safety and existing policy.

App methods run only with the ordinary no-bootstrap gate adapter. No model,
microphone, real OS input, live profile, or recorded user text is needed.
"""
from types import SimpleNamespace
from unittest.mock import Mock
import os
from pathlib import Path
import threading

import numpy as np
import pytest

from samsara import execution_policy as policy
from samsara.command_catalog import matches_short_command
from samsara.command_registry import DispatchState as State
from samsara.command_scope import MatchContext
from samsara.config_defaults import DEFAULTS
from samsara.config_schema import SETTINGS_SCHEMA
from tests.test_several_commands_399 import rig


@pytest.mark.parametrize('text', [
    'new tab', 'NEW TAB!', 'scroll down',
    'new tab then scroll down',
    'new tab then scroll down then volume up',
])
def test_only_complete_live_literals_are_accepted(rig, text):
    executor, _app, calls = rig
    assert matches_short_command(text, executor)
    assert calls == []  # Classification must have no command effects.


@pytest.mark.parametrize('text', [
    '', 'new', 'new tab before the meeting', 'please new tab',
    'open notepad', 'find tab github', 'purple monkey',
    'new tab then purple monkey', 'new tab then',
    'new tab then scroll down then volume up then volume down',
    'new tab and scroll down',
])
def test_prefixes_arguments_and_partial_chains_cannot_use_fast_result(rig, text):
    executor, _app, calls = rig
    assert not matches_short_command(text, executor)
    assert not calls


def test_catalogue_alias_and_disabled_pack(rig):
    executor, _app, _calls = rig
    entry, _ = executor._matcher.match('top of page')
    assert entry.aliases
    alias = entry.aliases[0]
    assert matches_short_command(alias, executor)
    executor._matcher.set_enabled_packs({'definitely_no_command_pack'})
    assert not matches_short_command(alias, executor)


def test_scope_is_checked_against_live_registry(rig, monkeypatch):
    from samsara.command_scope import Scope
    executor, _app, _calls = rig
    matcher = executor._matcher
    entry, _ = matcher.match('new tab')
    monkeypatch.setattr(entry, 'scope', Scope(apps=('chrome.exe',)))
    monkeypatch.setattr(matcher, '_has_scoped', True)
    monkeypatch.setattr(matcher, '_needs_foreground', True)
    monkeypatch.setattr(matcher, '_context_provider', lambda: MatchContext.for_app('editor.exe'))
    assert not matches_short_command('new tab', executor)
    monkeypatch.setattr(matcher, '_context_provider', lambda: MatchContext.for_app('chrome.exe'))
    assert matches_short_command('new tab', executor)


def test_chain_off_switch_is_respected(rig):
    executor, _app, _calls = rig
    assert not matches_short_command('new tab then scroll down', executor, allow_chain=False)
    assert matches_short_command('new tab', executor, allow_chain=False)


@pytest.fixture
def decode_app(rig):
    # The approved gate installs the whole class without bootstrapping the app.
    from dictation import DictationApp
    executor, _app, _calls = rig
    app = DictationApp.__new__(DictationApp)
    app.config = {'command_mode': {'short_decode_enabled': True}}
    app.model_rate = 16000
    app.model_lock = threading.Lock()
    app.command_executor = executor
    return app


def _result(text, *, marker=None):
    segment = SimpleNamespace(text=text, avg_logprob=-0.1, no_speech_prob=0.01,
                              compression_ratio=1.0)
    return iter([segment]), SimpleNamespace(language='en', marker=marker)


def test_matched_short_final_audio_uses_one_decode_with_original_signals(decode_app, tmp_path):
    from samsara.stage_timing import StageTiming, set_current_stage_timing, reset_current_stage_timing
    app = decode_app
    audio = np.zeros(16000, dtype=np.float32)
    params = {'beam_size': 5, 'language': 'en', 'vad_filter': False,
              'initial_prompt': 'fixed synthetic vocabulary', 'no_speech_threshold': 0.4}
    original = dict(params)
    marker = object()
    def transcribe(data, **kwargs):
        assert app.model_lock.locked()
        assert data is audio
        assert kwargs['no_speech_threshold'] == 0.4
        return _result('New tab.', marker=marker)
    app.model = SimpleNamespace(transcribe=Mock(side_effect=transcribe))
    timing = StageTiming.create('handsfree', 'command', enabled=True, home_dir=tmp_path)
    timing.mark('transcribe_start', beam=5)
    token = set_current_stage_timing(timing)
    try:
        segments, info = app._decode_session_audio(audio, params, command_lane=True)
    finally:
        reset_current_stage_timing(token)
    assert timing.beam == 1
    assert info.marker is marker
    assert segments[0].avg_logprob == -0.1
    assert segments[0].no_speech_prob == 0.01
    assert app.model.transcribe.call_count == 1
    assert app.model.transcribe.call_args.kwargs['beam_size'] == 1
    assert params == original


@pytest.mark.parametrize('candidate', [
    'new tab before the meeting', 'purple monkey', 'new tab then purple monkey', '',
])
def test_unmatched_short_pass_redecodes_complete_original_audio(decode_app, candidate):
    app = decode_app
    audio = np.zeros(16000, dtype=np.float32)
    params = {'beam_size': 3, 'temperature': [0.0, 0.2], 'language': 'en'}
    app.model = SimpleNamespace(transcribe=Mock(side_effect=[
        _result(candidate), _result('A synthetic sentence remains complete.', marker='fallback')]))
    segments, info = app._decode_session_audio(audio, params, command_lane=True)
    assert ''.join(s.text for s in segments) == 'A synthetic sentence remains complete.'
    assert info.marker == 'fallback'
    assert app.model.transcribe.call_count == 2
    for call in app.model.transcribe.call_args_list:
        assert call.args[0] is audio
    assert app.model.transcribe.call_args.kwargs == params


@pytest.mark.parametrize('lazy', [False, True])
def test_failed_short_pass_falls_back_including_lazy_iterator(decode_app, lazy):
    app = decode_app
    def broken():
        yield SimpleNamespace(text='new tab')
        raise RuntimeError('synthetic decoder failure')
    first = (broken(), SimpleNamespace()) if lazy else RuntimeError('synthetic decoder failure')
    app.model = SimpleNamespace(transcribe=Mock(side_effect=[first, _result('Fallback text.')]))
    segments, _ = app._decode_session_audio(np.zeros(16000), {'beam_size': 5}, command_lane=True)
    assert ''.join(s.text for s in segments) == 'Fallback text.'
    assert app.model.transcribe.call_count == 2


@pytest.mark.parametrize('lane,enabled,length,beam', [
    (False, True, 16000, 5),   # DICTATE and AVA always use the configured decoder.
    (True, False, 16000, 5),
    (True, True, 64001, 5),   # Long recordings never incur a speculative pass.
    (True, True, 16000, 1),   # Fast preset already has the cheapest beam.
])
def test_ineligible_audio_or_lane_uses_exactly_original_decode(decode_app, lane, enabled, length, beam):
    app = decode_app
    app.config['command_mode']['short_decode_enabled'] = enabled
    params = {'beam_size': beam, 'language': None, 'initial_prompt': 'synthetic context'}
    app.model = SimpleNamespace(transcribe=Mock(return_value=_result('Unchanged prose.')))
    app._decode_session_audio(np.zeros(length), params, command_lane=lane)
    assert app.model.transcribe.call_count == 1
    assert app.model.transcribe.call_args.kwargs == params


def test_absent_opt_in_preserves_default_decode(decode_app):
    app = decode_app
    app.config = {}
    app.model = SimpleNamespace(transcribe=Mock(return_value=_result('new tab')))
    app._decode_session_audio(np.zeros(16000), {'beam_size': 5}, command_lane=True)
    assert app.model.transcribe.call_count == 1
    assert app.model.transcribe.call_args.kwargs == {'beam_size': 5}


def test_415_prose_corpus_cannot_pass_short_command_filter(rig):
    from tests.test_dictation_literal_commands_415 import PROSE
    executor, _app, calls = rig
    assert not any(matches_short_command(text, executor) for text in PROSE)
    assert not calls


def test_short_result_still_uses_existing_chain_policy(decode_app, rig, monkeypatch):
    executor, app, calls = rig
    decode_app.command_executor = executor
    decode_app.model = SimpleNamespace(transcribe=Mock(return_value=_result(
        'new tab then scroll down then volume up')))
    segments, _ = decode_app._decode_session_audio(np.zeros(16000), {'beam_size': 3}, command_lane=True)
    assert not calls
    authorize = Mock(wraps=policy.authorize)
    monkeypatch.setattr(policy, 'authorize', authorize)
    result = executor.process_text(''.join(s.text for s in segments), force_commands=True)
    assert result.state is State.COMPLETED
    assert [phrase for phrase, _ in calls] == ['new tab', 'scroll down', 'volume up']
    assert authorize.call_count >= 3
    assert all(call.args[0].route is policy.Route.GRAMMAR for call in authorize.call_args_list)


def test_destructive_chain_keeps_existing_confirmation_refusal(rig):
    executor, _app, calls = rig
    result = executor.process_text('volume down then scratch everything', force_commands=True)
    assert result.state is State.REJECTED
    assert result.detail['reason'] == 'confirmation_required'
    assert not calls


def test_config_defaults_keep_today_endpoint_and_experimental_decode_off():
    row = SETTINGS_SCHEMA['command_mode.utterance_silence_s']
    assert row['default'] == DEFAULTS['command_mode.utterance_silence_s'] == 1.0
    assert row['min'] <= 0.45 <= row['max']
    assert DEFAULTS['command_mode.short_decode_enabled'] is False


def test_existing_pause_control_saves_045_without_changing_dictation(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    app = _StubApp({'command_mode': {'utterance_silence_s': 1.0,
                                   'dictate_utterance_silence_s': 0.9,
                                   'short_decode_enabled': True}})
    window = _SettingsWindow(app)
    try:
        spin = window._widgets['cmd_utterance_silence']
        assert spin.value() == 1.0
        spin.setValue(0.45)
        save = next(fn for saves in window._source_saves.values() for fn in saves
                    if '_build_modes_tab' in fn.__qualname__)
        changed = save({})['command_mode']
        assert changed['utterance_silence_s'] == 0.45
        assert changed['dictate_utterance_silence_s'] == 0.9
        assert changed['short_decode_enabled'] is True
        if os.environ.get('GATE_RESULT'):
            row = window._rows_by_widget['cmd_utterance_silence']
            window.show_tab('Dictation'); window.show(); qapp.processEvents()
            row.help.click(); qapp.processEvents()
            row.grab().save(str(Path(os.environ['GATE_RESULT']).parent / 'command-pause-control.png'))
            row.help_panel.grab().save(str(Path(os.environ['GATE_RESULT']).parent / 'command-pause-help.png'))
    finally:
        window.close(); window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
