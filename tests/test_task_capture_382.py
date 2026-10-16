"""382: task hold capture, exact memo sink branch, and permitted raw-only path.

The desktop bootstrap is never imported. Provider, OS and Todoist hands are
stubbed; actual production hotkey methods and the memo delivery branch run.
"""
import ast
import importlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import tasks
from samsara import hotkeys, tasks_store
from samsara.runtime import thread_registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_store, '_TASKS_PATH', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(tasks_store, '_data', {'tasks': [], 'next_id': 1})
    return tasks_store


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    import urllib.request
    import requests
    from samsara import todoist_client, plugin_commands

    def forbidden(*args, **kwargs):
        raise AssertionError('external/model hand called')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(requests, 'post', forbidden)
    monkeypatch.setattr(todoist_client, 'quick_add', forbidden)
    # A stub module catches any accidental import/call of the model provider.
    monkeypatch.setitem(sys.modules, 'plugins.commands.ask_ollama',
                        SimpleNamespace(ask_ollama=forbidden))
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES',
                        {name: dict(entries) for name, entries in plugin_commands._MODULE_ENTRIES.items()})


def _hotkey_app(held, *, starts=True, config=None):
    app = SimpleNamespace(
        config={'hotkey': 'f8', 'mode': 'hold', **(config or {})},
        snoozed=False, recording=False, hotkey_pressed=False,
        _memo_recording=False, command_mode_recording=False,
        _stop_in_flight=False, _streaming_session=None,
        current_keys=set(), key_press_times={},
        get_key_name=lambda key: key,
        check_hotkey_state=lambda combo: combo in held,
        parse_hotkey=lambda combo: set(combo.split('+')),
        _check_command_mode_key=lambda *args, **kwargs: None,
        _hands_free_dictation_commit_available=lambda: False,
        play_sound=Mock(), stop_recording=Mock(),
    )
    app.start_recording = Mock(side_effect=lambda **kwargs: setattr(app, 'recording', starts))
    for name in ('on_key_press', 'on_key_release', '_hotkey_state_text', '_other_hotkey_held'):
        setattr(app, name, MethodType(getattr(hotkeys.HotkeyCluster, name), app))
    return app


def test_task_press_uses_memo_lane_and_nonstreaming_capture():
    app = _hotkey_app({'ctrl+alt+t'})
    app.on_key_press('t')
    assert app._memo_recording and app.hotkey_pressed and app.recording
    assert app._capture_sink == 'task'
    app.start_recording.assert_called_once_with(streaming=False, play_earcon=False)


def test_failed_task_start_clears_sink_and_press_flags():
    app = _hotkey_app({'ctrl+alt+t'}, starts=False)
    app.on_key_press('t')
    assert not app._memo_recording and not app.hotkey_pressed
    assert app._capture_sink is None


@pytest.mark.parametrize('held', [{'ctrl+alt+m'}, {'ctrl+alt+m', 'ctrl+alt+t'}])
def test_memo_press_stays_memo_and_wins_when_both_combos_are_held(held):
    app = _hotkey_app(held)
    app.on_key_press('m')
    assert app._memo_recording and app.recording
    assert getattr(app, '_capture_sink', None) is None
    app.start_recording.assert_called_once_with(streaming=False, play_earcon=False)


def test_release_waits_for_task_combo_then_spawns_existing_stop_once(monkeypatch):
    held = {'ctrl+alt+t'}
    app = _hotkey_app(held)
    jobs = []
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kwargs: jobs.append((name, fn)))
    app.on_key_press('t')
    app.on_key_release('unrelated')
    assert jobs == [] and app.hotkey_pressed
    held.clear()
    app.on_key_release('t')
    app.on_key_release('alt')
    assert [name for name, _ in jobs] == ['stop-memo']
    assert app._stop_in_flight and not app.hotkey_pressed
    jobs[0][1]()
    app.stop_recording.assert_called_once_with()
    assert not app._stop_in_flight


def test_custom_task_combo_is_read_and_owns_the_press():
    app = _hotkey_app({'f9'}, config={'task_hotkey': 'f9'})
    app.on_key_press('f9')
    assert app._capture_sink == 'task'
    assert app._other_hotkey_held()


def test_default_task_combo_owns_hotkey_pressed():
    assert _hotkey_app({'ctrl+alt+t'})._other_hotkey_held()


