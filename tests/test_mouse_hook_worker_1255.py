"""1255: deterministic process/Win32 fakes; no hooks, input or child processes."""
import ast
import ctypes
import inspect
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import mouse_hook as mh
from samsara import mouse_hook_process as mp
from samsara import mouse_hook_worker as w


@pytest.fixture(autouse=True)
def no_native_process(monkeypatch):
    monkeypatch.setattr(mp, '_launch_worker', Mock(side_effect=AssertionError('real helper forbidden')))
    monkeypatch.setattr(mh, '_foreground_not_elevated', lambda: True)


def frame(source, button=0, down=False, injected=False, value=0, us=1_000_000_000):
    return w.FRAME.pack(button, down, source, injected, value, us)


def parent(monkeypatch, now=1000.0):
    state = SimpleNamespace(now=now, events=[], controls=[])
    monkeypatch.setattr(mh, '_perf', lambda: state.now)
    state.hook = mh.ProcessMouseHook(lambda *edge: state.events.append(edge),
                                     suppress_buttons={'mouse5'}, poll_buttons={'mouse5'})
    state.hook._process = SimpleNamespace(poll=lambda: None)
    state.hook._hook_id = True
    state.hook._send_control = lambda *cmd: state.controls.append(cmd)
    return state


def native_pump(monkeypatch, output=None):
    state = SimpleNamespace(us=1_000_000_000, calls=[], next_hook=0x123456789, unhook_ok=True)

    def bind(_lib, name, args, result):
        def call(*values):
            state.calls.append((name, values))
            if name in ('QueryPerformanceCounter', 'QueryPerformanceFrequency'):
                ctypes.cast(values[0], ctypes.POINTER(ctypes.c_int64)).contents.value = (
                    state.us if name == 'QueryPerformanceCounter' else 1_000_000)
                return 1
            if name == 'SetWindowsHookExW':
                return state.next_hook
            if name == 'UnhookWindowsHookEx':
                ctypes.set_last_error(0 if state.unhook_ok else 5)
                return state.unhook_ok
            if name in ('GetModuleHandleW', 'CreateWindowExW'):
                return 0x223456789
            if name in ('DefWindowProcW', 'CallNextHookEx'):
                return 0
            return 1
        return call

    monkeypatch.setattr(w, 'native', bind)
    state.frames = []
    if output is None:
        output = SimpleNamespace(put=lambda *values: state.frames.append(w.FRAME.pack(*values)), flush=lambda: None)
    state.pump = w.Pump(object(), object(), output)
    return state


def ll(pump, down, button=5, injected=False):
    info = w.MSLLHOOKSTRUCT()
    info.mouseData = (button - 3) << 16
    info.flags = int(injected)
    return pump.on_hook(0, 0x020B if down else 0x020C, ctypes.addressof(info))


def raw(pump, down, button=5, device=1):
    packet = w.RAWINPUT()
    packet.header.hDevice = device
    packet.mouse.flags = (0x100 if down else 0x200) if button == 5 else (0x40 if down else 0x80)
    pump.on_raw(packet)


def test_protocol_and_worker_imports_are_leaf_only():
    assert w.FRAME.size == 16
    assert w.CONTROL.size == 2
    assert w.FRAME.unpack(frame(w.LOW_LEVEL, 5, True, True, 0xFFFFFFFF, 2**63)) == (
        5, 1, w.LOW_LEVEL, 1, 0xFFFFFFFF, 2**63)
    tree = ast.parse(inspect.getsource(w))
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert set(imports) == {'sys', 'os', 'ctypes', 'struct', 'gc'}
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))


@pytest.mark.parametrize('raw_first', [False, True])
@pytest.mark.parametrize('button', [4, 5])
def test_fragmented_frames_deduplicate_both_sources(monkeypatch, raw_first, button):
    s = parent(monkeypatch)
    data = frame(w.READY, value=w.VERSION)
    for down, stamp in ((True, 1_000_010_000), (False, 1_000_110_000)):
        sources = (w.RAW_INPUT, w.LOW_LEVEL) if raw_first else (w.LOW_LEVEL, w.RAW_INPUT)
        data += b''.join(frame(source, button, down, us=stamp) for source in sources)
    for byte in data:
        s.hook._consume_frames(bytes([byte]))
    assert s.events == [('mouse' + str(button), True), ('mouse' + str(button), False)]
    assert not s.hook._wire


def test_injected_release_is_delivered(monkeypatch):
    s = parent(monkeypatch)
    s.hook._consume_frames(frame(w.LOW_LEVEL, 5, True) + frame(w.LOW_LEVEL, 5, False, True))
    assert s.events == [('mouse5', True), ('mouse5', False)]


