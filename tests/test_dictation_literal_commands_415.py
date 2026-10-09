"""415: prose stays text; literal commands and explicit command lanes stay distinct."""
import copy
import json
import os
import sys
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import execution_policy as policy
from samsara.command_catalog import build_catalog
from samsara.command_registry import DispatchState as State
from tests.test_several_commands_399 import rig as live_rig
from tests.test_commands_only_key_391 import _app as hold_app, _run as release_hold

def adapted_hold_app(tmp_path, monkeypatch, text, **kwargs):
    from samsara.config_defaults import DEFAULTS
    from tests import test_commands_only_key_391 as hold
    module = sys.modules['dictation']
    namespace = vars(module)
    names = ('_stop_recording_impl', '_take_recording_ownership',
             'get_transcription_params', '_build_hotkey_transcribe_params', '_decode_hotkey_audio',
             '_resolve_command_miss', '_handle_session_dispatch_outcome',
             '_show_dispatch_outcome_chip', '_show_outcome_chip',
             '_show_hold_pending_chip', '_resolve_hold_chip')
    for name in names:
        monkeypatch.setitem(namespace, name, getattr(module.DictationApp, name))
    # Bind the adapter's original method bodies; never compile dictation source.
    monkeypatch.setitem(namespace, 'thread_registry', SimpleNamespace(spawn=lambda name, fn, **kw: fn()))
    monkeypatch.setitem(namespace, 'flight_recorder', SimpleNamespace(record=Mock()))
    monkeypatch.setitem(namespace, 'increment_command_count', Mock())
    for name in ('benchmark_store', 'voice_memo', 'smart_correct', 'time'):
        monkeypatch.setitem(namespace, name, namespace.get(name))
    config = {}
    for key, value in DEFAULTS.items():
        node = config
        *parents, leaf = key.split('.')
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = copy.deepcopy(value)
    config.setdefault('command_mode', {})['command_matching_enabled'] = False
    config.setdefault('ava_command_session', {})['interpret_misses'] = False
    monkeypatch.setattr(hold, '_production', lambda: (namespace, config))
    return hold_app(tmp_path, monkeypatch, text, **kwargs)


ROOT = Path(__file__).resolve().parents[1]
KEY = 'dictation_literal_commands_only'
# Fixed synthetic prose authored for 415; never user dictation.
OWN = ['open the door when you get here', 'open notepad before you tell the story', 'open firefox is what the teacher wrote on the board', 'open your heart to the possibility of change', 'close the window before it rains', 'close notepad after the last paragraph is finished', 'close the deal before the end of the week', 'close firefox is the example in the manual', 'bring the washing inside before the storm', 'bring notepad to the meeting tomorrow', 'bring the children home before dinner', 'bring everything you need for the picnic', 'switch to a lighter jacket when spring arrives', 'switch to firefox when you explain the example', 'switch to another subject before he gets angry', 'switch to tab gardening when planning the weekend is the phrase she dictated', 'focus on the problem before deciding what to do', 'focus notepad on the next chapter is an odd instruction', 'focus the camera on the birds outside', 'focus on what matters to your family', 'play it by ear until Friday', 'play the part with a little more feeling', 'play music by the river is our plan for Saturday', 'play firefox is a phrase that makes no sense', 'search for a better way to explain the problem', 'search the garden before you call the neighbours', 'search for lost time is the title of her essay', 'find the courage to tell him what happened', 'find tab gardening before dinner is what she said', 'find the keys before leaving for work']
CORPUS = json.loads((ROOT / 'tools/plain_speech_corpus.json').read_text(encoding='utf-8'))
STRESS = [r['text'] for r in CORPUS['utterances'] if r['tag'] != 'exact'] + OWN
PROSE = [r['text'] for r in CORPUS['utterances'] if r['tag'] == 'dictation'] + OWN


@pytest.fixture
def rig(live_rig):
    executor, app, calls = live_rig
    app.config['command_mode'] = {KEY: True}
    records = [s.to_dict() for s in build_catalog(executor)]
    executor._matcher.set_enabled_packs({r['pack'] for r in records})
    return executor, app, calls


def test_70_prose_rows_and_190_stress_rows_never_capture_arguments(rig, monkeypatch):
    executor, app, calls = rig
    assert len(PROSE) == 70 and len(STRESS) == 190
    authorize = Mock(wraps=policy.authorize)
    monkeypatch.setattr(policy, 'authorize', authorize)
    for text in PROSE:
        calls.clear(); authorize.reset_mock()
        result = executor.process_text(text)
        assert tuple(result) == (text, False), text
        assert result.state is State.MISS and not calls, text
        authorize.assert_not_called()
    # Replay BOTH switches in the forced lane, preserving canonical proposal/args,
    # including refusals. Policy remains real; only OS effects in the rig are inert.
    before = []
    for setting in (False, True):
        app.config['command_mode'][KEY] = setting
        measured = []
        for text in STRESS:
            calls.clear()
            result = executor.process_text(text, force_commands=True)
            measured.append((result.state, result.result, calls[:]))
        if not setting:
            before = measured
        else:
            assert measured == before
    for text in STRESS:
        calls.clear()
        result = executor.process_text(text)
        if calls:
            entry = executor._registry_entry(result.result)
            assert not executor._takes_argument(entry), text
            assert executor.find_exact_command(text) == entry.phrase, text


