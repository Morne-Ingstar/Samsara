"""781: visible labels own whole picks; hidden labels never eat dictation.

Run dictation imports through the maintained no-bootstrap gate. All utterances
and controls here are synthetic; mouse/Qt/bridge/timer boundaries are inert.
"""
import threading
from types import SimpleNamespace

import pytest

from dictation import DictationApp
from plugins.commands import click_by_name, show_numbers as sn
from samsara import command_scope, execution_policy, plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.commands import CommandExecutor
from samsara.numbered_picks import parse_pick, pick_remainder
from samsara.session_modes import (
    CommandDispatchResult, SessionMode, SessionModeManager, UtteranceSignals,
)


PICKS = ('click 5', 'click five', '5', 'five', 'number 5', 'pick 5',
         'choose 5', 'press 5', 'tap 5')
MODIFIED = (
    ('click five twice', 'double', frozenset()),
    ('5 right', 'right', frozenset()),
    ('ctrl click 5', 'single', frozenset({'ctrl'})),
    ('shift number five', 'single', frozenset({'shift'})),
    ('alt pick 5', 'single', frozenset({'alt'})),
    ('ctrl shift alt choose five right', 'right', frozenset({'ctrl', 'shift', 'alt'})),
    ('double click 5', 'double', frozenset()),
    ('right-click 5', 'right', frozenset()),
)
GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
BAD = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.2,))


@pytest.fixture
def rig(monkeypatch):
    click_by_name.cancel()
    plugin_commands._reinstall_module_commands(sn)
    for name, value in {'_elements': [], '_dom_active': False, '_dom_hint_count': 0,
                        '_dismiss_timer': None, '_overlay_window': None,
                        '_fg_timer': None, '_taskbar_mode': False}.items():
        monkeypatch.setattr(sn, name, value)
    monkeypatch.setattr(sn, 'grid_active', lambda: False)
    monkeypatch.setattr(sn.qt_runtime, 'ensure_started', lambda: None)
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: fn())
    state = SimpleNamespace(now=0, timers=[], clicks=[], dom_clicks=[], pastes=[],
                            agents=[], spoken=[], click_ok=True, dismissals=[])

    def timer(name, seconds, callback, **kwargs):
        result = SimpleNamespace(deadline=state.now + seconds, callback=callback,
                                 cancelled=False)
        result.cancel = lambda: setattr(result, 'cancelled', True)
        state.timers.append(result)
        return result

    def click(control, modifier, keys):
        state.clicks.append((control, modifier, keys))
        return state.click_ok

    def dom_click(number, action, **kwargs):
        state.dom_clicks.append((number, action, kwargs['modifiers']))
        return state.click_ok

    monkeypatch.setattr(sn.thread_registry, 'timer', timer)
    monkeypatch.setattr(sn, '_click_with_validation', click)
    monkeypatch.setattr(sn, '_speak', lambda app, text: state.spoken.append(text))
    monkeypatch.setattr(sn.browser_bridge, 'get_bridge', lambda: SimpleNamespace(
        send_selection=dom_click, send_dismiss=lambda: state.dismissals.append(True)))
    app = DictationApp.__new__(DictationApp)
    app.config = {'hands_free': {'exact_commands': True}}
    app.command_mode_active = False
    app.command_matching_enabled = True
    app._session_mode_manager = None
    app._probe_media_transport = lambda text: None
    ex = CommandExecutor.__new__(CommandExecutor)
    ex._app, ex.commands = app, {}
    ex._matcher_lock = threading.RLock()
    ex._matcher = CommandMatcher()
    ex._matcher.load_plugins(plugin_commands._MODULE_ENTRIES[sn.__name__])
    ex._matcher.set_context_provider(lambda: command_scope.MatchContext.for_app(
        'editor.exe', tags=command_scope.active_tags()))
    ex._matcher.freeze()
    app.command_executor = ex
    state.app, state.executor = app, ex
    state.controls = [object() for _ in range(5)]

    def show(backend='uia'):
        sn._elements[:] = state.controls if backend == 'uia' else []
        sn._dom_active = backend == 'dom'
        sn._dom_hint_count = 5 if backend == 'dom' else 0
        sn._schedule_dismiss_timer(app)

    def manager(mode=SessionMode.DICTATE, *, buffered=True):
        def dispatch(text):
            result = ex.process_text(text, app, force_commands=True)
            return CommandDispatchResult(matched=result.claimed, phrase=result.phrase,
                                         state=result.state)

        result = SessionModeManager(
            abort_phrases=['cancel', 'abort', 'stop listening'],
            foreground_exe_resolver=lambda: 'editor.exe',
            foreground_hwnd_resolver=lambda: 101,
            inject_fn=lambda text, **kwargs: state.pastes.append(text) or text,
            remove_chars_fn=lambda count: None,
            command_dispatch_fn=dispatch,
            agent_dispatch_fn=lambda text, context: state.agents.append(text),
            buffer_dictate_until_commit=buffered,
            hands_free_command_probe_fn=app._probe_hands_free_command,
        )
        result.reset(initial_mode=mode)
        app._session_mode_manager = result
        app.command_mode_active = mode is SessionMode.COMMAND
        return result

    state.show, state.manager = show, manager
    yield state
    sn._cancel_dismiss_timer()
    click_by_name.cancel()