@pytest.mark.parametrize('first,second', [(w.LOW_LEVEL, w.RAW_INPUT), (w.RAW_INPUT, w.LOW_LEVEL)])
def test_batched_source_pair_does_not_replay_completed_click(monkeypatch, first, second):
    s = parent(monkeypatch)
    data = b''.join(frame(source, 5, down, us=stamp)
                    for source in (first, second)
                    for down, stamp in ((True, 1_000_001_000), (False, 1_000_011_000)))
    s.hook._consume_frames(data)
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_controls_fragment_mask_updates_and_eof(monkeypatch):
    s = native_pump(monkeypatch)
    p = s.pump
    p.controls.feed(w.CONTROL.pack(w.SET_MASK, 2)[:1])
    assert not p.controls.started
    p.controls.feed(w.CONTROL.pack(w.SET_MASK, 2)[1:])
    assert ll(p, True, 5) == 1 and ll(p, True, 4) == 0
    p.controls.feed(w.CONTROL.pack(w.SET_MASK, 0xFD))
    assert p.controls.mask == 1
    assert ll(p, False, 5) == 0 and ll(p, False, 4) == 1
    p.controls.feed(None)
    assert p.controls.stopping


@pytest.mark.parametrize('started', [False, True])
def test_eof_unhooks_on_pump_without_waiting_for_output(monkeypatch, started):
    s = native_pump(monkeypatch)
    inputs = iter([w.CONTROL.pack(w.SET_MASK, 2), None] if started else [None])
    waits = []
    s.pump.run(lambda: next(inputs), lambda: True, waits.append)
    names = [name for name, _ in s.calls]
    assert ('SetWindowsHookExW' in names) is started
    assert ('UnhookWindowsHookEx' in names) is started
    if started:
        assert waits == [w.POLL_MS]
        assert names.index('UnhookWindowsHookEx') < names.index('DestroyWindow')
    assert not s.pump.hook and not s.pump.window


@pytest.mark.parametrize('install_ok,cleanup_ok', [(True, True), (False, True), (True, False)])
def test_refresh_installs_before_unhook_and_retains_suppression(monkeypatch, install_ok, cleanup_ok):
    s = native_pump(monkeypatch)
    p = s.pump
    p.controls.feed(w.CONTROL.pack(w.SET_MASK, 2))
    p.hook = 0x123456788
    s.next_hook = 0x123456789 if install_ok else None
    s.unhook_ok = cleanup_ok
    p.refresh()
    names = [name for name, _ in s.calls]
    assert names.index('SetWindowsHookExW') < names.index('UnhookWindowsHookEx') if install_ok else (
        'UnhookWindowsHookEx' not in names)
    assert ll(p, True) == 1
    assert p.hook == (0x123456789 if install_ok else 0x123456788)
    assert bool(p.retired) is (install_ok and not cleanup_ok)
    p.close()
    assert any(name == 'UnhookWindowsHookEx' and values == (0x123456788,) for name, values in s.calls)


def test_raw_null_device_and_moves_do_not_emit_edges(monkeypatch):
    s = native_pump(monkeypatch)
    raw(s.pump, True, device=None)
    packet = w.RAWINPUT()
    packet.header.hDevice, packet.mouse.x = 1, 1
    for _ in range(100):
        s.pump.on_raw(packet)
    info = w.MSLLHOOKSTRUCT()
    for _ in range(100):
        s.pump.on_hook(0, 0x0200, ctypes.addressof(info))
    assert not s.frames
    assert s.pump.raw_count == 100 and s.pump.ll_count == 100


def output_rig(monkeypatch):
    state = SimpleNamespace(blocked=True, pending=None, wire=b'', cancelled=False)

    def bind(_library, name, args, result):
        def call(*values):
            if name == 'CreateEventW':
                return 0x123456789
            if name == 'WriteFile':
                state.pending = ctypes.string_at(values[1], values[2])
                ctypes.set_last_error(997)
                return 0
            if name == 'GetOverlappedResult':
                if state.blocked:
                    ctypes.set_last_error(996)
                    return 0
                ctypes.cast(values[2], ctypes.POINTER(w.DWORD)).contents.value = len(state.pending)
                state.wire += state.pending
                state.pending = None
                return 1
            if name == 'CancelIoEx':
                state.cancelled = True
                state.blocked = False
            return 1
        return call
    monkeypatch.setattr(w, 'native', bind)
    state.output = w.Output(object(), 0x223456789)
    return state