@pytest.mark.parametrize('literal', ['copy', 'COPY!', 'open notepad', 'volume down'])
def test_whole_literal_commands_still_execute(rig, literal):
    executor, app, calls = rig
    assert executor.process_text(literal).state is State.COMPLETED
    assert len(calls) == 1


@pytest.mark.parametrize('text', ['copy please', 'volume down now', 'open notepad and read this',
                                  'please open notepad and read this', 'open', 'find tab',
                                  'send 415 to claude', 'ask Spotify for jazz', 'open screen'])
def test_no_filler_shorter_prefix_phonetic_or_routing_bypass(rig, monkeypatch, text):
    executor, app, calls = rig
    app.config['smart_actions'] = {'enabled': True}
    route = Mock(side_effect=AssertionError('dictation reached routing fallback'))
    monkeypatch.setattr(executor, '_try_smart_actions_route', route)
    execute = Mock(side_effect=AssertionError('dictation reached effects'))
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    result = executor.process_text(text)
    assert tuple(result) == (text, False)
    assert result.state is State.MISS and not calls
    execute.assert_not_called(); route.assert_not_called()


@pytest.mark.parametrize('old', [False, True])
@pytest.mark.parametrize('lane', ['ordinary', 'forced', 'active', 'prefix', 'custom-prefix'])
def test_arguments_only_in_explicit_lanes_unless_old_behavior_chosen(rig, monkeypatch, old, lane):
    executor, app, calls = rig
    app.config['command_mode'][KEY] = not old
    text = 'close firefox'
    forced = lane == 'forced'
    app.command_mode_active = lane == 'active'
    if lane != 'ordinary':
        app.command_matching_enabled = False
    if lane == 'prefix':
        text = 'command ' + text
    if lane == 'custom-prefix':
        app.config['intent'] = {'command_prefix': 'do this'}
        text = 'do this ' + text
    result = executor.process_text(text, force_commands=forced)
    if lane == 'ordinary' and not old:
        assert tuple(result) == (text, False) and not calls
    else:
        assert result.state is State.COMPLETED
        assert calls == [('close', 'firefox')]


def test_reminders_cannot_bypass_the_literal_gate(rig, monkeypatch):
    executor, app, calls = rig
    app.play_sound = Mock()
    app.notification_manager = SimpleNamespace(
        parse_remind_command=Mock(return_value=(5, 'stretch')),
        add_quick_reminder=Mock())
    text = 'remind me in 5 minutes to stretch'
    assert tuple(executor.process_text(text)) == (text, False)
    app.notification_manager.parse_remind_command.assert_not_called()
    app.notification_manager.add_quick_reminder.assert_not_called()
    assert executor.process_text(text, force_commands=True).state is State.COMPLETED
    app.notification_manager.add_quick_reminder.assert_called_once_with(5, 'stretch')


def test_whole_session_controls_and_pending_answers_keep_ownership(rig, monkeypatch):
    executor, app, calls = rig
    app._config_lock = threading.Lock(); app.save_config = Mock()
    assert executor.process_text('command mode off').state is State.COMPLETED
    assert app.command_matching_enabled is False
    assert executor.process_text('command mode on').state is State.COMPLETED
    monkeypatch.setattr(policy, 'pending_operation', lambda: object())
    answer = Mock(return_value='approved')
    monkeypatch.setattr(policy, 'answer_pending', answer)
    assert executor.process_text('yes').result == 'pending_reply'
    answer.assert_called_once_with(app, 'yes')


@pytest.mark.parametrize('mode', ['hold', 'toggle'])
@pytest.mark.parametrize('old', [False, True])
def test_hold_and_tap_release_type_the_complete_sentence(tmp_path, monkeypatch, mode, old):
    app, spies = adapted_hold_app(tmp_path, monkeypatch, 'please open notepad and read this',
                          matching=True, command_key=False)
    app.config['mode'] = mode
    app.config['command_mode'][KEY] = not old
    release_hold(app)
    assert spies.canonical.call_count == spies.resolver.call_count == 0
    assert app._paste_preserving_clipboard.call_args.args[0] == 'Please open notepad and read this. '


def test_command_prefix_on_hold_uses_command_lane(tmp_path, monkeypatch):
    app, spies = adapted_hold_app(tmp_path, monkeypatch, 'command scroll down',
                          matching=False, command_key=False)
    release_hold(app)
    assert spies.hand.call_count == 1
    app._paste_preserving_clipboard.assert_not_called()


