"""774: spoken help must survive the production router, with effects isolated.

Routing tests replace the handler boundary, not matching or authorization.
Cube tests additionally run the real numeric handlers and observe their
requested windows. No app services, real windows, network or devices run.
"""
from functools import wraps
from types import SimpleNamespace as NS

import pytest

from samsara import command_catalog as cc, execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.command_scope import MatchContext


@pytest.fixture(scope="module")
def catalog_router():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    executor._matcher.set_enabled_packs({entry.pack for entry in executor._matcher._sorted})
    specs = cc.build_catalog(executor)
    executor._grammar_catalog()  # Preserve real handler identities before effect spies.
    from samsara import plugin_commands
    executor._example_registry = dict(plugin_commands._REGISTRY)
    return executor, specs


@pytest.fixture(autouse=True)
def registered_examples(catalog_router, _isolate_plugin_registry):
    # The shared autouse fixture clears plugin declarations before each test.
    # Restore this router's reviewed declarations, without rediscovery/services.
    from samsara import plugin_commands
    plugin_commands._REGISTRY.update(catalog_router[0]._example_registry)


def _app(executor):
    return NS(config={'commands': {'grammar_tier': True}}, command_mode_active=False,
              command_matching_enabled=True, command_executor=executor)


def _context(monkeypatch, executor, phrase):
    from plugins.commands import windows
    monitors = [dict(index=n, rect=(1920 * (n - 1), 0, 1920 * n, 1080),
                     width=1920, height=1080) for n in (1, 2)]
    monkeypatch.setattr(windows, '_strict_monitors', lambda: monitors)
    tags = {'window_cube.selection', 'click_by_name.pending', 'show_numbers.active'}
    entry = executor._registry_entry(phrase)
    if entry.scope is not None:
        tags.update(entry.scope.tags)
        tags.update(entry.scope.argument_tags)
    context = MatchContext.for_app('winword.exe' if phrase in {'font size', 'set font', 'line spacing'} else 'chrome.exe',
                                   tags=tags)
    monkeypatch.setattr(executor._matcher, 'current_context', lambda: context)


def test_every_spoken_argument_command_has_reviewed_examples(catalog_router):
    executor, specs = catalog_router
    for spec in specs:
        if not spec.args:
            continue
        phrase = cc.canonical_phrase(spec.to_dict())
        entry = executor._registry_entry(phrase)
        if not entry.voice_triggerable:
            assert phrase in cc._INTERNAL_HELP
            assert not spec.examples and 'not a spoken command' in spec.precondition
            continue
        assert spec.examples, spec.canonical_id
        assert 1 <= len(spec.examples) <= 3
        assert spec.precondition, spec.canonical_id


@pytest.mark.parametrize('phrase,sentence', [
    (phrase, sentence) for phrase, (examples, _before) in cc._COMMAND_HELP.items()
    for sentence in examples
])
def test_example_through_production_router(catalog_router, monkeypatch, phrase, sentence):
    executor, _specs = catalog_router
    _context(monkeypatch, executor, phrase)
    app = _app(executor)
    entry = executor._registry_entry(phrase)
    calls = []

    @wraps(entry.handler)
    def effect_spy(_app, remainder):
        calls.append(remainder)
        return True

    monkeypatch.setattr(entry, 'handler', effect_spy)
    # Confirmation is part of a destructive command's spoken workflow.
    # The two private submit identities teach the actual reply, never a draft id.
    if sentence == 'yes':
        inv = policy.Invocation(phrase, {'remainder': 'example-draft'}, policy.Route.EXACT, 0)
        policy.stage_pending(app, inv, 'Send the prepared example?',
                             on_approve=lambda _op: executor.execute_canonical(
                                 phrase, app, args=inv.args, generation=inv.generation, confirmed=True))
    try:
        result = executor.process_text(sentence, app, force_commands=True)
        if sentence == 'yes':
            assert result.state is DispatchState.COMPLETED
            assert calls == ['example-draft']
        else:
            assert result.phrase == phrase, (sentence, result)
            if result.state is DispatchState.QUEUED:
                assert not calls
                assert executor.process_text('yes', app, force_commands=True).state is DispatchState.COMPLETED
            else:
                assert result.state is DispatchState.COMPLETED, (sentence, result)
            assert len(calls) == 1, (sentence, calls)
    finally:
        from plugins.commands import ask_ollama
        ask_ollama.clear_pending_action()