def test_five_second_main_stall_keeps_every_edge_and_pump_alive(monkeypatch):
    out = output_rig(monkeypatch)
    pump = native_pump(monkeypatch, out.output)
    app = parent(monkeypatch, now=1005)
    ticks = 0

    def read_control():
        if ticks == 0:
            return w.CONTROL.pack(w.SET_MASK, 2)
        return None if ticks == 2000 else b''

    def messages():
        nonlocal ticks
        pressed = ticks % 2 == 0
        assert ll(pump.pump, pressed) == 1
        raw(pump.pump, pressed)
        ticks += 1
        pump.us += 2500  # simulate 5 s with app completely unable to read
        return True

    waits = []
    pump.pump.run(read_control, messages, waits.append)
    assert ticks == 2000 and len(waits) == 2000
    assert pump.us == 1_005_000_000 and out.blocked
    assert not app.events  # no app GIL/dispatcher time during the entire stall
    out.blocked = False
    while out.output.buffer is not None or out.output.head < len(out.output.frames):
        out.output.flush()
    for offset in range(0, len(out.wire), 113):
        app.hook._consume_frames(out.wire[offset:offset + 113])
    assert app.events == [('mouse5', i % 2 == 0) for i in range(2000)]
    assert not app.controls  # queued LL partners prevent false loss after stall
    out.output.close()


def test_pending_output_is_cancelled_before_event_close(monkeypatch):
    out = output_rig(monkeypatch)
    out.output.put(5, True, w.LOW_LEVEL, False, 0, 123)
    out.output.flush()
    assert out.output.buffer is not None
    out.output.close()
    assert out.cancelled and out.output.buffer is None


def test_raw_loss_refresh_budget_and_mask_updates(monkeypatch):
    s = parent(monkeypatch)
    h = s.hook
    h._consume_frames(frame(w.RAW_INPUT, 5, True) + frame(w.HEARTBEAT, us=1_000_021_000))
    assert h._refresh_pending and s.controls == [(w.REFRESH,)]
    assert s.events == [('mouse5', True)]
    for i in range(2):
        h._consume_frames(frame(w.REFRESHED, down=True, injected=True))
        h._request_refresh('physical loss')
    h._consume_frames(frame(w.REFRESHED, down=True, injected=True))
    h._request_refresh('physical loss')
    assert len(s.controls) == 3 and h._retry_due == 1030
    assert h.suppress_buttons == {'mouse5'}
    h.set_suppress_buttons({'mouse4', 'mouse5'})
    assert s.controls[-1] == (w.SET_MASK, 3)


def test_raw_motion_requires_threshold_and_safe_foreground(monkeypatch):
    s = parent(monkeypatch)
    h = s.hook
    h._consume_frames(frame(w.RAW_STATUS, value=49) + frame(w.HEARTBEAT))
    assert not s.controls
    monkeypatch.setattr(mh, '_foreground_not_elevated', lambda: False)
    h._consume_frames(frame(w.RAW_STATUS, value=50) + frame(w.HEARTBEAT))
    assert not s.controls
    monkeypatch.setattr(mh, '_foreground_not_elevated', lambda: True)
    h._consume_frames(frame(w.RAW_STATUS, value=100) + frame(w.HEARTBEAT))
    assert s.controls == [(w.REFRESH,)]


def test_motion_batch_uses_earliest_raw_time_for_liveness(monkeypatch):
    s = native_pump(monkeypatch)
    app = parent(monkeypatch)
    turns = 0

    def messages():
        nonlocal turns
        if turns == 1:
            for _ in range(60):
                info = w.MSLLHOOKSTRUCT()
                s.pump.on_hook(0, 0x0200, ctypes.addressof(info))
                s.us += 10  # LL precedes its WM_INPUT delivery
                packet = w.RAWINPUT()
                packet.header.hDevice, packet.mouse.x = 1, 1
                s.pump.on_raw(packet)
                s.us += 10
        turns += 1
        s.us += w.STATUS_US
        return True

    s.pump.run(lambda: (w.CONTROL.pack(w.SET_MASK, 2) if turns == 0 else
                        None if turns == 3 else b''), messages, lambda _: None)
    app.hook._consume_frames(b''.join(s.frames))
    assert not app.controls


def test_failed_retired_cleanup_keeps_retry_after_returning_callback(monkeypatch):
    s = parent(monkeypatch)
    h = s.hook
    h._begin_recovery('loss')
    h._consume_frames(frame(w.REFRESHED, down=True, injected=False, value=5))
    due = h._retry_due
    h._consume_frames(frame(w.LOW_LEVEL, 5, True))
    assert h._retry_due == due and h._retired_hooks


