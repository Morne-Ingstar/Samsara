"""825f: ordinary policy confirmations reuse the local question microphone lease."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_pending, command_catalog as cc, execution_policy as policy, plugin_commands
from samsara.plugin_commands import load_plugins as load_shipped_plugins
from samsara.commands import CommandExecutor
from samsara.command_registry import DispatchState

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = json.loads((ROOT / 'commands.json').read_text(encoding='utf-8'))['commands']
REAL_SPEAK = ava.speak


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ava.clear_pending_action()
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps({'commands': {
        'risky action': {'type': 'method', 'method': 'risky_action'},
        'other command': {'type': 'method', 'method': 'show_tutorial'},
    }}), encoding='utf-8')
    executor = CommandExecutor(path, plugins_dir=tmp_path / 'none')
    now, callbacks, spoken, timers = [100.0], [], [], []
    app = SimpleNamespace(config={'ava': {'enabled': False}}, command_executor=executor,
        _ava_cmd_generation=0, app_state='asleep', wake_word_triggered=False,
        command_mode_active=False, command_matching_enabled=True,
        _ensure_wake_consumer=Mock(), _release_wake_consumer=Mock(),
        _schedule_ui=lambda fn, *a: fn(*a), listening_indicator=Mock(),
        risky_action=Mock(), show_tutorial=Mock())

    def speak(_app, text, **kw):
        spoken.append(text)
        if kw.get('on_done'):
            callbacks.append(kw['on_done'])
        return True

    def timer(_name, delay, fn, **kw):
        item = SimpleNamespace(cancel=Mock(), fire=fn)
        timers.append(item)
        return item

    monkeypatch.setattr(ava, 'speak', speak)
    monkeypatch.setattr(ava_pending, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ava_pending.thread_registry, 'timer', timer)
    yield SimpleNamespace(app=app, executor=executor, now=now, callbacks=callbacks,
                          spoken=spoken, timers=timers)
    q = getattr(app, '_ava_question', None)
    if q:
        ava_pending._clear(app, q, 'Cancelled.')
    ava.clear_pending_action()


def stage(rig):
    result = rig.executor.execute_canonical('risky action', rig.app)
    assert result.state is DispatchState.QUEUED
    q = rig.app._ava_question
    assert q.kind == 'policy' and not q.opened
    assert 'Say yes to confirm or no to cancel.' in rig.spoken[-1]
    rig.app._ensure_wake_consumer.assert_not_called()
    rig.callbacks[-1]()
    assert q.opened and q.deadline == rig.now[0] + 6
    rig.app._ensure_wake_consumer.assert_called_once_with('ava_question')
    assert (rig.app.app_state, rig.app.wake_word_triggered) == ('command_window', True)
    assert rig.app.listening_indicator.show_outcome.call_args.args[0] == 'Say yes or no'
    return q


def assert_cleared(rig):
    assert rig.app._ava_question is None
    assert policy.pending_operation() is None
    rig.app._release_wake_consumer.assert_called_with('ava_question')
    assert (rig.app.app_state, rig.app.wake_word_triggered) == ('asleep', False)
    assert rig.app.listening_indicator.show_outcome.call_args.args[0] != 'Say yes or no'


@pytest.mark.parametrize('lane', ['wake', 'command_key', 'ava_hold', None])
def test_yes_executes_once_without_ai_or_another_key(rig, lane):
    stage(rig)
    result = rig.executor.process_text('yes.', rig.app, force_commands=True, capture_path=lane)
    assert result.state is DispatchState.COMPLETED
    assert result.phrase == 'pending_reply' and result.detail['answer'] == 'approved'
    rig.app.risky_action.assert_called_once_with()
    assert_cleared(rig)
    assert not ava_pending.consume(rig.app, 'yes')


@pytest.mark.parametrize('text', ['yes but not now', 'yeah I think so', 'do it later maybe',
                                  'I said yes to the dress', '"yes"', "'yes'", 'ordinary words'])
def test_non_answers_remain_unconsumed_and_keep_the_question(rig, text):
    q = stage(rig)
    op = policy.pending_operation()
    assert policy.answer_pending(rig.app, text) is None
    assert ava_pending.consume(rig.app, text, capture_path='wake') is False
    result = rig.executor.process_text(text, rig.app, force_commands=True)
    assert result.state is DispatchState.MISS
    assert rig.app._ava_question is q and policy.pending_operation() is op
    assert op.approved is None
    rig.app._release_wake_consumer.assert_not_called()
    rig.app.risky_action.assert_not_called()


def test_keyed_yes_before_readback_executes_and_late_callback_cannot_reopen(rig):
    rig.executor.execute_canonical('risky action', rig.app)
    assert not rig.app._ava_question.opened
    result = rig.executor.process_text('yes', rig.app, force_commands=True, capture_path='command_key')
    assert result.state is DispatchState.COMPLETED and result.detail['answer'] == 'approved'
    rig.app.risky_action.assert_called_once_with()
    rig.callbacks[-1]()
    assert rig.app._ava_question is None and policy.pending_operation() is None
    rig.app._ensure_wake_consumer.assert_not_called()


def test_executor_host_without_microphone_keeps_keyed_confirmation(rig, monkeypatch):
    spoken = []

    class Coordinator:
        def speak(self, text, category=None, interruptible=None):
            spoken.append((text, category))
            return SimpleNamespace(utterance_id='test')

    del rig.app._ensure_wake_consumer
    rig.app.audio_coordinator = Coordinator()
    monkeypatch.setattr(ava, 'speak', REAL_SPEAK)
    result = rig.executor.execute_canonical('risky action', rig.app)
    assert result.state is DispatchState.QUEUED and policy.pending_operation() is not None
    assert spoken == [('Risky action? Say yes to confirm or no to cancel.', 'confirmation')]
    result = rig.executor.process_text('yes', rig.app, force_commands=True)
    assert result.state is DispatchState.COMPLETED and result.detail['answer'] == 'approved'
    rig.app.risky_action.assert_called_once_with()


@pytest.mark.parametrize('retirement', ['expired', 'stale'])
def test_keyed_refusal_preserves_dispatch_details(rig, retirement):
    stage(rig)
    op = policy.pending_operation()
    if retirement == 'expired':
        op.deadline = 0
    else:
        rig.app._ava_cmd_generation += 1
    result = rig.executor.process_text('yes', rig.app, force_commands=True)
    assert result.state is DispatchState.REJECTED
    assert result.detail['answer'] == 'refused:' + retirement
    rig.app.risky_action.assert_not_called()
    assert_cleared(rig)


def test_no_cancels_and_clears(rig):
    stage(rig)
    assert ava_pending.consume(rig.app, 'no')
    rig.app.risky_action.assert_not_called()
    assert_cleared(rig)


def test_timeout_speaks_cancelled_and_late_callback_cannot_reopen(rig):
    q = stage(rig)
    rig.now[0] += 6.01
    ava_pending._tick(rig.app, q)
    assert rig.spoken[-1] == 'Cancelled.'
    rig.app.risky_action.assert_not_called()
    assert_cleared(rig)
    rig.callbacks[-1]()
    assert rig.app._ava_question is None


@pytest.mark.parametrize('before_readback', [False, True])
def test_new_command_cancels_old_question_then_executes(rig, before_readback):
    if before_readback:
        rig.executor.execute_canonical('risky action', rig.app)
    else:
        stage(rig)
    result = rig.executor.process_text('other command', rig.app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    rig.app.show_tutorial.assert_called_once_with()
    rig.app.risky_action.assert_not_called()
    assert rig.app._ava_question is None and policy.pending_operation() is None
    rig.callbacks[-1]()
    assert rig.app._ava_question is None


@pytest.mark.parametrize('retirement', ['generation', 'stop', 'direct_no', 'superseded'])
def test_retirement_clears_the_window(rig, retirement):
    q = stage(rig)
    if retirement == 'generation':
        rig.app._ava_cmd_generation += 1
        ava_pending._tick(rig.app, q)
    elif retirement == 'stop':
        policy.stop_all(rig.app)
    elif retirement == 'direct_no':
        policy.answer_pending(rig.app, 'no')
    else:
        successor = policy.stage_pending(rig.app, policy.Invocation('other', generation=0), 'Other?')
        assert policy.pending_operation() is successor
        policy._release_slot(successor)
    rig.app.risky_action.assert_not_called()
    assert_cleared(rig)


def test_changed_target_is_still_refused(rig):
    stage(rig)
    op = policy.pending_operation()
    op.targets, op._target_probe = {'version': 1}, lambda: {'version': 2}
    assert ava_pending.consume(rig.app, 'yes', capture_path='wake')
    rig.app.risky_action.assert_not_called()
    assert_cleared(rig)


def test_wait_extends_once_and_keeps_capture_owned(rig):
    q = stage(rig)
    assert ava_pending.consume(rig.app, 'wait')
    assert q.opened and q.deadline == rig.now[0] + ava_pending.reply_seconds(rig.app)
    assert ava_pending.consume(rig.app, 'yes')
    rig.app.risky_action.assert_called_once_with()
    assert_cleared(rig)


def test_catalogue_has_no_unexplained_unknown_risks(monkeypatch):
    # 825f scope permits non-819 fixes only for obvious UI opens. Keep the
    # three non-UI exceptions visible; never declare arbitrary repeats safe.
    exceptions = {'redo that': 'redo_last_dictation',
                  'repeat': 'repeat_last_command', 'again': 'repeat_last_command'}
    # 888b: unknown records may be non-method commands; keep them visible to the assertion.
    unknown = {p: c.get('method', c.get('type')) for p, c in COMMANDS.items()
               if policy.classify_builtin(c) == policy.RISK_UNKNOWN}
    assert unknown == exceptions
    document = json.loads((ROOT / 'commands_catalog.json').read_text(encoding='utf-8'))
    assert all(c['risk'] in {'ui', 'read', 'write', 'destructive'} for c in document['commands'])
    monkeypatch.setattr(plugin_commands, 'load_plugins', load_shipped_plugins)
    executor = CommandExecutor(ROOT / 'commands.json')
    resolved = {}
    for entry in executor._matcher._sorted:
        plugin = 'builtin' if entry.source == 'builtin' else cc._plugin_stem(entry)
        resolved[f'{plugin}.{cc.slug(entry.phrase)}'] = entry.phrase
    assert set(resolved) == {row['canonical_id'] for row in document['commands']}
    unknown_catalogue = {cid: phrase for cid, phrase in resolved.items()
                         if policy.classify(phrase, executor=executor)[0] == 'unknown'}
    assert unknown_catalogue == {'builtin.' + cc.slug(p): p for p in exceptions}



@pytest.mark.parametrize('phrase,command', [
    (p, c) for p, c in COMMANDS.items() if c.get('samsara_destination')])
def test_every_navigation_command_is_allowed_by_actual_policy(phrase, command):
    executor = SimpleNamespace(commands=COMMANDS)
    app = SimpleNamespace(_ava_cmd_generation=0)
    decision = policy.authorize(policy.Invocation(phrase, generation=0), executor=executor, app=app)
    # 888b / 772: reviewed local navigation is read-only; external/action UI stays UI.
    from samsara.samsara_navigation import PLACES
    destination = command['samsara_destination']
    place = PLACES[destination]
    expected = 'read' if (place.kind in {'hub', 'settings', 'tool', 'reference', 'guides',
                                       'tutorial', 'reminders', 'tasks'}
                         or destination in {'quick_reference', 'diagnostics', 'live_log', 'log',
                                            'benchmark_review', 'transcribe_file'}) else 'ui'
    assert isinstance(decision, policy.Allowed) and decision.risk == expected


def test_risk_metadata_or_unknown_method_names_cannot_bypass_policy():
    # 823 / 890: receipt-guarded deletion is still a write; selection is UI.
    assert policy.classify_builtin({'type': 'text_selection', 'selection_action': 'delete that'}) == 'write'
    assert policy.classify_builtin({'type': 'text_selection', 'selection_action': 'select that'}) == 'ui'
    assert policy.classify_builtin({'type': 'text_selection', 'selection_action': 'unreviewed'}) == 'unknown'
    assert policy.classify_builtin({'type': 'method', 'method': 'open_samsara_unreviewed',
                                   'risk_class': 'ui'}) == policy.RISK_UNKNOWN
    assert policy.classify_builtin({'type': 'hotkey', 'keys': ['shift', 'delete'],
                                   'risk_class': 'ui'}) == policy.RISK_DESTRUCTIVE
    # 888b / 772: opening the reviewed file-transcription UI is read-only.
    assert policy.classify_builtin({'type': 'method', 'method': 'open_transcribe_file'}) == 'read'


@pytest.mark.parametrize('choice', ['full', 'compact'])
@pytest.mark.parametrize('answer', ['yes', 'no', 'timeout'])
def test_real_displays_clear_the_question(rig, qapp, choice, answer):
    from samsara.listening_display import build_display
    from samsara.ui.listening_indicator import ListeningIndicator
    rig.app.config['ui'] = {'listening_display': choice, 'live_surface': {'enabled': True}}
    build_display(rig.app, ListeningIndicator)
    indicator = rig.app.listening_indicator
    controller = getattr(rig.app, 'live_surface', None)
    widget = controller.widget if controller else indicator
    try:
        rig.executor.execute_canonical('risky action', rig.app)
        rig.callbacks[-1]()
        if controller:
            controller.drain()
        qapp.processEvents()
        label = controller.view().notice.message if controller else indicator._chip_label
        assert label == 'Say yes or no'
        if answer == 'timeout':
            rig.now[0] += 7
            ava_pending._tick(rig.app, rig.app._ava_question)
        else:
            ava_pending.consume(rig.app, answer)
        if controller:
            controller.drain()
        qapp.processEvents()
        label = controller.view().notice.message if controller else indicator._chip_label
        assert label != 'Say yes or no'
        assert rig.app._ava_question is None and policy.pending_operation() is None
    finally:
        widget.close()
        widget.deleteLater()
