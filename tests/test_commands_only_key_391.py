"""391: real release/decode/dispatch methods, synthetic speech and fake OS seams.

Never imports the desktop dictation bootstrap. The production registry,
executor, resolver boundary, outcome mapper and hold watchdog run unchanged.
"""
from samsara.log import describe_text, safe_exception
import ast
import collections
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace, MethodType
from unittest.mock import MagicMock

import numpy as np
import pytest

from samsara import (ava_command_session, commands, config_defaults, diagnostics,
                     execution_policy, injection_safety, languages, outcome_ring)
from samsara import constants
from samsara.audio_tools import (_apply_retry_on_suspected_loss, _fade_edges,
                                 _HotkeyDecodeResult, _split_audio_at_silences)
from samsara.cleanup import clean_text
from samsara.hallucination_filter import SignoffContext, is_signoff_hallucination
from samsara.key_macros import get_default_macro_config
from samsara.notifications import get_default_notification_config
from samsara.plugin_commands import command
from samsara.session_modes import normalize_utterance, is_self_hear_capture, outcome_chip
from samsara.transcript_gates import _apply_segment_quality_gates

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_ast_plugin_module_entries(monkeypatch):
    # The shared conftest isolates _REGISTRY, but decorator registration also
    # updates this persistent discovery cache. Keep our AST handler test-local.
    from samsara import plugin_commands
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES',
        {name: dict(entries) for name, entries in plugin_commands._MODULE_ENTRIES.items()})


def _production():
    source = ROOT / 'dictation.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    names = {'_stop_recording_impl', '_take_recording_ownership',
             'get_transcription_params', '_build_hotkey_transcribe_params', '_decode_hotkey_audio',
             '_resolve_command_miss', '_handle_session_dispatch_outcome',
             '_show_dispatch_outcome_chip', '_show_outcome_chip',
             '_show_hold_pending_chip', '_resolve_hold_chip'}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names
    constants_wanted = {'_RecordingOwnership', '_FADE_MS', '_GATE_HEAD_GRACE_CLICK_PAD_MS',
                        '_LONG_DECODE_CEILING_S', '_LOGPROB_THRESHOLD', '_NO_SPEECH_THRESHOLD',
                        '_REPEAT_BLACKLIST_NAMES', '_REPEAT_BLACKLIST_TYPES'}
    assignments = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in constants_wanted for t in n.targets)]
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_is_repeat_blacklisted']
    namespace = dict(globals(), **vars(constants), _languages=languages,
                     describe_text=describe_text, safe_exception=safe_exception,
                     logger=logging.getLogger('391.production'),
                     flight_recorder=SimpleNamespace(record=MagicMock()),
                     thread_registry=SimpleNamespace(spawn=lambda name, fn, **kw: fn()),
                     increment_command_count=MagicMock())
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
             *assignments, *helpers, *methods]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), 'exec'), namespace)
    load = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'load_config')
    defaults = next(n.value for n in load.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == 'default_config' for t in n.targets))
    default_ns = dict(vars(constants), Path=Path, get_default_macro_config=get_default_macro_config,
                      get_default_notification_config=get_default_notification_config)
    config = eval(compile(ast.Expression(defaults), str(source), 'eval'), default_ns)
    return namespace, copy.deepcopy(config)