@pytest.mark.parametrize('phrase', PICKS)
@pytest.mark.parametrize('backend', ['uia', 'dom'])
@pytest.mark.parametrize('mode', list(SessionMode))
def test_visible_picks_execute_through_real_session_probe_and_executor(rig, phrase, backend, mode):
    manager = rig.manager(mode)
    if mode is SessionMode.DICTATE:
        manager.dispatch_utterance('unfinished synthetic draft', GOOD)
    rig.show(backend)
    outcome = manager.dispatch_utterance(phrase, GOOD)
    assert outcome.kind in {'hands_free_command_executed', 'command_executed'}
    assert manager.mode is mode
    assert not rig.pastes and not rig.agents
    if mode is SessionMode.DICTATE:
        assert manager.dictate_pending_buffer == 'unfinished synthetic draft'
    if backend == 'uia':
        assert rig.clicks == [(rig.controls[4], 'single', frozenset())]
    else:
        assert rig.dom_clicks == [(5, 'click', {'ctrlKey': False, 'shiftKey': False,
                                             'altKey': False})]
    assert not sn.is_overlay_active() and sn._dismiss_timer is None


@pytest.mark.parametrize('phrase', PICKS + tuple(row[0] for row in MODIFIED) + ('I have 5 apples',))
@pytest.mark.parametrize('exact', [True, False])
def test_hidden_picks_are_exactly_the_original_hands_free_dictation(rig, phrase, exact):
    rig.app.config['hands_free']['exact_commands'] = exact
    manager = rig.manager()
    outcome = manager.dispatch_utterance(phrase, GOOD)
    assert outcome.kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == phrase
    assert not rig.clicks and not rig.dom_clicks and not rig.timers


@pytest.mark.parametrize('phrase,modifier,keys', MODIFIED)
@pytest.mark.parametrize('backend', ['uia', 'dom'])
def test_modifiers_reach_the_existing_click_path(rig, phrase, modifier, keys, backend):
    manager = rig.manager()
    rig.show(backend)
    assert manager.dispatch_utterance(phrase, GOOD).kind == 'hands_free_command_executed'
    if backend == 'uia':
        assert rig.clicks == [(rig.controls[4], modifier, keys)]
    else:
        action = {'single': 'click', 'double': 'doubleclick', 'right': 'rightclick'}[modifier]
        assert rig.dom_clicks == [(5, action, {key + 'Key': key in keys
                                             for key in ('ctrl', 'shift', 'alt')})]


@pytest.mark.parametrize('phrase', ('I have 5 apples', 'click 5 apples', 'choose 5 then 2',
                                   '5 6', 'someone', 'pick', 'click 100', 'twenty twenty'))
def test_overlay_never_extracts_a_number_from_prose(rig, phrase):
    manager = rig.manager()
    rig.show()
    assert manager.dispatch_utterance(phrase, GOOD).kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == phrase
    assert not rig.clicks


@pytest.mark.parametrize('backend', ['uia', 'dom'])
@pytest.mark.parametrize('failure', ['out_of_range', 'stale'])
def test_each_unsuccessful_pick_restarts_thirty_seconds(rig, backend, failure):
    manager = rig.manager()
    rig.show(backend)
    for now in (25, 50):
        previous = sn._dismiss_timer
        rig.now = now
        rig.click_ok = failure != 'stale'
        phrase = 'pick 9' if failure == 'out_of_range' else 'pick 5'
        assert manager.dispatch_utterance(phrase, GOOD).kind == 'hands_free_command_executed'
        assert previous.cancelled
        assert sn._dismiss_timer.deadline == now + 30
        assert sn.is_overlay_active()
    sn._dismiss_timer.callback()
    assert not sn.is_overlay_active()