def test_live_and_generated_help_agree(catalog_router):
    executor, specs = catalog_router
    live = {r['canonical_id']: r for r in cc.catalog_from_registry_rows(executor._matcher.list_commands())}
    for spec in specs:
        assert live[spec.canonical_id]['examples'] == spec.examples
        assert live[spec.canonical_id]['precondition'] == spec.precondition
        assert bool(live[spec.canonical_id]['args']) == bool(spec.args)


def _isolated_effects(monkeypatch, executor, app, phrase, tmp_path):
    """Sample resources plus observers at OS/service boundaries.

    Actual registered handlers and their local parsers run. Asynchronous
    services are observed where a request is queued, without running workers.
    """
    import importlib
    import time
    from unittest.mock import Mock

    entry = executor._registry_entry(phrase)
    module = importlib.import_module(entry.handler.__module__)
    observed = []

    def effect(name, result=True):
        def call(*args, **kwargs):
            observed.append((name, args, kwargs))
            return result
        return call

    def stub(name, result=True):
        monkeypatch.setattr(module, name, effect(name, result))

    # Silence feedback, never count it as evidence that an action succeeded.
    for name in ('_speak', '_say', '_chip', '_announce', '_report', '_notice'):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *_a, **_kw: None)
    app.audio_coordinator = NS(speak=lambda *_a, **_kw: None)
    app._show_outcome_chip = lambda *_a, **_kw: None
    app.config_path = tmp_path / 'config.json'

    stem = module.__name__.rsplit('.', 1)[-1]
    if stem == 'alarm_commands':
        stub('_get_manager', NS(items=[{'id': 'morning', 'name': 'morning'}],
                                update_alarm=effect('update_alarm')))
    elif stem == 'app_verbs':
        for name in ('do_open', 'do_focus', 'do_close'):
            stub(name, module.ActionResult.DONE)
        stub('_switch_desktop')
    elif stem == 'ask_agent':
        stub('ask_cli')
        monkeypatch.setattr(module.cli_agents, 'agent', lambda _name: NS(new_conversation=effect('new_conversation')))
        from samsara.agent_channel import hermes_voice
        monkeypatch.setattr(hermes_voice, 'ask', effect('hermes_ask'))
        monkeypatch.setattr(hermes_voice, 'permitted', lambda _cfg: True)
        monkeypatch.setattr(hermes_voice, 'channel', lambda _app: NS(new_conversation=effect('hermes_new')))
        monkeypatch.setattr(hermes_voice, 'notice', lambda *_a, **_kw: None)
    elif stem == 'ask_ollama':
        stub('_ai_state', NS(allowed=True, provider=NS(identity='cloud')))
        for name in ('_handle_persona_intent', '_handle_working_context_intent', '_handle_personal_memory_intent'):
            stub(name, False)
        monkeypatch.setattr(module.ava_turn_queue, 'submit', effect('ava_worker'))
    elif stem == 'audio_switch':
        stub('switch_audio_output')
        stub('switch_audio_input')
    elif stem == 'click_by_name':
        if phrase == 'choose control':
            selection = NS(nodes=['first', 'second'], service=NS(), verb='focus', hwnd=42)
            monkeypatch.setattr(module, '_pending', selection)
            monkeypatch.setattr(module, 'active', lambda _app: True)
            monkeypatch.setattr(module, 'cancel', lambda *_a: None)
            stub('_act')
        else:
            stub('try_named')
    elif stem == 'comfy':
        stub('_state', NS(last_prompt='a red fox in the snow'))
        stub('_start', module.DispatchResult(DispatchState.QUEUED, phrase, phrase))
    elif stem == 'core_utils':
        if phrase == 'save alias':
            app._personal_alias_offer = {'miss': 'my window list', 'canonical': 'show windows', 'expires': time.monotonic() + 60}
            monkeypatch.setattr(cc, 'save_user_alias', effect('save_user_alias'))
            monkeypatch.setattr(cc, 'remove_user_alias', effect('remove_user_alias'))
            monkeypatch.setattr(cc, 'install_user_aliases', lambda *_a: 0)
        else:
            from samsara import handlers
            from samsara.ui import qt_runtime
            monkeypatch.setattr(handlers, '_get_foreground_hwnd', lambda: 42)
            monkeypatch.setattr(handlers, '_get_foreground_exe_lower', lambda: 'chrome.exe')
            stub('_clickable_help', '')
            monkeypatch.setattr(qt_runtime, 'post', effect('show_scoped_reference'))
    elif stem == 'flashforge_printer':
        stub('_send', 'ok')
    elif stem == 'gif_search' or stem == 'web_shortcuts':
        monkeypatch.setattr(module.webbrowser, 'open', effect('open_url'))
    elif stem == 'health_tracker':
        monkeypatch.setattr(module.health_store, 'get_recent', effect('health_query', []))
        monkeypatch.setattr(module.health_store, 'add_entry', effect('health_entry'))
    elif stem == 'hyperion_lights':
        stub('_effects', ['rainbow'])
        stub('_send', {'success': True})
    elif stem in {'message_claude', 'message_chatgpt'}:
        monkeypatch.setattr(module, '_drafts', {})
        stub('_get_adapter', NS(resolve_window=lambda: (42, 'Sample chat', 'sample'),
                                find_composer=effect('find_composer', object())))
        if stem == 'message_claude':
            app._cli_agent_voice_state = {
                'agent': 'gpt', 'expires': time.monotonic() + 60,
                'full': '# Overview\nFirst section.\n```python\nprint(1)\n```\n'
                        '# Installation\nSecond section.\n```python\nprint(2)\n```\n',
            }
            app.audio_coordinator.speak = effect('read_reply')
            from samsara import clipboard
            monkeypatch.setattr(clipboard, 'copy_text', effect('copy_code'))
        # Prepare handlers still create their actual draft and pending record.
        # Sending is observed only at the adapter boundary in submit-specific tests.
    elif stem == 'music':
        library = [dict(name='Nick Cave', uri='spotify:artist:Sample', type='artist'),
                   dict(name='Abbey Road', uri='spotify:album:Sample', type='album'),
                   dict(name='Favourites', uri='spotify:playlist:Sample', type='playlist')]
        monkeypatch.setattr(module, '_library_entries', lambda _app: library)

        async def playback(*args):
            observed.append(('spotify', args, {}))
            return True

        monkeypatch.setattr(module, '_play_spotify', playback)

        def run(coroutine):
            # This fake playback has no awaits; a real Windows event loop
            # would create a self-pipe socket forbidden by the headless gate.
            try:
                coroutine.send(None)
            except StopIteration as done:
                return done.value
            finally:
                coroutine.close()
            raise AssertionError('Playback unexpectedly needed an event loop')

        monkeypatch.setattr(module, 'asyncio', NS(run=run, wait_for=lambda coro, timeout: coro))
    elif stem == 'quick_ask':
        stub('_send_to_arc')
    elif stem == 'reminders':
        manager = NS(get_all_reminders=lambda: [{'id': 'first', 'name': 'first'}, {'id': 'stretch', 'name': 'stretch'}],
                     remove_reminder=effect('remove_reminder'), add_reminder=effect('add_reminder'))
        stub('_get_manager', manager)
    elif stem == 'screen_gif':
        def start(_app, remainder, window_only):
            assert module._parse_duration(remainder) == 5
            return effect('recording')(_app, remainder, window_only)
        monkeypatch.setattr(module, '_start_recording', start)
    elif stem == 'show_numbers':
        monkeypatch.setattr(module, 'grid_active', lambda: False)
        if phrase == 'pick number':
            # Populate the real plugin's label state: scope tags alone do not
            # satisfy the handler's independent visibility check.
            from plugins.commands import show_numbers
            assert module is show_numbers
            assert entry.handler.__globals__ is vars(show_numbers)
            monkeypatch.setattr(module, '_elements', [object() for _ in range(12)])
        for name in ('_click_number', '_click_text', '_click_at_cursor', '_grid_show', '_grid_apply'):
            stub(name)
        stub('_grid_area', ((0, 0, 1920, 1080), 'sample screen'))
        import win32gui
        monkeypatch.setattr(win32gui, 'GetForegroundWindow', lambda: 42)
    elif stem == 'smart_actions':
        stub('_do_capture')
    elif stem == 'snippets':
        record = {'id': 'sample', 'name': 'sign off', 'text': 'Synthetic snippet sample'}
        monkeypatch.setattr(module.snippets, 'load_snippets', lambda: [record])
        monkeypatch.setattr(module.snippets, 'record_use', effect('record_use'))
        monkeypatch.setattr(module.snippets, 'add_snippet', effect('add_snippet', record))
        stub('last_dictation', 'Synthetic snippet sample')
        app._paste_preserving_clipboard = effect('paste_snippet')
    elif stem == 'tab_finder':
        for name in ('hotkey', 'press', 'write'):
            monkeypatch.setattr(module.pyautogui, name, effect(name))
        monkeypatch.setattr(module.pyperclip, 'copy', effect('clipboard'))
        monkeypatch.setattr(module.time, 'sleep', lambda _seconds: None)
        stub('_focus_browser')
    elif stem == 'tasks':
        store = module.tasks_store
        monkeypatch.setattr(store, 'get_active', lambda: [{'id': 'first', 'text': 'first'}, {'id': 'second', 'text': 'second'}])
        for name in ('add_task', 'complete_task', 'remove_task'):
            monkeypatch.setattr(store, name, effect(name, {'id': 'sample'}))
        monkeypatch.setattr(module, '_refresh_overlay', lambda: None)
        monkeypatch.setattr(module, '_todoist_token', lambda _app: None)
    elif stem == 'timer':
        monkeypatch.setattr(module.thread_registry, 'spawn', effect('timer_thread', Mock()))
        monkeypatch.setattr(module, '_active_timers', [])
    elif stem == 'volume':
        monkeypatch.setattr(module._audio, 'set_volume', effect('windows_volume'))
    elif stem == 'window_cube':
        monkeypatch.setattr(module, '_app_ref', app)
        monkeypatch.setattr(module, '_selection_available', lambda: True)
        monkeypatch.setattr(module, '_slot_for_number', lambda _n: {'hwnd': 42, 'title': 'Sample', 'closed': False})
        monkeypatch.setattr(module, '_build_rows', lambda _app: [(42, 'Sample app', 'Sample')])
        stub('_switch_to_number')
        monkeypatch.setattr(module, '_slots', [{'number': n} for n in range(1, 5)])
        monkeypatch.setattr(module, '_page', 1)
        app.config['window_cube'] = {'max_rows': 1}
        stub('_render')
        monkeypatch.setattr(module, '_letters_for_numbers', lambda _app, nums: ['ABCD'[n - 1] for n in nums])
        stub('handle_window_tile')
        stub('handle_window_copy')
    elif stem == 'window_switcher':
        stub('_resolve', (42, 'Sample window', False))
        for name in ('_force_focus', '_dismiss_all', '_cancel_timer', '_reset_timer', '_move_window', '_set_process_mute'):
            stub(name)
        monitors = [dict(index=n, rect=(0, 0, 1920, 1080), width=1920, height=1080) for n in (1, 2)]
        stub('_get_monitors', monitors)
        monkeypatch.setattr(module, '_mapping', {})
        monkeypatch.setattr(module, 'user32', NS(PostMessageW=effect('close_window')))
        import win32api
        import win32gui
        monkeypatch.setattr(win32api, 'keybd_event', effect('copy_paste_key'))
        monkeypatch.setattr(win32gui, 'IsIconic', lambda _hwnd: False)
        monkeypatch.setattr(win32gui, 'SetWindowPos', effect('tile_window'))
        monkeypatch.setattr(module.time, 'sleep', lambda _seconds: None)
        from plugins.commands import windows
        monkeypatch.setattr(windows, 'get_monitors', lambda: monitors)
        monkeypatch.setattr(windows, 'get_monitor_under_cursor', lambda *_a: monitors[0])
    elif stem == 'windows':
        monitors = [dict(index=n, rect=(0, 0, 1920, 1080), width=1920, height=1080) for n in (1, 2)]
        stub('get_monitors', monitors)
        stub('get_monitor_under_cursor', monitors[0])
        stub('find_windows_by_app', [42])
        stub('move_window_to_monitor')
        stub('teleport_cursor')
        for name in ('_delete_layout', '_restore_layout', '_save_current_layout'):
            stub(name)
        stub('_place_one', {'ok': True, 'app': 'firefox', 'monitor': 2})
        monkeypatch.setattr(module.win32gui, 'GetWindowRect', lambda _h: (0, 0, 640, 480))
        monkeypatch.setattr(module.win32gui, 'GetWindowText', lambda _h: 'Sample Firefox')
        monkeypatch.setattr(module.win32gui, 'IsIconic', lambda _h: False)
        from plugins.commands import app_verbs
        monkeypatch.setattr(app_verbs, 'snap_named_window', effect('snap_window'))
    elif stem == 'dictionary':
        from plugins.commands import ask_ollama as _ava
        monkeypatch.setattr(_ava, '_check_teaching_intent', effect('dictionary_teach'))
        monkeypatch.setattr(module, '_check_teaching_intent', effect('dictionary_teach'), raising=False)
    elif stem == 'word':
        selection = NS(Font=NS(Name=None, Size=None), ParagraphFormat=NS(LineSpacingRule=None),
                       Application=NS(FontNames=['Arial']))

        def select(_app, call):
            result = call(selection)
            observed.append(('word_selection', (selection,), {}))
            return True, result

        monkeypatch.setattr(module, '_selection_call', select)
    else:
        pytest.fail('Missing isolation for ' + stem)
    return observed