def _app(tmp_path, monkeypatch, text, *, matching=False, command_key=True,
         interpret=False, ava_enabled=False, timeout=False):
    namespace, config = _production()
    assert config['command_mode']['command_matching_enabled'] is False
    assert not config.get('ava_command_session', {}).get('interpret_misses', False)
    config['command_mode']['command_matching_enabled'] = matching
    config['recording_tail_ms'] = 0  # exclude capture timing; no device is opened
    config.setdefault('ava_command_session', {})['interpret_misses'] = interpret
    if ava_enabled:
        config['ava_command_session']['enabled'] = True
        config.setdefault('ava', {})['provider_policy'] = 'local'
    audio = np.full(32000, .1, dtype=np.float32)
    model = MagicMock()
    model.transcribe.return_value = ([SimpleNamespace(text=text, avg_logprob=-.1,
        no_speech_prob=.01, compression_ratio=1.1, temperature=0.0, start=0.0, end=2.0)],
        SimpleNamespace(language='en', language_probability=.99))
    app = SimpleNamespace(config=config, command_matching_enabled=matching,
        command_mode_recording=command_key, ava_mode_recording=False, command_mode_active=False,
        recording=True, hotkey_pressed=False, _ace_dictation_active=True,
        _streaming_session=None, model_rate=16000, model=model, model_lock=threading.Lock(),
        _last_recording_earcon_ms=0, _tutorial_hooks={}, _ava_interpret_request=0,
        _command_mode_miss_count=0, listening_indicator=MagicMock(),
        voice_training_window=SimpleNamespace(apply_corrections=lambda s:s,
            get_initial_prompt=lambda **kw:''),
        _dictation_consumer=SimpleNamespace(drain=lambda:audio, drain_after_release=lambda **kw:audio),
        _live_surface_hold_parking_enabled=lambda:False,
        _buffer_should_skip_decode=lambda *a,**kw:False,
        _filter_dictation_language=lambda s,*a,**kw:s,
        _schedule_ui=lambda fn,*a,**kw:fn(*a,**kw),
        _release_icon_chase=MagicMock(), _update_tray_tooltip=MagicMock(), play_sound=MagicMock(),
        add_to_history=MagicMock(), _log_history=MagicMock(), _notify_main_window=MagicMock(),
        process_transcription=lambda s:s, _apply_verbatim_if_active=lambda s:None,
        _apply_formatting_tokens=lambda s:s, _paste_preserving_clipboard=MagicMock(return_value=True),
        _touch_session_activity=MagicMock())
    app.set_app_state=lambda **kw:[setattr(app,k,v) for k,v in kw.items()]
    for name in ('_stop_recording_impl','_take_recording_ownership','get_transcription_params',
                 '_build_hotkey_transcribe_params','_decode_hotkey_audio','_resolve_command_miss',
                 '_handle_session_dispatch_outcome','_show_dispatch_outcome_chip',
                 '_show_outcome_chip','_show_hold_pending_chip','_resolve_hold_chip'):
        setattr(app,name,MethodType(namespace[name],app))
    # Execute the actual default scroll command's decorator/body; replace only its OS hand.
    scroll_hand=MagicMock(return_value=True)
    scroll_tree=ast.parse((ROOT/'plugins/commands/scroll.py').read_text(encoding='utf-8'))
    scroll_nodes=[n for n in scroll_tree.body if isinstance(n,ast.FunctionDef) and n.name in {'_clicks','scroll_down'}]
    exec(compile(ast.Module(body=scroll_nodes,type_ignores=[]),'plugins/commands/scroll.py','exec'),
         {'__name__':'plugins.commands.scroll','command':command,'_scroll':scroll_hand})
    empty=tmp_path/'commands.json'; empty.write_text('{"commands":{}}',encoding='utf-8')
    monkeypatch.setattr(commands,'KeyboardController',MagicMock)
    monkeypatch.setattr(commands,'MouseController',MagicMock)
    executor=commands.CommandExecutor(empty,plugins_dir=tmp_path/'no_plugins')
    executor._app=app; executor.rebuild_matcher()
    app.command_executor=executor
    match=MagicMock(wraps=executor._matcher.match)
    monkeypatch.setattr(executor._matcher,'match',match)
    canonical=MagicMock(wraps=executor.execute_canonical)
    monkeypatch.setattr(executor,'execute_canonical',canonical)
    monkeypatch.setattr(execution_policy,'pending_operation',lambda:None)
    monkeypatch.setattr(injection_safety,'window_integrity',lambda:SimpleNamespace(blocked=False,confirmed_ok=True))
    from samsara import handlers, command_catalog
    monkeypatch.setattr(handlers,'_get_foreground_hwnd',lambda:42)
    monkeypatch.setattr(command_catalog,'personal_alias_offer_after_outcome',lambda *a:None)
    # Resolver boundary is faked, but _resolve_command_miss eligibility/deadline/
    # canonical execution and shared outcome routing are production code.
    def resolve(*args):
        if timeout:
            deadline=args[-1]
            namespace['time']=SimpleNamespace(**{k:getattr(time,k) for k in
                ('time','perf_counter','perf_counter_ns')}, monotonic=lambda:deadline+.01)
        return ava_command_session.Proposal('scroll down')
    resolver=MagicMock(side_effect=resolve)
    monkeypatch.setattr(ava_command_session,'resolve',resolver)
    monkeypatch.setattr(ava_command_session,'is_cancelled',lambda request_id:False)
    namespace['benchmark_store']=SimpleNamespace(append_sample=MagicMock())
    namespace['voice_memo']=SimpleNamespace(is_armed=lambda cfg:False)
    namespace['smart_correct']=lambda s,app:s
    return app, SimpleNamespace(model=model,match=match,canonical=canonical,hand=scroll_hand,
                                resolver=resolver,namespace=namespace)


def _run(app):
    app._stop_recording_impl()
    assert not app.command_mode_recording and not app.ava_mode_recording
    assert not app.play_sound.call_args_list or 'error' not in [c.args[0] for c in app.play_sound.call_args_list]
    assert not any(call.kwargs.get('status')=='failed' for call in app._log_history.call_args_list)