@pytest.mark.parametrize('old', [False, True])
def test_hands_free_probe_rejects_targets_before_forced_dispatch(rig, monkeypatch, old):
    executor, app, calls = rig
    app.config['command_mode'][KEY] = not old
    app.command_mode_active = True  # A latched session is armed even in Dictate.
    cls = sys.modules['dictation'].DictationApp  # maintained gate adapter, no bootstrap import
    for name in ('_probe_media_transport', '_probe_hands_free_command'):
        setattr(app, name, MethodType(getattr(cls, name), app))
    for text in ('close firefox', 'open', 'find tab', 'window switch bravo'):
        match = app._probe_hands_free_command(text)
        if not old:
            assert match is None, text
    match = app._probe_hands_free_command('command close firefox')
    assert match is not None and match.dispatch_text == 'command close firefox'
    assert executor.process_text(match.dispatch_text, force_commands=True).state is State.COMPLETED
    assert calls == [('close', 'firefox')]


def test_settings_default_save_and_help(qapp, monkeypatch):
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    from samsara import autostart
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    app = _StubApp({'command_mode': {'enabled': False, 'foreign_setting': 'preserved'}})
    window = _SettingsWindow(app)
    try:
        widget = window._widgets['cmd_dictation_literal_only']
        row = window._rows_by_widget['cmd_dictation_literal_only']
        assert widget.isChecked()
        assert row.page_name == 'Dictation'
        assert row.config_keys == ('command_mode.' + KEY,)
        assert SETTINGS_SCHEMA['command_mode.' + KEY]['default'] is True
        widget.setChecked(False)
        save = next(fn for saves in window._source_saves.values() for fn in saves
                    if '_build_modes_tab' in fn.__qualname__)
        updates = save({})
        assert updates['command_mode'][KEY] is False
        assert updates['command_mode']['foreign_setting'] == 'preserved'
        widget.setChecked(True)
        app.config.update(save({}))
        assert app.config['command_mode'][KEY] is True
        # Evidence is optional output, never an input required by exported tests.
        if os.environ.get('GATE_RESULT'):
            window.show_tab('Dictation'); window.resize(1080, 920); window.show()
            qapp.processEvents()
            row.help.click(); qapp.processEvents()
            row.grab().save(str(Path(os.environ['GATE_RESULT']).parent / 'settings-415.png'))
            row.help_panel.grab().save(str(Path(os.environ['GATE_RESULT']).parent / 'settings-415-help.png'))
    finally:
        window.close(); window.deleteLater()


def test_latched_dictate_state_is_not_the_command_lane(rig, monkeypatch):
    from samsara.session_modes import SessionMode
    executor, app, calls = rig
    app.command_mode_active = True
    app._session_mode_manager = SimpleNamespace(mode=SessionMode.DICTATE)
    parse = Mock(side_effect=AssertionError('Dictate must not parse command chains'))
    monkeypatch.setattr(executor, '_grammar_chain', parse)
    for text in ('close firefox', 'new tab and scroll down'):
        assert tuple(executor.process_text(text)) == (text, False)
    parse.assert_not_called()
    assert not calls
    app._session_mode_manager.mode = SessionMode.COMMAND
    assert executor.process_text('close firefox').state is State.COMPLETED
    assert calls == [('close', 'firefox')]


def test_hands_free_stress_and_prose_stay_in_dictate(rig, monkeypatch):
    from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals, CommandDispatchResult
    executor, app, calls = rig
    cls = sys.modules['dictation'].DictationApp
    for name in ('_probe_media_transport', '_probe_hands_free_command'):
        setattr(app, name, MethodType(getattr(cls, name), app))
    def dispatch(text):
        result = executor.process_text(text, force_commands=True)
        return CommandDispatchResult(matched=result.state is not State.MISS,
                                     phrase=result.result, state=result.state.value)
    manager = SessionModeManager(
        abort_phrases=['cancel dictation'], foreground_exe_resolver=lambda: 'chrome.exe',
        foreground_hwnd_resolver=lambda: 10, inject_fn=Mock(return_value=True),
        remove_chars_fn=Mock(), command_dispatch_fn=dispatch, agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True, hands_free_command_probe_fn=app._probe_hands_free_command)
    app._session_mode_manager = manager; app.command_mode_active = True
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
    for text in STRESS:
        manager.reset(SessionMode.DICTATE); calls.clear()
        outcome = manager.dispatch_utterance(text, signals)
        if text in PROSE:
            assert outcome.kind == 'dictate_staged', text
            assert not calls and text in manager.dictate_pending_buffer, text
        for phrase, _argument in calls:
            assert not executor._takes_argument(executor._registry_entry(phrase)), text
    manager.reset(SessionMode.COMMAND); calls.clear()
    manager.dispatch_utterance('close firefox', signals)
    assert calls == [('close', 'firefox')]


def test_real_user_alias_is_a_whole_literal_command(rig):
    executor, app, calls = rig
    executor.commands['synthetic user command'] = {
        'type': 'hotkey', 'keys': ['ctrl', 's'], 'aliases': ['synthetic alias']}
    executor.rebuild_matcher()
    assert executor.accepts_dictation_command('synthetic alias', app)
    assert executor.process_text('synthetic alias').state is State.COMPLETED
    assert calls == [('synthetic user command', '')]
    calls.clear()
    assert tuple(executor.process_text('synthetic alias in a sentence')) == ('synthetic alias in a sentence', False)
    assert not calls