class FakeProcess:
    def __init__(self, journal):
        self.journal = journal
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self._handle = 0x123456789
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.journal.append('wait')
        self.returncode = 0
        return 0

    def kill(self):
        self.journal.append('kill')
        self.returncode = 1


def test_crash_restarts_bounded_says_once_and_releases_hold(monkeypatch, caplog):
    journal, processes, delays, interruptions = [], [], [], []
    h = mh.ProcessMouseHook(lambda *event: journal.append(event), suppress_buttons={'mouse5'},
                            on_hook_interrupted=interruptions.append)

    def launch():
        process = FakeProcess(journal)
        process.reads = 0
        processes.append(process)
        journal.append('launch')
        return process

    def job(process):
        assert process.stdin.getvalue() == b''  # no installation before assignment
        journal.append('job')
        return SimpleNamespace(close=lambda: journal.append('job close'))

    def read(process):
        assert process.stdin.getvalue() == w.CONTROL.pack(w.SET_MASK, 2)
        process.reads += 1
        if process.reads == 1:
            return frame(w.READY, value=w.VERSION) + frame(w.LOW_LEVEL, 5, True)
        raise OSError('simulated crash')

    monkeypatch.setattr(mp, '_launch_worker', launch)
    monkeypatch.setattr(mp, '_WorkerJob', job)
    monkeypatch.setattr(mp, '_read_worker', read)
    monkeypatch.setattr(h._stopping, 'wait', lambda seconds: delays.append(seconds) or False)
    h._dispatch_process()
    assert len(processes) == 4 and delays == list(h.RESTART_DELAYS)
    assert h.gave_up and not h.retries_automatically and not h.installed
    assert journal.count(('mouse5', True)) == journal.count(('mouse5', False)) == 4
    assert journal.count('job close') == 4
    assert caplog.text.count('mouse helper stopped; restarting automatically:') == 1
    assert 'restart limit reached' in h.install_error
    assert interruptions[-1] == h.install_error


def test_job_failure_never_sends_start_and_cleans_own_child(monkeypatch):
    journal = []
    process = FakeProcess(journal)
    h = mh.ProcessMouseHook(lambda *_: None)
    monkeypatch.setattr(mp, '_launch_worker', lambda: process)
    monkeypatch.setattr(mp, '_WorkerJob', Mock(side_effect=OSError('assignment denied')))
    with pytest.raises(OSError, match='assignment denied'):
        h._open_worker()
    assert process.stdin.getvalue() == b''
    h._close_worker()
    assert process.stdin.closed and process.stdout.closed


def test_job_native_width_and_kill_on_close(monkeypatch):
    calls = []
    def native(_library, name, args, result):
        def call(*values):
            calls.append((name, values))
            if name == 'CreateJobObjectW':
                assert result is ctypes.c_void_p and values == (None, None)
                return 0x223456789
            if name == 'SetInformationJobObject':
                assert values[1] == 9 and values[3] == 144
                assert ctypes.c_uint32.from_address(ctypes.cast(values[2], ctypes.c_void_p).value + 16).value == 0x2000
            return 1
        return call
    monkeypatch.setattr(mh, '_native', native)
    job = mp._WorkerJob(SimpleNamespace(_handle=0x123456789))
    job.close()
    assert ('AssignProcessToJobObject', (0x223456789, 0x123456789)) in calls
    assert calls[-1] == ('CloseHandle', (0x223456789,))
    assert job.handle is None


def test_stop_closes_stdin_job_and_does_not_restart(monkeypatch):
    journal = []
    h = mh.ProcessMouseHook(lambda *_: None)
    h._process = FakeProcess(journal)
    process = h._process
    h._job = SimpleNamespace(close=lambda: journal.append('job close'))
    h.release('test release')
    assert process.stdin.closed and process.stdout.closed
    assert h._stopping.is_set() and not h.suppress_buttons and not h.installed
    assert journal == ['wait', 'job close', 'wait']


@pytest.mark.parametrize('source,value', [(w.READY, 999), (99, 0)])
def test_bad_protocol_is_rejected(monkeypatch, source, value):
    s = parent(monkeypatch)
    with pytest.raises(ValueError):
        s.hook._consume_frames(frame(source, value=value))


