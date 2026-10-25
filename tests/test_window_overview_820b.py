"""820b: real dispatch/policy, native geometry seams and completion-paced audio."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import window_switcher as ws, windows as mover, window_cube as cube
from samsara import execution_policy as policy, plugin_commands
from samsara.commands import CommandExecutor
from samsara.command_registry import DispatchState
from samsara.command_scope import MatchContext
from samsara.window_overview_flow import ReadSequence
from tests.test_window_overview_820 import overview, _matcher, FakeDwm

_snap_window = mover.snap_window
_native_monitor = ws._overview_monitor


@pytest.fixture
def executor(overview, tmp_path, monkeypatch):
    for module in (ws, cube):
        plugin_commands._reinstall_module_commands(module)
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps({'commands': {'copy': {'type': 'press', 'key': 'c'}}}))
    executor = CommandExecutor(path, plugins_dir=tmp_path / 'no-plugins')
    executor._matcher = _matcher()
    monkeypatch.setattr(executor._matcher, 'current_context', lambda: MatchContext(
        tags=frozenset({'window_overview.visible', 'window_cube.visible'})
            if ws.overview_active(overview[0]) else frozenset()))
    monkeypatch.setattr(policy, 'retire_confirmation', Mock())
    monkeypatch.setattr(policy, 'pending_operation', lambda: None)
    monkeypatch.setattr(executor, '_stage_confirmation', Mock())
    return executor


@pytest.mark.parametrize('reply', ['yes', 'confirm', 'confirm close'])
def test_close_has_one_local_prompt_through_real_dispatch(executor, overview, reply):
    import win32gui
    app, _, panel = overview
    start = time.perf_counter()
    result = executor.process_text('close three', app, force_commands=True)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert result.state is DispatchState.COMPLETED
    executor._stage_confirmation.assert_not_called()
    prompts = [call.args[0] for call in panel.confirmation.call_args_list if call.args[0]]
    assert len(prompts) == 1
    app.audio_coordinator.speak.assert_called_once_with(
        'Confirm', category='confirmation', queue_mode='append')
    app.audio_coordinator.cancel_speech.assert_called_once()
    assert elapsed_ms < 1000  # fake audio submission only, not acoustic latency
    print(f'820b close-three dispatch-to-submission: {elapsed_ms:.3f} ms (fake audio)')
    assert executor.process_text(reply, app, force_commands=True).state is DispatchState.COMPLETED
    win32gui.PostMessage.assert_called_once_with(103, 0x0010, 0, 0)
    executor._stage_confirmation.assert_not_called()


@pytest.mark.parametrize('reply', ['no', 'cancel', 'hide windows', 'three left'])
def test_new_command_or_rejection_abandons_close(executor, overview, reply, monkeypatch):
    import win32gui
    app, _, _ = overview
    monkeypatch.setattr(ws, '_dismiss_all', Mock())
    executor.process_text('close three', app, force_commands=True)
    executor.process_text(reply, app, force_commands=True)
    executor.process_text('yes', app, force_commands=True)
    win32gui.PostMessage.assert_not_called()
    assert ws._overview_pending_close is None


def test_confirmation_opt_out_is_exact_only_and_scope_still_blocks(executor, overview):
    app, _, _ = overview
    generation = policy.capture_generation(app)
    exact = policy.Invocation('close 3', {}, policy.Route.EXACT, generation)
    assert policy.authorize(exact, app=app, executor=executor).reason == 'handler_confirmation'
    model = policy.Invocation('close 3', {}, policy.Route.MODEL, generation)
    assert not isinstance(policy.authorize(model, app=app, executor=executor), policy.Allowed)
    ws._overview_visible = False
    assert isinstance(policy.authorize(exact, app=app, executor=executor), policy.Denied)


def monitors():
    return [dict(index=1, device=r'\\.\DISPLAY3', handle=3, primary=False,
                 rect=(-1920, 0, 0, 1080), width=1920, height=1080),
            dict(index=2, device=r'\\.\DISPLAY1', handle=1, primary=True,
                 rect=(0, 0, 1920, 1080), width=1920, height=1080),
            dict(index=3, device=r'\\.\DISPLAY2', handle=2, primary=False,
                 rect=(1920, 0, 3840, 1080), width=1920, height=1080)]


@pytest.mark.parametrize('state', ['normal', 'minimized', 'maximized'])
def test_left_stays_on_native_middle_monitor_before_restore(overview, monkeypatch, state):
    import win32api
    import win32gui
    import win32con
    app, _, _ = overview
    screens = monitors()
    rect = [100, 100, 900, 700]
    monkeypatch.setattr(mover, 'get_monitors', lambda: screens)
    monkeypatch.setattr(win32api, 'MonitorFromWindow', lambda hwnd, flags: 1)
    monkeypatch.setattr(ws, '_overview_monitor', _native_monitor)
    monkeypatch.setattr(mover, 'snap_window', _snap_window)
    monkeypatch.setattr(win32gui, 'IsIconic', lambda hwnd: state == 'minimized')
    monkeypatch.setattr(win32gui, 'GetWindowPlacement', lambda hwnd:
        (0, win32con.SW_SHOWMAXIMIZED if state == 'maximized' else 1, (), (), ()))
    monkeypatch.setattr(win32gui, 'GetWindowRect', lambda hwnd: tuple(rect))
    monkeypatch.setattr(win32gui, 'ShowWindow', lambda *args: rect.__setitem__(slice(None), [-1800, 100, -1000, 700]))
    moved = Mock(side_effect=lambda hwnd, z, x, y, w, h, flags:
        rect.__setitem__(slice(None), [x, y, x + w, y + h]))
    monkeypatch.setattr(win32gui, 'SetWindowPos', moved)
    monkeypatch.setattr(mover, 'raise_window', lambda *args, **kw: True)
    parse = Mock(side_effect=AssertionError('snap direction parsed as monitor'))
    monkeypatch.setattr(ws, '_parse_monitor_index', parse)
    assert ws.handle_overview_number(app, 3, 'left')
    assert tuple(rect) == (0, 0, 960, 1080)
    moved.assert_called_once()
    mover.move_window_to_monitor.assert_not_called()
    parse.assert_not_called()


@pytest.mark.parametrize('phrase,action', [
    ('three left on one', 'left on one'), ('three to one', 'to one'),
    ('three top right on main', 'top right on main'),
    ('three maximize on two', 'maximize on two')])
def test_on_to_forms_use_existing_scoped_number_route(overview, monkeypatch, phrase, action):
    app, _, _ = overview
    entry, rest = _matcher().match(phrase, context=MatchContext(
        tags=frozenset({'window_overview.visible', 'window_cube.visible'})))
    invoked = Mock(return_value=True)
    monkeypatch.setattr(ws, 'overview_action', invoked)
    assert entry.handler(app, rest)
    assert invoked.call_args.args[:3] == (app, 3, action)


def test_explicit_targets_use_windows_number_and_primary_not_left_to_right(overview, monkeypatch):
    app, _, _ = overview
    screens = monitors()
    monkeypatch.setattr(mover, 'get_monitors', lambda: screens)
    monkeypatch.setattr(ws, '_overview_keep', True)
    assert ws.overview_monitor_target('one', screens) is screens[1]
    assert ws.overview_monitor_target('main', screens) is screens[1]
    assert ws.overview_action(app, 3, 'left on one')
    assert mover.snap_window.call_args.kwargs['target_monitor'] is screens[1]
    mover.move_window_to_monitor.assert_not_called()
    assert ws.overview_action(app, 3, 'to one')
    assert mover.move_window_to_monitor.call_args.args == (103, screens[1])
    assert ws._parse_monitor_index('left') is None


def test_names_only_and_pacing_one_outstanding_item(monkeypatch):
    rows = [dict(number=i, app='Microsoft Editor', title='private', short_title='private')
            for i in range(1, 9)]
    rows[1]['app'] = 'Google Chrome'
    chunks = ws.overview_read_chunks(rows)
    assert chunks == ['1, Editor', '2, Chrome'] + [f'{i}, Editor' for i in range(3, 9)]
    coordinator = Mock()
    coordinator.speak.side_effect = lambda text, **kwargs: (
        kwargs['on_done']() or SimpleNamespace(utterance_id='test'))
    highlight = Mock()
    sequence = ReadSequence(coordinator, list(enumerate(chunks, 1)), highlight)
    pauses = []
    monkeypatch.setattr(sequence, '_pause', lambda seconds: pauses.append(seconds) or False)
    sequence.run()
    assert [call.args[0] for call in coordinator.speak.call_args_list] == chunks
    assert pauses == [.25] * 5 + [1.5, .25]
    assert [call.args[0] for call in highlight.call_args_list if call.args[0]] == list(range(1, 9))
    assert highlight.call_args.args == (None,)


def test_cancel_unblocks_current_audio_and_never_queues_remaining(monkeypatch):
    coordinator = Mock()
    submitted = threading.Event()
    completion = []
    def speak(text, **kw):
        completion.append(kw['on_done'])
        submitted.set()
        return SimpleNamespace(utterance_id='test')
    coordinator.speak.side_effect = speak
    sequence = ReadSequence(coordinator, [(1, '1, Editor'), (2, '2, Chrome')], Mock())
    worker = threading.Thread(target=sequence.run)
    worker.start()
    try:
        assert submitted.wait(1)
        sequence.cancel()
        completion[0]()  # late playback completion cannot revive the sequence
        worker.join(1)
        assert not worker.is_alive()
        coordinator.speak.assert_called_once()
        coordinator.cancel_speech.assert_called_once()
    finally:
        sequence.cancelled.set()
        worker.join(1)


@pytest.mark.parametrize('text', ['stop', 'cancel', 'hide windows', 'copy', 'three left'])
def test_any_new_dispatch_drains_read_queue(executor, overview, monkeypatch, text):
    app, _, _ = overview
    reader = Mock()
    monkeypatch.setattr(ws, '_overview_reader', reader)
    monkeypatch.setattr(ws, '_dismiss_all', Mock())
    # Keep all OS/clipboard effects behind the mocked builtin handler.
    monkeypatch.setattr('samsara.commands.get_handler', lambda kind: Mock())
    executor.process_text(text, app, force_commands=True)
    reader.cancel.assert_called_once()
    assert ws._overview_reader is None


def test_noop_speech_does_not_stall_or_repeat(monkeypatch):
    coordinator = Mock()
    coordinator.speak.return_value = SimpleNamespace(utterance_id='noop-tts-disabled')
    sequence = ReadSequence(coordinator, [(1, '1, Editor')], Mock())
    sequence.run()
    coordinator.speak.assert_called_once()


def test_prompt_latency_probe_measures_playback_not_submission(monkeypatch, caplog):
    import logging
    from samsara import window_overview_flow as flow
    monkeypatch.setattr(flow.thread_registry, 'spawn', lambda name, run, **kw: run())
    with caplog.at_level(logging.INFO):
        flow.observe_audio_start(SimpleNamespace(audio_started_ms=432.1), time.perf_counter())
    assert 'audio started: elapsed_ms=432.1' in caplog.text


def test_read_focus_and_stale_gui_updates(qapp, overview, monkeypatch):
    from samsara.ui import window_overview_qt as ui
    window = ui.OverviewWindow(overview[1], Mock(), Mock(), thumbnails=ui.DwmThumbnails(FakeDwm()))
    window.focus_reading(2)
    assert [number for number, tile in window._tiles.items() if tile.property('reading')] == [2]
    window.focus_reading(None)
    assert not any(tile.property('reading') for tile in window._tiles.values())
    pending = []
    monkeypatch.setattr(ui.qt_runtime, 'post', pending.append)
    panel = ui.OverviewPanel()
    panel.window = Mock()
    panel.focus_reading(2)
    panel.confirmation('Old question')
    panel.hide()
    for callback in pending:
        callback()
    panel.window.focus_reading.assert_not_called()
    panel.window.show_confirmation.assert_not_called()
    window.close()