@pytest.mark.parametrize('backend', ['uia', 'dom'])
def test_show_and_reshow_restart_timeout(rig, monkeypatch, backend):
    monkeypatch.setattr(sn, '_foreground_info', lambda hwnd: {})
    import win32gui
    monkeypatch.setattr(win32gui, 'GetForegroundWindow', lambda: 101)
    if backend == 'dom':
        monkeypatch.setattr(sn, '_try_show_dom_numbers', lambda app: (rig.show('dom') or True, None))
    else:
        monkeypatch.setattr(sn, '_try_show_dom_numbers', lambda app: (False, None))
        monkeypatch.setattr(sn, '_invalidate_cache', lambda: None)
        monkeypatch.setattr(sn, '_cached_enumerate', lambda: rig.controls)
        monkeypatch.setattr(sn, '_enumerate_foreground_clickables', lambda: rig.controls)
        monkeypatch.setattr(sn, '_render_elements_qt',
                            lambda app, elements, *args: sn._elements.extend(elements))
    for now in (0, 25):
        previous = sn._dismiss_timer
        rig.now = now
        assert sn.handle_show_numbers(rig.app, '') is True
        assert sn._dismiss_timer.deadline == now + 30
        if previous is not None:
            assert previous.cancelled


def test_disabled_pack_keeps_the_pick_as_dictation(rig):
    manager = rig.manager()
    rig.executor._matcher.set_enabled_packs(set())
    rig.show()
    assert manager.dispatch_utterance('click 5', GOOD).kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == 'click 5' and not rig.clicks


def test_literal_escape_keeps_a_visible_number_as_dictation(rig):
    manager = rig.manager()
    rig.show()
    assert manager.dispatch_utterance('literal 5', GOOD).kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == '5' and not rig.clicks


@pytest.mark.parametrize('mode', [SessionMode.DICTATE, SessionMode.AVA])
def test_untrusted_pick_is_refused_without_click_or_dictation(rig, mode):
    manager = rig.manager(mode)
    rig.show()
    assert manager.dispatch_utterance('pick 5', BAD).kind == 'hands_free_command_refused'
    assert not manager.dictate_pending_buffer and not rig.clicks and not rig.agents


@pytest.mark.parametrize('phrase', PICKS)
def test_unbuffered_dictation_can_pick_too(rig, phrase):
    manager = rig.manager(buffered=False)
    rig.show()
    assert manager.dispatch_utterance(phrase, GOOD).kind == 'command_executed'
    assert len(rig.clicks) == 1 and not rig.pastes


def test_picks_still_pass_existing_execution_policy(rig, monkeypatch):
    manager = rig.manager()
    rig.show()
    seen = []

    def deny(inv, **kwargs):
        seen.append(inv.command_id)
        return execution_policy.Denied('synthetic denial')

    monkeypatch.setattr(execution_policy, 'authorize', deny)
    assert manager.dispatch_utterance('choose 5', GOOD).kind == 'hands_free_command_failed'
    assert seen == ['click'] and not rig.clicks


def test_number_aliases_are_scoped_and_choose_is_not_overwritten(rig):
    assert rig.executor._matcher.match('number 5')[0] is None
    rig.show()
    assert sn._NUMBERS_TAG in command_scope.active_tags()
    entry, rest = rig.executor._matcher.match('number 5')
    assert entry.phrase == 'pick number' and rest == '5'
    assert entry.scope.tags == {sn._NUMBERS_TAG}
    assert 'choose' not in plugin_commands._MODULE_ENTRIES[sn.__name__]


def test_mouse_grid_keeps_its_existing_bare_number_grammar(rig, monkeypatch):
    rig.show()
    monkeypatch.setattr(sn, 'grid_active', lambda: True)
    assert pick_remainder('five') is None
    assert pick_remainder('five', allow_grid=True) == 'five'
    assert pick_remainder('choose five', allow_grid=True) is None
    assert parse_pick('number twenty-seven.') == 'twenty seven'


@pytest.mark.parametrize('phrase,number', [('pick seven', 7), ('number twelve', 12)])
def test_reviewed_alias_runs_registered_pick_handler_and_clicks(rig, phrase, number):
    rig.controls.extend(object() for _ in range(7))
    rig.show()
    result = rig.executor.process_text(phrase, rig.app, force_commands=True)
    assert result.phrase == 'pick number'
    assert result.state is DispatchState.COMPLETED
    assert rig.clicks == [(rig.controls[number - 1], 'single', frozenset())]
    assert not sn.is_overlay_active()


@pytest.mark.parametrize('phrase', ['pick seven', 'number twelve', 'pick', 'pick five apples'])
def test_scope_tag_alone_never_allows_hidden_pick(rig, monkeypatch, phrase):
    monkeypatch.setattr(rig.executor._matcher, 'current_context', lambda:
                        command_scope.MatchContext.for_app('editor.exe', tags={sn._NUMBERS_TAG}))
    result = rig.executor.process_text(phrase, rig.app, force_commands=True)
    assert result.state is DispatchState.MISS
    assert not rig.clicks and not rig.dom_clicks and not rig.timers