def test_all_default_places_and_quick_reference_follow_live_config():
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    defaults = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '_OTHER_HOTKEY_KEYS' for t in n.targets))
    assert ('task_hotkey', 'ctrl+alt+t') in ast.literal_eval(defaults)
    assert ('task_hotkey', 'ctrl+alt+t') in hotkeys._OTHER_HOTKEY_KEYS
    from samsara.config_seed import build_default_config
    config = build_default_config(
        home=Path("synthetic-home"), macro_defaults={}, notification_defaults={},
    )
    assert config['task_hotkey'] == 'ctrl+alt+t'
    from samsara.ui import quick_reference_qt as qr
    from samsara.config_schema import SETTINGS_SCHEMA
    app = SimpleNamespace(config={})
    rows = qr._resolve_hotkeys(app)
    labels = [row['label'] for row in rows]
    assert labels[labels.index('Memo recording') + 1] == 'Add a task (hold)'
    assert rows[labels.index('Add a task (hold)')]['value'] == 'Ctrl+Alt+T'
    app.config['task_hotkey'] = 'f9'
    assert {r['label']: r for r in qr._resolve_hotkeys(app)}['Add a task (hold)']['value'] == 'F9'
    entry = SETTINGS_SCHEMA['tasks.capture.ava_parse']
    assert (entry['type'], entry['default'], entry['tab']) == ('bool', False, 'commands')
    # Config-only: no Settings row, so the schema must say so and say why (285 inventory rule).
    assert entry['settings_ui'] is False and entry['ui_less_reason'].strip()


def test_deliver_task_calls_lazy_plugin_and_returns_its_result(monkeypatch):
    sinks = importlib.import_module('samsara.capture_sinks')
    app = object()
    capture = Mock(return_value=True)
    monkeypatch.setattr(tasks, 'capture_spoken_task', capture)
    assert sinks.deliver_task(app, 'item') is True
    capture.assert_called_once_with(app, 'item')


def test_deliver_task_exception_logs_and_stores_raw_once(store, monkeypatch, caplog):
    sinks = importlib.import_module('samsara.capture_sinks')
    monkeypatch.setattr(tasks, 'capture_spoken_task', Mock(side_effect=RuntimeError('synthetic failure')))
    assert sinks.deliver_task(object(), '  item  ') is True
    assert [task['text'] for task in store.get_all()] == ['item']
    assert 'parsed' not in store.get_all()[0]
    assert any(record.exc_info for record in caplog.records)


def test_deliver_task_exception_does_not_store_empty(store, monkeypatch):
    sinks = importlib.import_module('samsara.capture_sinks')
    monkeypatch.setattr(tasks, 'capture_spoken_task', Mock(side_effect=RuntimeError('synthetic failure')))
    assert sinks.deliver_task(object(), '   ') is False
    assert store.get_all() == []


def test_add_task_preserves_five_keys_and_optional_parse_shape(store):
    plain = store.add_task(' item ')
    assert set(plain) == {'id', 'text', 'completed', 'created', 'completed_at'}
    parsed = store.add_task('item', due='2026-10-09', time='15:00', heard='synthetic', parsed=True)
    assert set(parsed) == set(plain) | {'parsed', 'due', 'time', 'heard'}
    assert {key: parsed[key] for key in ('parsed', 'due', 'time', 'heard')} == {
        'parsed': True, 'due': '2026-10-09', 'time': '15:00', 'heard': 'synthetic'}
    no_heard = store.add_task('item', parsed=True)
    assert set(no_heard) == set(plain) | {'parsed', 'due', 'time'}
    assert no_heard['due'] == no_heard['time'] == ''
    plain_with_extras = store.add_task('item', due='ignored', time='ignored', heard='ignored')
    assert set(plain_with_extras) == set(plain)


@pytest.mark.parametrize('switch', [None, False, True])
def test_switch_is_read_but_inert_and_capture_calls_existing_raw_handler(store, monkeypatch, switch):
    class CaptureConfig(dict):
        def get(self, key, default=None):
            reads.append(key)
            return super().get(key, default)
    reads = []
    config = {} if switch is None else {'tasks': {'capture': CaptureConfig(ava_parse=switch)}}
    spoken = Mock()
    app = SimpleNamespace(config=config, audio_coordinator=SimpleNamespace(speak=spoken))
    raw = Mock(wraps=tasks.handle_add_to_list)
    monkeypatch.setattr(tasks, 'handle_add_to_list', raw)
    assert tasks.capture_spoken_task(app, '  synthetic item  ') is True
    raw.assert_called_once_with(app, '  synthetic item  ')
    assert [task['text'] for task in store.get_all()] == ['synthetic item']
    assert 'parsed' not in store.get_all()[0]
    assert spoken.call_args.args == ('Added: synthetic item.',)
    if switch is not None:
        assert 'ava_parse' in reads