def _capture(name, app, spies):
    """Only fixed synthetic phrases enter task evidence, never user dictation."""
    record={'model_calls':spies.model.transcribe.call_count,'registry_matches':spies.match.call_count,
            'canonical_calls':spies.canonical.call_count,'executions':spies.hand.call_count,
            'resolver_calls':spies.resolver.call_count,
            'chips':[(c.args[0],c.args[1]) for c in app.listening_indicator.show_outcome.call_args_list],
            'pending_clears':app.listening_indicator.clear_outcome.call_count,
            'pastes':[c.args[0] for c in app._paste_preserving_clipboard.call_args_list],
            'sounds':[c.args[0] for c in app.play_sound.call_args_list]}
    if os.environ.get('GATE_RESULT'):
        dest=Path(os.environ['GATE_RESULT']).with_name(Path(os.environ['GATE_RESULT']).stem+'_'+name+'.json')
        dest.write_text(json.dumps(record,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return record


@pytest.mark.parametrize('matching',[False,True])
def test_commands_key_matches_and_executes_regardless_of_dictation_setting(tmp_path,monkeypatch,matching):
    app,spies=_app(tmp_path,monkeypatch,'scroll down',matching=matching)
    _run(app); record=_capture('a_default' if not matching else 'b_enabled',app,spies)
    assert record['model_calls']==1
    assert record['registry_matches']>=1
    assert record['executions']==1
    assert spies.hand.call_args.args==(8,)
    assert spies.hand.call_args.kwargs=={'up':False}
    assert record['pastes']==[]


def test_commands_key_miss_uses_shared_session_chip_and_never_types(tmp_path,monkeypatch):
    app,spies=_app(tmp_path,monkeypatch,'purple monkey')
    _run(app); record=_capture('c_miss',app,spies)
    assert record['executions']==0 and record['pastes']==[] and record['resolver_calls']==0
    assert record['chips'][-1]==outcome_chip('command_miss',{'heard':'purple monkey'})
    assert record['pending_clears']==0
    assert record['sounds']==['stop']  # existing release sound only; no new miss sound


def test_commands_key_ava_success_uses_shared_success_chip(tmp_path,monkeypatch):
    app,spies=_app(tmp_path,monkeypatch,'purple monkey',interpret=True,ava_enabled=True)
    _run(app); record=_capture('d_ava',app,spies)
    assert record['resolver_calls']==1 and record['executions']==1 and record['pastes']==[]
    assert record['chips'][-1]==outcome_chip('command_executed',
        {'phrase':'scroll down','state':'completed','heard':'purple monkey','origin':'model'})
    assert spies.canonical.call_args.kwargs['route'] is execution_policy.Route.MODEL
    assert record['pending_clears']==0


def test_commands_key_ava_timeout_shows_shared_miss_chip_without_execution(tmp_path,monkeypatch):
    app,spies=_app(tmp_path,monkeypatch,'purple monkey',interpret=True,ava_enabled=True,timeout=True)
    _run(app); record=_capture('ava_timeout',app,spies)
    assert record['resolver_calls']==1 and record['executions']==0 and record['pastes']==[]
    assert record['chips'][-1]==outcome_chip('command_miss',{'heard':'purple monkey'})
    assert record['pending_clears']==0


@pytest.mark.parametrize('matching',[False,True])
def test_main_dictation_key_always_types_and_never_executes(tmp_path,monkeypatch,matching):
    app,spies=_app(tmp_path,monkeypatch,'scroll down',matching=matching,command_key=False)
    _run(app); record=_capture('main_'+str(matching).lower(),app,spies)
    assert record['pastes']==['Scroll down. ']  # existing default cleanup/capitalization
    assert record['executions']==0 and record['canonical_calls']==0 and record['registry_matches']==0
    assert record['resolver_calls']==0


def test_interpret_misses_alone_does_not_enable_ava_master(tmp_path,monkeypatch):
    app,spies=_app(tmp_path,monkeypatch,'purple monkey',interpret=True)
    _run(app)
    assert spies.resolver.call_count==0 and spies.hand.call_count==0
    app._paste_preserving_clipboard.assert_not_called()


def test_ava_miss_above_the_word_limit_never_calls_resolver(tmp_path,monkeypatch):
    # 400 raised the limit from eight words to sixteen; seventeen is the first length that must not reach Ava.
    app,spies=_app(tmp_path,monkeypatch,
                   'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen',
                   interpret=True,ava_enabled=True)
    _run(app)
    assert spies.resolver.call_count==0 and spies.hand.call_count==0
    app._paste_preserving_clipboard.assert_not_called()