@pytest.mark.parametrize('fallback', [False, True])
def test_hotkeys_choose_process_default_and_explicit_fallback(monkeypatch, fallback):
    from samsara.hotkeys import HotkeyCluster
    created = []
    def factory(kind):
        def construct(**kwargs):
            created.append((kind, kwargs))
            return SimpleNamespace(start=lambda: None, installed=True, recovering=False)
        return construct
    monkeypatch.setattr(mh, 'MouseHook', factory('in process'))
    monkeypatch.setattr(mh, 'ProcessMouseHook', factory('helper'))
    app = SimpleNamespace(config={'hotkey': 'mouse5', 'mode': 'hold', 'mouse_hook': {'in_process': fallback}},
                          _mouse_hook_bindings=lambda: ({'mouse5'}, {'mouse5'}), _on_mouse_button=lambda *_: None)
    HotkeyCluster._install_mouse_listener(app)
    assert created[0][0] == ('in process' if fallback else 'helper')
    assert created[0][1]['poll_buttons'] == {'mouse5'}


def test_frozen_entry_resolves_to_separate_console_helper(monkeypatch):
    # Execute the actual helper-only spec declarations with build primitives
    # replaced; never execute the expensive application analysis/bootstrap.
    root = Path(__file__).resolve().parents[1]
    spec = ast.parse((root / 'scripts/samsara.spec').read_text(encoding='utf-8'))
    names = {'mouse_analysis', 'mouse_pyz', 'mouse_exe', 'coll'}
    nodes = [n for n in spec.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    analysis_calls, exe_calls, collect_calls = [], [], []
    def analysis(scripts, **kwargs):
        analysis_calls.append((scripts, kwargs))
        assert Path(scripts[0]).is_file()
        return SimpleNamespace(pure=[], scripts=scripts, binaries=[], datas=[])
    def executable(*args, **kwargs):
        exe_calls.append((args, kwargs))
        return kwargs['name']
    namespace = dict(app_dir=root, Analysis=analysis, PYZ=lambda *_: 'pyz', EXE=executable,
                     COLLECT=lambda *args, **kw: collect_calls.append(args), exe='app', relay_exe='relay',
                     a=SimpleNamespace(binaries=[], zipfiles=[], datas=[]),
                     relay_analysis=SimpleNamespace(binaries=[], datas=[]))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<helper spec>', 'exec'), namespace)
    assert analysis_calls[0][0] == [str(root / 'samsara/mouse_hook_worker.py')]
    assert analysis_calls[0][1]['runtime_hooks'] == []
    assert exe_calls[0][1]['console'] is True
    assert 'samsara-mouse-hook' in collect_calls[0]
    monkeypatch.setattr(mh.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(mh.sys, 'executable', str(root / 'Samsara.exe'))
    assert mp._worker_command() == [str(root / 'samsara-mouse-hook.exe')]
    monkeypatch.delattr(mh.sys, 'frozen')
    assert mp._worker_command()[-2:] == ['-m', 'samsara.mouse_hook_worker']


def test_worker_native_layouts_and_pointer_sized_callback():
    assert ctypes.sizeof(w.RAWMOUSE) == 24
    assert w.RAWMOUSE.u.offset == 4
    assert ctypes.sizeof(w.RAWINPUTHEADER) == 24
    assert w.RAWINPUT.mouse.offset == 24
    assert ctypes.sizeof(w.OVERLAPPED) == 32
    assert w.HookProc._restype_ is ctypes.c_ssize_t


def test_main_high_priority_gc_disabled_and_eof_before_install(monkeypatch):
    calls = []
    monkeypatch.setattr(w.ctypes, 'WinDLL', lambda *a, **kw: object())
    monkeypatch.setattr(w.gc, 'disable', lambda: calls.append('gc disabled'))
    def native(_library, name, args, result):
        def call(*values):
            calls.append((name, values))
            if name == 'GetCurrentProcess':
                return 0x123456789
            if name == 'GetStdHandle':
                return 55 if values[0] == -10 else 56
            if name == 'PeekNamedPipe':
                ctypes.set_last_error(109)
                return 0
            return 1
        return call
    monkeypatch.setattr(w, 'native', native)
    monkeypatch.setattr(w, 'Output', lambda *args: SimpleNamespace(close=lambda: calls.append('output closed')))
    def pump(_user, _kernel, _output):
        def run(read_control, _messages, _wait):
            assert read_control() is None
            calls.append('EOF before install')
        return SimpleNamespace(run=run)
    monkeypatch.setattr(w, 'Pump', pump)
    assert w.main() == 0
    assert ('SetPriorityClass', (0x123456789, 0x80)) in calls
    assert calls[0] == 'gc disabled' and calls[-1] == 'output closed'