@pytest.mark.parametrize('text', ['', ' \t\n '])
def test_empty_capture_stores_nothing_and_gives_feedback(store, text):
    spoken = Mock()
    app = SimpleNamespace(config={}, audio_coordinator=SimpleNamespace(speak=spoken))
    assert tasks.capture_spoken_task(app, text) is False
    assert store.get_all() == []
    assert spoken.call_args.args == ("I didn't catch a task.",)


def test_raw_capture_reuses_existing_opt_in_todoist_delivery(store, monkeypatch):
    jobs = []
    monkeypatch.setattr(thread_registry, 'spawn',
                        lambda name, fn, args=(), **kwargs: jobs.append((name, fn, args)))
    app = SimpleNamespace(config={'tasks': {'todoist': {'enabled': True, 'api_token': 'test-only'}}},
                          audio_coordinator=None, tts_engine=None)
    assert tasks.capture_spoken_task(app, 'synthetic item') is True
    assert len(jobs) == 1 and jobs[0][0] == 'tasks-todoist-add' and jobs[0][1] is tasks._deliver
    assert jobs[0][2] == (app, store.get_all()[0])


def test_production_stop_delivers_task_resets_sink_and_skips_memo_and_paste(tmp_path, monkeypatch):
    # Reuse 391's accepted complete-method AST harness with synthetic audio.
    from tests.test_commands_only_key_391 import _app, _run
    app, spies = _app(tmp_path, monkeypatch, 'synthetic item', command_key=False)
    app._memo_recording = True
    app._capture_sink = 'task'
    forbidden = Mock(side_effect=AssertionError('memo side effect on task lane'))
    spies.namespace['quick_memo'] = SimpleNamespace(
        retain_audio=forbidden, categories=forbidden, split_category=forbidden,
        add_memo=forbidden, mirror_to_vault=forbidden, prune_audio=forbidden)
    delivery = Mock(return_value=True)
    monkeypatch.setattr(importlib.import_module('samsara.capture_sinks'), 'deliver_task', delivery)
    _run(app)
    assert delivery.call_count == 1 and delivery.call_args.args[0] is app
    assert 'synthetic item' in delivery.call_args.args[1].lower()
    assert not app._memo_recording and app._capture_sink is None
    forbidden.assert_not_called()
    app._paste_preserving_clipboard.assert_not_called()


def _capture_lifecycle_app(tmp_path, monkeypatch):
    """Run exact press/release/cancel/stop bodies with synthetic audio only."""
    import logging
    import threading
    from tests.test_commands_only_key_391 import _app

    app, spies = _app(tmp_path, monkeypatch, 'second synthetic item', command_key=False)
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {'cancel_recording', '_cancel_recording_impl'}]
    assert {n.name for n in methods} == {'cancel_recording', '_cancel_recording_impl'}
    namespace = {'logger': logging.getLogger('382.cancel.production')}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(ROOT / 'dictation.py'), 'exec'), namespace)
    for name in namespace.keys() & {'cancel_recording', '_cancel_recording_impl'}:
        setattr(app, name, MethodType(namespace[name], app))
    app._hold_capture_lifecycle_lock = threading.RLock()
    app._restore_audio = Mock()
    app._close_hold_capture_duck = Mock()
    app._dictation_consumer.cancel = Mock()
    app._memo_recording = False
    app.snoozed = False
    app._stop_in_flight = False
    app.current_keys = set()
    app.key_press_times = {}
    app.config.update(hotkey='f8', mode='hold', memo_retain_audio=False)
    app.get_key_name = lambda key: key
    held = set()
    app.check_hotkey_state = lambda combo: combo in held
    app.parse_hotkey = lambda combo: set(combo.split('+'))
    app._check_command_mode_key = lambda *a, **kw: None
    app._hands_free_dictation_commit_available = lambda: False
    app.audio_coordinator = SimpleNamespace(speak=Mock())
    app.start_recording = Mock(side_effect=lambda **kw: app.set_app_state(
        recording=True, _ace_dictation_active=True))
    app.stop_recording = app._stop_recording_impl
    for name in ('on_key_press', 'on_key_release', '_hotkey_state_text', '_other_hotkey_held'):
        setattr(app, name, MethodType(getattr(hotkeys.HotkeyCluster, name), app))
    jobs = []
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: jobs.append((name, fn)))
    monkeypatch.setattr(tasks, '_refresh_overlay', Mock())
    memos = Mock(return_value={})
    spies.namespace['quick_memo'] = SimpleNamespace(
        categories=lambda home: [], split_category=lambda text, known: (None, text),
        add_memo=memos, SOURCE_VOICE='voice', CATEGORY_SPOKEN='spoken',
        retain_audio=Mock(), mirror_to_vault=Mock(), prune_audio=Mock())
    app.set_app_state(recording=False, _ace_dictation_active=False)
    return app, held, jobs, memos


