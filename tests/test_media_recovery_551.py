"""551 regression coverage uses fake playback and fake pipe replies only."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest
from samsara import audio_ducking as ad
from samsara import media_pause as mp


class Operation:
    """WinRT-like awaitable deliberately not accepted as a coroutine."""
    def __init__(self, result):
        self.result = result

    def __await__(self):
        async def done():
            return self.result
        return done().__await__()


def test_manager_pause_and_resume_accept_non_coroutine_awaitables(monkeypatch):
    # The immediate fake operation needs no cross-thread wake pipe. This keeps
    # asyncio.run real without opening loopback sockets forbidden by the gate.
    class LocalLoop(asyncio.SelectorEventLoop):
        def __init__(self):
            super().__init__()
            # Windows select() rejects empty descriptor sets. These fake
            # operations complete from the ready queue and perform no I/O.
            self._selector.select = lambda timeout=None: []

        def _make_self_pipe(self):
            pass

        def _close_self_pipe(self):
            pass

    monkeypatch.setattr(asyncio.events,'new_event_loop',LocalLoop)
    operation = Operation(SimpleNamespace(get_sessions=lambda: ['synthetic']))
    assert not asyncio.iscoroutine(operation)
    layer = mp.WinRTMediaSessionLayer.__new__(mp.WinRTMediaSessionLayer)
    layer._manager = None
    request = Mock(return_value=operation)
    layer._manager_type = SimpleNamespace(request_async=request)
    assert layer.list_sessions() == ['synthetic']
    assert layer.list_sessions() == ['synthetic']
    request.assert_called_once()
    session = SimpleNamespace(try_pause_async=lambda: Operation(True),
                              try_play_async=lambda: Operation(False))
    assert layer.pause(session) is True
    assert layer.resume(session) is False


class Replies:
    def __init__(self):
        self.timeouts = []
        self.request = None

    def get(self, timeout):
        self.timeouts.append(timeout)
        return json.dumps(dict(id=self.request['id'],ok=True,elapsed_ms=0))


def fake_transport(monkeypatch):
    replies = Replies()
    pipe = SimpleNamespace(write=lambda text: setattr(replies,'request',json.loads(text)),flush=lambda: None)
    transport = ad.DuckingHostTransport()
    def spawn():
        transport._proc = SimpleNamespace(stdin=pipe)
        transport._generation += 1
        transport._replies = replies
    monkeypatch.setattr(transport,'_spawn_locked',spawn)
    monkeypatch.setattr(ad.flight_recorder,'record',Mock())
    return transport,replies


def test_each_cold_generation_gets_startup_budget_but_warm_calls_do_not(monkeypatch):
    transport,replies = fake_transport(monkeypatch)
    assert transport.request('list_sessions')['ok']
    assert replies.timeouts[-1] > ad._CHILD_DEADLINE_S
    assert transport.request('list_sessions')['ok']
    assert 0 < replies.timeouts[-1] <= ad._CHILD_DEADLINE_S
    transport._proc = None  # Simulate a previous generation having been discarded.
    assert transport.request('list_sessions')['ok']
    assert replies.timeouts[-1] > ad._CHILD_DEADLINE_S


def test_explicit_deadline_is_not_extended_on_a_cold_child(monkeypatch):
    transport,replies = fake_transport(monkeypatch)
    assert transport.request('ping',deadline=0.25)['ok']
    assert 0 < replies.timeouts[-1] <= 0.25


def test_cold_timeout_discards_generation_without_in_process_com(monkeypatch):
    transport,replies = fake_transport(monkeypatch)
    def expired(timeout):
        raise ad.queue.Empty
    replies.get = expired
    clock = iter([0,0,1,2,3])
    monkeypatch.setattr(ad.time,'monotonic',lambda: next(clock))
    killed = Mock()
    monkeypatch.setattr(ad,'_kill_process_tree',killed)
    assert not transport.request('list_sessions',deadline=0.5)['ok']
    assert transport._proc is None
    killed.assert_called_once()


def test_recording_duck_start_and_finish_do_not_wait_for_enumeration():
    entered,released,stopped = threading.Event(),threading.Event(),threading.Event()
    def start():
        entered.set()
        assert released.wait(2)
    ducker = SimpleNamespace(start=start,stop=stopped.set,last_error=None)
    recording = mp.RecordingDucker(lambda: ducker,Mock())
    try:
        recording.start()
        assert entered.wait(1)
        recording.finish()
        assert not stopped.is_set()
        released.set()
        recording.wait()
        assert stopped.is_set()
    finally:
        released.set()
        recording.finish()
        recording.wait()


def test_finish_before_duck_start_never_acquires_a_lease():
    factory = Mock()
    recording = mp.RecordingDucker(factory,Mock())
    recording.finish()
    recording.start()
    factory.assert_not_called()


def test_pause_failure_reports_step_and_fallback_finish_cancels_late_duck():
    failed,fallback,finish = Mock(),Mock(),Mock()
    pauser = mp.RecordingMediaPauser(layer_factory=lambda: SimpleNamespace(
        list_sessions=Mock(side_effect=OSError('synthetic list failure'))),
        fallback=fallback,fallback_finish=finish,on_failure=failed)
    pauser.start()
    pauser.wait()
    pauser.finish()
    failed.assert_called_once_with('list_sessions','synthetic list failure')
    fallback.assert_called_once()
    finish.assert_called_once()


def test_audio_failure_is_queued_and_shown_once_per_process(monkeypatch):
    monkeypatch.setattr(ad,'_media_notice_shown',False)
    monkeypatch.setattr(ad,'_notice_sink',None)
    monkeypatch.setattr(ad,'_pending_notices',[])
    monkeypatch.setattr(ad.flight_recorder,'record',Mock())
    ad._recording_media_failed('list_sessions','synthetic failure')
    ad._recording_media_failed('recording duck','synthetic timeout')
    shown = []
    ad.set_notice_sink(shown.append)
    ad.set_notice_sink(shown.append)
    assert len(shown) == 1
    assert 'Other audio' in shown[0]


@pytest.mark.parametrize('mode',['pause','duck'])
def test_recording_policy_is_asynchronous_and_restore_finishes_its_owner(monkeypatch,mode):
    monkeypatch.setattr(ad,'_recording_media_pauser',None)
    monkeypatch.setattr(ad,'_shim_ducker',None)
    jobs = []
    monkeypatch.setattr(mp.thread_registry,'spawn',lambda *a,**kw: jobs.append(a) or SimpleNamespace())
    monkeypatch.setattr(ad,'SessionDucker',Mock(side_effect=AssertionError('must run on worker')))
    ad.start_recording_media(mode)
    owner = ad._recording_media_pauser
    assert len(jobs) == 1
    ad.restore()
    assert ad._recording_media_pauser is None
    assert (owner._finish_requested if mode=='pause' else owner._finished).is_set()


def test_both_display_branches_register_only_after_construction():
    source = ast.parse((Path(__file__).parents[1]/'dictation.py').read_text(encoding='utf-8'))
    fn = next(node for node in ast.walk(source) if isinstance(node,ast.FunctionDef) and node.name=='_init_indicator')
    branch = fn.body[0]
    assert isinstance(branch,ast.If) and 'build_display' in ast.unparse(branch.test)
    calls = [branch.body[0],fn.body[1]]
    for node in calls:
        assert 'set_notice_sink' in ast.unparse(node)
        assert "self._schedule_ui(self._show_outcome_chip, message, 'warning')" in ast.unparse(node)


def test_warning_sink_scheduler_delivers_on_qt_thread(qapp):
    from dictation import DictationApp  # Maintained no-bootstrap adapter only.
    delivered = []
    app = SimpleNamespace(_running=True)
    owner = threading.get_ident()
    worker = threading.Thread(target=lambda: DictationApp._schedule_ui(
        app,lambda: delivered.append(threading.get_ident())))
    worker.start()
    worker.join(1)
    assert not worker.is_alive()
    assert delivered == []
    for _ in range(20):
        qapp.processEvents()
        if delivered:
            break
    assert delivered == [owner]