@pytest.mark.parametrize('phrase,sentence', [
    (phrase, sentence) for phrase, (examples, _before) in cc._COMMAND_HELP.items()
    for sentence in examples if sentence != 'yes'
])
def test_example_runs_real_handler_with_isolated_effects(catalog_router, monkeypatch, tmp_path, phrase, sentence):
    from plugins.commands import ask_ollama
    executor, _specs = catalog_router
    _context(monkeypatch, executor, phrase)
    app = _app(executor)
    observed = _isolated_effects(monkeypatch, executor, app, phrase, tmp_path)
    try:
        result = executor.process_text(sentence, app, force_commands=True)
        assert result.phrase == phrase, (sentence, result)
        if result.state is DispatchState.QUEUED and not observed:
            assert executor.process_text('yes', app, force_commands=True).state is DispatchState.COMPLETED
        else:
            assert result.state in {DispatchState.COMPLETED, DispatchState.QUEUED}, (sentence, result)
        assert observed, (sentence, 'Handler did not request an effect')
    finally:
        ask_ollama.clear_pending_action()


@pytest.mark.parametrize('phrase', sorted(cc._NO_ARGUMENT_PHRASES))
def test_no_tail_controls_are_not_advertised_as_argument_commands(catalog_router, phrase):
    _executor, specs = catalog_router
    spec = next(s for s in specs if cc.canonical_phrase(s.to_dict()) == phrase)
    assert not spec.args and not spec.examples