def _press_capture(app, held, combo):
    starts = app.start_recording.call_count
    held.add(combo)
    app.on_key_press(combo.split('+')[-1])
    assert app.recording and app.start_recording.call_count == starts + 1


def _release_capture(app, held, jobs, combo):
    held.discard(combo)
    app.on_key_release(combo.split('+')[-1])
    while jobs:
        _name, job = jobs.pop(0)
        job()


@pytest.mark.parametrize(('first', 'second', 'destination'), [
    ('ctrl+alt+t', 'ctrl+alt+m', 'memo'),
    ('ctrl+alt+m', 'ctrl+alt+t', 'task'),
    ('ctrl+alt+t', 'ctrl+alt+t', 'task'),
])
def test_escape_then_next_capture_uses_its_own_destination(
        store, tmp_path, monkeypatch, first, second, destination):
    app, held, jobs, memos = _capture_lifecycle_app(tmp_path, monkeypatch)
    _press_capture(app, held, first)
    held.add('escape')
    app.on_key_press('escape')
    assert not app.recording
    assert app._capture_sink is None
    held.discard('escape')
    app.on_key_release('escape')
    _release_capture(app, held, jobs, first)
    assert not app.hotkey_pressed and jobs == [] and store.get_all() == []
    _press_capture(app, held, second)
    _release_capture(app, held, jobs, second)
    assert not app._memo_recording and app._capture_sink is None
    app._paste_preserving_clipboard.assert_not_called()
    if destination == 'memo':
        assert memos.call_count == 1 and store.get_all() == []
    else:
        memos.assert_not_called()
        assert len(store.get_all()) == 1
        assert 'second synthetic item' in store.get_all()[0]['text'].lower()


def test_memo_press_explicitly_clears_a_stale_task_sink():
    app = _hotkey_app({'ctrl+alt+m'})
    app._capture_sink = 'task'
    app.on_key_press('m')
    assert app.recording and app._memo_recording and app._capture_sink is None


def test_task_delivery_exception_clears_sink_before_next_memo(
        store, tmp_path, monkeypatch):
    app, held, jobs, memos = _capture_lifecycle_app(tmp_path, monkeypatch)
    monkeypatch.setattr(tasks, 'capture_spoken_task', Mock(side_effect=RuntimeError('synthetic failure')))
    _press_capture(app, held, 'ctrl+alt+t')
    _release_capture(app, held, jobs, 'ctrl+alt+t')
    assert app._capture_sink is None and not app._memo_recording
    assert len(store.get_all()) == 1  # delivery exception still saves raw
    _press_capture(app, held, 'ctrl+alt+m')
    _release_capture(app, held, jobs, 'ctrl+alt+m')
    assert memos.call_count == 1 and len(store.get_all()) == 1
    app._paste_preserving_clipboard.assert_not_called()


@pytest.mark.parametrize('key_still_held', [True, False])
def test_cancelled_memo_then_ordinary_hold_is_typed(
        store, tmp_path, monkeypatch, key_still_held):
    app, held, jobs, memos = _capture_lifecycle_app(tmp_path, monkeypatch)
    _press_capture(app, held, 'ctrl+alt+m')
    if not key_still_held:
        # Cancellation can also run after physical key ownership ended.
        held.clear()
        app.hotkey_pressed = False
    held.add('escape')
    app.on_key_press('escape')
    held.discard('escape')
    app.on_key_release('escape')
    # Case g: Escape while held clears both capture flags; release is inert.
    assert not app.recording and not app._memo_recording
    assert app._capture_sink is None
    assert jobs == []
    _release_capture(app, held, jobs, 'ctrl+alt+m')
    assert jobs == [] and not app.hotkey_pressed and not app._stop_in_flight
    assert memos.call_count == 0 and store.get_all() == []
    app._paste_preserving_clipboard.assert_not_called()
    _press_capture(app, held, 'f8')
    _release_capture(app, held, jobs, 'f8')
    assert memos.call_count == 0 and store.get_all() == []
    assert app._paste_preserving_clipboard.call_count == 1
    assert 'second synthetic item' in app._paste_preserving_clipboard.call_args.args[0].lower()


def test_escape_without_recording_leaves_all_capture_attributes_untouched(tmp_path, monkeypatch):
    app, held, jobs, _memos = _capture_lifecycle_app(tmp_path, monkeypatch)
    app.recording = False
    app._memo_recording = True
    app._capture_sink = 'task'
    app._hotkey_recording = True
    before = vars(app).copy()
    app._cancel_recording_impl()
    assert vars(app) == before
    assert held == set() and jobs == []
