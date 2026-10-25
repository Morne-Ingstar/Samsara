"""877: command-key receipts through real decode, dispatch and both displays."""
import json
import os
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock

import pytest

from samsara.listening_display import build_display
from samsara.session_modes import outcome_chip
from samsara.ui.listening_indicator import ListeningIndicator
from tests.test_commands_only_key_391 import _app
from tests.test_several_commands_399 import rig as command_rig
from tests.test_dictionary_voice_818 import rig
from tests.test_teach_readback_caption_846 import caption_flow, display_label


@pytest.fixture
def command_display(tmp_path, monkeypatch, qapp, command_rig):
    created = []

    def make(choice, text):
        app, spies = _app(tmp_path, monkeypatch, text)
        executor, _, calls = command_rig
        executor._app = app
        app.command_executor = executor
        app._log_command_dispatch = Mock()
        app.config['ui'] = {'listening_display': choice, 'live_surface': {'enabled': True}}
        build_display(app, ListeningIndicator)
        indicator = app.listening_indicator
        indicator.set_mode('Command')
        indicator.set_listening(True)
        controller = getattr(app, 'live_surface', None)
        now = [0.0]
        if controller:
            controller.model._clock = lambda: now[0]
        else:
            monkeypatch.setattr(indicator, '_now_ms', lambda: now[0] * 1000)

        def snapshot():
            if controller:
                controller.drain()
            qapp.processEvents()
            if controller:
                return controller.view().notice.message
            return indicator._chip_label

        app._display_trace = []
        started = perf_counter()

        def schedule(fn, *args, **kwargs):
            fn(*args, **kwargs)
            if getattr(fn, '__self__', None) is indicator:
                app._display_trace.append((round((perf_counter() - started) * 1000, 2),
                                           fn.__name__, snapshot()))

        app._schedule_ui = schedule
        created.append(app)
        return app, spies, calls, indicator, controller, now, snapshot

    yield make
    for app in created:
        app.listening_indicator.destroy()
    qapp.processEvents()


@pytest.mark.parametrize('choice', ['full', 'compact'])
@pytest.mark.parametrize('text', ['Scroll down.', 'Next tab.', 'scroll down', 'next tab'])
def test_command_key_confirmation_survives_watchdog_and_idle(command_display, choice, text):
    app, spies, calls, indicator, controller, now, snapshot = command_display(choice, text)
    timeline = [(now[0], snapshot())]
    # Real release/decode/dispatch and its pending-chip watchdog, inert OS handlers.
    app._stop_recording_impl()
    timeline.append((now[0], snapshot()))
    assert len(calls) == 1
    assert calls[0][1] == ''  # No punctuation remainder reaches the command.
    phrase = calls[0][0]
    expected = str(outcome_chip('command_executed',
        {'phrase': phrase, 'heard': text, 'state': 'completed'})[0])
    for seconds in (0.1, 1.0, 2.49):
        now[0] = seconds
        indicator.set_listening(False)
        if controller:
            controller.refresh_expired()
        timeline.append((seconds, snapshot()))
    evidence = os.environ.get('GATE_RESULT')
    if evidence:
        name = f'877_{choice}_{text.lower().replace(" ", "_").replace(".", "_period")}.json'
        Path(evidence).with_name(name).write_text(json.dumps(
            {'checkpoints_seconds': timeline, 'emissions_ms': app._display_trace}, indent=2), encoding='utf-8')
    assert all(label == expected for _, label in timeline[1:]), timeline
    assert all(label not in ('.', '...') for _, label in timeline[1:])
    assert spies.model.transcribe.call_count == 1
    app._paste_preserving_clipboard.assert_not_called()
    expires = controller.view().notice.expires_at if controller else indicator._chip_deadline
    assert expires >= 2.5
    now[0] = expires - .001
    assert snapshot() == expected
    now[0] = expires
    if not controller:
        indicator._clear_outcome()
    assert not snapshot()


@pytest.mark.parametrize('choice', ['full', 'compact'])
def test_failed_command_retains_specific_reason(command_display, monkeypatch, choice):
    from samsara.commands import DispatchResult, DispatchState
    app, _, _, _, _, _, snapshot = command_display(choice, 'Scroll down.')
    monkeypatch.setattr(app.command_executor, 'execute_canonical', lambda *a, **kw:
        DispatchResult(DispatchState.FAILED, 'scroll down', 'scroll down',
                       {'reason': 'Synthetic scroll failure'}))
    app._stop_recording_impl()
    assert 'Synthetic scroll failure' in snapshot()
    assert 'ran scroll down' not in snapshot()


@pytest.mark.parametrize('choice', ['full', 'compact'])
def test_plugin_owned_result_is_not_replaced(command_display, monkeypatch, choice):
    from samsara.commands import DispatchResult, DispatchState
    app, _, _, _, _, _, snapshot = command_display(choice, 'Scroll down.')

    def execute(*a, **kw):
        app._show_outcome_chip('Synthetic plugin result', 'success')
        return DispatchResult(DispatchState.COMPLETED, 'scroll down', 'scroll down')

    monkeypatch.setattr(app.command_executor, 'execute_canonical', execute)
    app._stop_recording_impl()
    assert snapshot() == 'Synthetic plugin result'


@pytest.mark.parametrize('choice', ['full', 'compact'])
def test_watchdog_cannot_clear_result_queued_before_first_paint(command_display, choice):
    app, _, calls, _, _, _, snapshot = command_display(choice, 'Scroll down.')
    queued = []
    app._schedule_ui = lambda fn, *a, **kw: queued.append((fn, a, kw))
    app._stop_recording_impl()
    assert len(calls) == 1
    assert app._hold_chip_resolved_seq == app._hold_chip_seq
    assert not any(fn.__name__ in ('clear_outcome', 'clear_feedback') for fn, _, _ in queued)
    for fn, args, kwargs in queued:
        fn(*args, **kwargs)
    assert 'ran scroll down' in snapshot()


@pytest.mark.parametrize('choice', ['full', 'compact'])
def test_spoken_teaching_question_keeps_its_readback(caption_flow, qapp, choice):
    from plugins.commands import ask_ollama as ava
    from samsara.commands import DispatchResult, DispatchState
    from samsara.result_messages import report_held_command

    flow = caption_flow
    engine = flow.build(choice)
    flow.app._notify_tutorial = Mock()
    assert ava._check_teaching_intent(flow.app, 'spell word A T L A S')
    expected = flow.app._ava_question.text + ' Say yes or no'
    report_held_command(flow.app,
        DispatchResult(DispatchState.COMPLETED, 'spell word', 'spell word'), 'spell word A T L A S')
    assert display_label(flow, qapp) == expected
    assert engine.speak.called


@pytest.mark.parametrize('state', ['queued', 'matched', 'failed', 'rejected', 'cancelled', 'miss'])
def test_incomplete_dispatch_never_gets_a_success_receipt(state):
    from samsara.commands import DispatchResult
    from samsara.result_messages import report_held_command
    app = Mock(_hold_chip_seq=1, _hold_chip_resolved_seq=0, _ava_question=None)
    report_held_command(app, DispatchResult(state, 'scroll down', 'scroll down'), 'Scroll down.')
    app._show_dispatch_outcome_chip.assert_not_called()