@pytest.mark.parametrize('phrase', ['cube tile', 'cube copy'])
def test_cube_numeric_metadata_even_without_handler_schema(phrase):
    args = cc.infer_args('plugin', phrase, None, None)
    assert [a.type for a in args] == ['int']


@pytest.mark.parametrize('phrase,suffix,numbers', [
    ('cube tile', '3 and 4', [3, 4]),
    ('cube tile', 'three and four', [3, 4]),
    ('cube tile', 'two and three and four', [2, 3, 4]),
    ('cube copy', '2 into 4', [2, 4]),
    ('cube copy', 'two into four', [2, 4]),
])
def test_cube_examples_reach_real_numeric_handler(catalog_router, monkeypatch, phrase, suffix, numbers):
    from plugins.commands import window_cube as cube
    executor, _specs = catalog_router
    _context(monkeypatch, executor, phrase)
    app = _app(executor)
    resolved, effects = [], []

    def letters(_app, values):
        resolved.append(values)
        return ['ABCD'[n - 1] for n in values]

    monkeypatch.setattr(cube, '_letters_for_numbers', letters)
    monkeypatch.setattr(cube, 'handle_window_tile', lambda _app, r: effects.append(r) or True)
    monkeypatch.setattr(cube, 'handle_window_copy', lambda _app, r: effects.append(r) or True)
    monkeypatch.setattr(cube, '_speak', lambda *_args: pytest.fail('Valid example refused'))
    result = executor.process_text(phrase + ' ' + suffix, app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert resolved == [numbers]
    joiner = ' and ' if phrase == 'cube tile' else ' into '
    assert effects == [joiner.join('ABCD'[n - 1] for n in numbers)]


@pytest.mark.parametrize('suffix', ['three and for', 'to and four'])
def test_cube_homophones_are_an_existing_handler_limitation(catalog_router, monkeypatch, suffix):
    from plugins.commands import window_cube as cube
    executor, _specs = catalog_router
    _context(monkeypatch, executor, 'cube tile')
    spoken = []
    monkeypatch.setattr(cube, '_speak', lambda _app, text: spoken.append(text))
    monkeypatch.setattr(cube, '_letters_for_numbers', lambda *_args: pytest.fail('Homophone unexpectedly parsed'))
    result = executor.process_text('cube tile ' + suffix, _app(executor), force_commands=True)
    # True means the handler consumed the request, not that windows were tiled.
    assert result.state is DispatchState.COMPLETED
    assert spoken == ['Need at least two numbers. Say cube tile 2 and 4.']


@pytest.mark.parametrize('phrase', ['cube tile', 'cube copy', 'set a timer'])
@pytest.mark.parametrize('width', [420, 640])
def test_detail_card_shows_examples_without_execution(catalog_router, qapp, tmp_path, monkeypatch, phrase, width):
    from PySide6.QtCore import QRect, Qt
    from samsara import command_stats
    from samsara.ui import command_cheatsheet_qt as sheet
    executor, _specs = catalog_router
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _limit: [])
    monkeypatch.setattr(sheet, '_scope_context', lambda: MatchContext.for_app('chrome.exe'))
    calls = []
    win = sheet._CheatSheetWindow(calls.append, executor._matcher.list_commands, tmp_path / 'palette.json')
    win.resize(width, 720)
    win.show()
    qapp.processEvents()
    command = next(row for row in win._all if row['phrase'] == phrase)
    card = win._detail_card
    card.open(command, win._list, QRect(0, 60, 300, 44))
    qapp.processEvents()
    assert calls == []
    assert card.labels[3].text().startswith('Say it like this:\n')
    assert all(sentence in card.labels[3].text() for sentence in command['examples'])
    assert command['precondition'] in card.labels[2].text()
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in card.labels)
    assert 'cube tile and' not in card.labels[1].text()
    assert 'cube copy into' not in card.labels[1].text()
    assert card.run_button.width() >= card.run_button.sizeHint().width()
    assert card.rect().contains(card.run_button.geometry().center())
    assert card.scroll.horizontalScrollBar().maximum() == 0
    win.hide()
    win.deleteLater()


def test_examples_shape_is_validated():
    record = cc.CommandSpec('sample.example', 'sample', 'example', '').to_dict()
    record['aliases'] = ['example']
    record['description'] = 'Example'
    record['source'] = 'example.py:1'
    record['examples'] = ['']
    assert any('examples' in problem for problem in cc.validate_catalog({'version': 1, 'commands': [record]}))
