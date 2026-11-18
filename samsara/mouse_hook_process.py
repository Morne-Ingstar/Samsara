"""App-side transport and supervision for the isolated mouse hook (1255).

Native input and the wire protocol stay in the stdlib-only worker. The
in-process fallback owns shared edge routing and recovery state.
"""
import collections
import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import threading

from samsara import mouse_hook as _base
from samsara import mouse_hook_worker as _worker
from samsara.runtime import thread_registry

logger = _base.logger


def _worker_command():
    if getattr(sys, 'frozen', False):
        return [os.path.join(os.path.dirname(sys.executable), 'samsara-mouse-hook.exe')]
    return [sys.executable, '-m', 'samsara.mouse_hook_worker']


class _WorkerJob:
    """The parent alone owns this non-inheritable KILL_ON_JOB_CLOSE handle."""
    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', ctypes.wintypes.DWORD), ('min_ws', ctypes.c_size_t),
                        ('max_ws', ctypes.c_size_t), ('active', ctypes.wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', ctypes.wintypes.DWORD),
                        ('scheduling', ctypes.wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

        self.handle = _base._native(_base._kernel32, 'CreateJobObjectW',
                              (ctypes.c_void_p, ctypes.c_wchar_p), ctypes.c_void_p)(None, None)
        if not self.handle:
            raise OSError('mouse helper job creation failed')
        try:
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _base._native(_base._kernel32, 'SetInformationJobObject',
                           (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.wintypes.DWORD),
                           ctypes.wintypes.BOOL)(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError('mouse helper job limits failed')
            if not _base._native(_base._kernel32, 'AssignProcessToJobObject',
                           (ctypes.c_void_p, ctypes.c_void_p), ctypes.wintypes.BOOL)(
                               self.handle, int(process._handle)):
                raise OSError('mouse helper job assignment failed')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            _base._native(_base._kernel32, 'CloseHandle', (ctypes.c_void_p,), ctypes.wintypes.BOOL)(self.handle)
            self.handle = None


def _launch_worker():
    # The worker's stdout write end MUST support OVERLAPPED I/O. Ordinary
    # subprocess.PIPE is synchronous and would stall the native hook pump.
    import msvcrt
    from asyncio.windows_utils import pipe
    import _winapi

    read_handle, write_handle = pipe(overlapped=(False, True), bufsize=65536)
    reader = writer = None
    try:
        reader = os.fdopen(msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY), 'rb', buffering=0)
        read_handle = None
        writer = os.fdopen(msvcrt.open_osfhandle(write_handle, os.O_WRONLY | os.O_BINARY), 'wb', buffering=0)
        write_handle = None
        process = subprocess.Popen(
            _worker_command(), stdin=subprocess.PIPE, stdout=writer, stderr=subprocess.DEVNULL,
            bufsize=0, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
        process.stdout = reader
        reader = None
        return process
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        for handle in (read_handle, write_handle):
            if handle is not None:
                _winapi.CloseHandle(handle)


def _read_worker(process):
    """Read only available bytes; stop/crash detection never waits on a pipe."""
    import msvcrt
    available = ctypes.wintypes.DWORD()
    handle = msvcrt.get_osfhandle(process.stdout.fileno())
    if not _base._native(_base._kernel32, 'PeekNamedPipe',
                   (ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p), ctypes.wintypes.BOOL)(
                        handle, None, 0, None, ctypes.byref(available), None):
        raise OSError('mouse helper output closed')
    return process.stdout.read(min(available.value, 65536)) if available.value else b''


class ProcessMouseHook(_base.MouseHook):
    """Same edge routing as MouseHook; all native input runs in another process.

    Set config mouse_hook.in_process to true (nested mouse_hook object) to
    retain the old implementation for one release. No implicit fallback: a
    broken helper must be visible rather than reintroduce the app GIL hazard.
    """
    RESTART_DELAYS = (1.0, 2.0, 5.0)
    HEARTBEAT_TIMEOUT_S = 2.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._process = self._job = None
        self._lifecycle_lock = threading.RLock()
        self._wire = b''
        self._worker_now = 0.0
        self._raw_total = 0
        self._restart_count = 0
        self._worker_notice_logged = False
        self._last_received = 0.0
        self._refresh_sent_at = 0.0
        self._pending_ll = collections.deque(maxlen=128)

    @property
    def installed(self):
        process = self._process
        return bool(self._hook_id) and process is not None and process.poll() is None

    def _send_control(self, command, value=0):
        with self._lifecycle_lock:
            if self._process is None or self._stopping.is_set():
                raise OSError('mouse helper stopped')
            packet = _worker.CONTROL.pack(command, value)
            if self._process.stdin.write(packet) != len(packet):
                raise OSError('short mouse helper control write')

    def set_suppress_buttons(self, buttons):
        buttons = _base._coerce_suppress_buttons(buttons) & {'mouse4', 'mouse5'}
        self.requested_suppress_buttons = self.suppress_buttons = buttons
        self._send_control(_worker.SET_MASK, self._mask())

    def _mask(self):
        return (1 if 'mouse4' in self.requested_suppress_buttons else 0) | (
            2 if 'mouse5' in self.requested_suppress_buttons else 0)

    def _open_worker(self):
        with self._lifecycle_lock:
            if self._stopping.is_set():
                return
            self._wire = b''
            self._raw_total = 0
            self._raw_activity.clear()
            self._raw_edges.clear()
            self._ll_button_at.clear()
            self._pending_ll.clear()
            self._retired_hooks.clear()
            self._physical_callback_at = None
            self.callback_count = 0
            self._recovery_count = 0
            self._refresh_pending = False
            self._retry_due = None
            self._process = _launch_worker()
            # The helper cannot install anything until this succeeds.
            self._job = _WorkerJob(self._process)
            self._send_control(_worker.SET_MASK, self._mask())
            self._last_received = _base._perf()

    def _close_worker(self):
        with self._lifecycle_lock:
            process, self._process = self._process, None
            job, self._job = self._job, None
            self._hook_id = None
            if process is not None:
                try:
                    process.stdin.close()  # EOF: helper unhooks on its own pump
                    process.wait(timeout=0.1)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                finally:
                    if job is not None:
                        job.close()  # also handles a wedged helper; ours only
                    elif process.poll() is None:
                        process.kill()  # startup failed before job assignment
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        logger.error('[MOUSE HOOK] helper did not exit after job close')
                    process.stdout.close()
            elif job is not None:
                job.close()

    def _consume_frames(self, data):
        self._wire += data
        while len(self._wire) >= _worker.FRAME.size and not self._stopping.is_set():
            button, pressed, source, injected, value, micros = _worker.FRAME.unpack(
                self._wire[:_worker.FRAME.size])
            self._wire = self._wire[_worker.FRAME.size:]
            stamp = micros / 1_000_000.0  # same system QPC clock as perf_counter
            if source in (_worker.LOW_LEVEL, _worker.RAW_INPUT):
                if button not in (4, 5) or pressed > 1 or injected > 1:
                    raise ValueError('invalid mouse helper edge')
                name, down = 'mouse' + str(button), bool(pressed)
                if source == _worker.LOW_LEVEL:
                    matched = False
                    if not injected:
                        self.callback_count += 1
                        self._ll_button_at[name, down] = stamp
                        self._physical_callback_at = stamp
                        self._hold_healthy_at = stamp
                        matched = self._match_edge(self._raw_edges, name, down, stamp)
                        if not matched:
                            self._pending_ll.append((name, down, stamp))
                    self.last_callback_at = stamp
                    if not matched:
                        self._deliver((name, down, stamp, bool(injected)))
                    else:
                        self._recover_from_callbacks()
                else:
                    if not self._match_edge(self._pending_ll, name, down, stamp):
                        self._raw_edges.append((name, down, stamp))
                        self._deliver((name, down, stamp, False), raw=True)
            elif source == _worker.LL_STATUS:
                self.callback_count = value
                self._physical_callback_at = stamp or None
                if stamp:
                    self._hold_healthy_at = stamp
                    self.last_callback_at = stamp
                self._recover_from_callbacks()
            elif source == _worker.RAW_STATUS:
                delta = (value - self._raw_total) & 0xFFFFFFFF
                self._raw_total = value
                # Movement is counters only across the pipe; preserve 1254's
                # 50-in-1-second threshold without streaming every mouse move.
                self._raw_activity.extend([stamp] * min(delta, _base.RAW_MOTION_LIMIT))
            elif source == _worker.READY:
                if value != _worker.VERSION:
                    raise ValueError('mouse helper protocol version mismatch')
                self._hook_id = True
                self.install_error = ''
                self._ready.set()
                if self.recovering:
                    self._recovered()
            elif source == _worker.REFRESHED:
                self._refresh_pending = False
                self._retired_hooks = [] if injected else [True]  # worker owns the native handles
                logger.info('[MOUSE HOOK] helper refresh installed=%s old_removed=%s error=%s',
                            bool(pressed), bool(injected), value)
                if not pressed or not injected:
                    self._schedule_retry('helper hook refresh/cleanup failed', refresh=True)
            elif source == _worker.HEARTBEAT:
                self._worker_now = stamp
                self.callback_errors = value
                # A complete pump watermark precedes loss checks. App stalls
                # must not make an old raw frame outrun its queued LL partner.
                self.watchdog_tick()
            else:
                raise ValueError('unknown mouse helper frame')

    @staticmethod
    def _match_edge(pending, name, down, stamp):
        # Correlate each edge before checking loss. A burst can contain several
        # presses in one telemetry interval; the latest per-button timestamp
        # alone cannot prove that an older raw edge had its own LL partner.
        for edge in pending:
            if edge[:2] == (name, down) and abs(edge[2] - stamp) <= _base.RAW_EDGE_WAIT_S:
                pending.remove(edge)
                return True
        return False

    def _request_refresh(self, reason, *, retry=False):
        if self._refresh_pending or self._stopping.is_set() or self._retry_due is not None:
            return
        now = _base._perf()
        while self._reinstalls and now - self._reinstalls[0] > _base.REINSTALL_WINDOW_S:
            self._reinstalls.popleft()
        if not retry and (len(self._reinstalls) >= _base.REINSTALL_LIMIT or self._backoff_step):
            self._schedule_retry(reason, refresh=True)
            return
        self._reinstalls.append(now)
        self._refresh_pending = True
        self._refresh_sent_at = now
        self._send_control(_worker.REFRESH)

    def watchdog_tick(self):
        if self._stopping.is_set():
            return
        now = _base._perf()
        if self._healthy_since is not None and now - self._healthy_since >= _base.HEALTHY_RESET_S:
            self._backoff_step = 0
            self._healthy_since = None
        if self._refresh_pending:
            if now - self._refresh_sent_at > self.HEARTBEAT_TIMEOUT_S:
                raise OSError('mouse helper refresh timed out')
            return
        if self._retry_due is not None:
            if now >= self._retry_due:
                self._retry_due = None
                self._request_refresh(self._recovery_reason, retry=True)
            return
        reason = self._hook_lost(now=self._worker_now)
        if reason:
            self._begin_recovery(reason)
            self._request_refresh(reason)

    def _worker_failed(self, error):
        self._hook_id = None
        # A dead helper cannot deliver the matching up: finalize any active hold.
        for name, down in tuple(self._button_down.items()):
            if down:
                self._deliver((name, False, _base._perf(), False))
        self.install_error = ''
        if not self._worker_notice_logged:
            self._worker_notice_logged = True
            # PLACEHOLDER COPY (1255)
            logger.warning('[MOUSE HOOK] mouse helper stopped; restarting automatically: %s', error)
        if self._restart_count >= len(self.RESTART_DELAYS):
            self.gave_up = True
            self.retries_automatically = False
            # PLACEHOLDER COPY (1255)
            self.install_error = 'mouse helper restart limit reached; re-enable mouse hotkey to retry'
            self._begin_recovery(self.install_error)
            self._ready.set()
            return False
        self._begin_recovery('mouse helper stopped; restarting automatically')
        delay = self.RESTART_DELAYS[self._restart_count]
        self._restart_count += 1
        return not self._stopping.wait(delay)

    def _dispatch_process(self):
        while not self._stopping.is_set():
            error = None
            try:
                self._open_worker()
                while not self._stopping.is_set():
                    process = self._process
                    data = _read_worker(process)
                    if data:
                        self._last_received = _base._perf()
                        self._consume_frames(data)
                    elif process.poll() is not None:
                        raise OSError('mouse helper exited')
                    elif _base._perf() - self._last_received > self.HEARTBEAT_TIMEOUT_S:
                        raise OSError('mouse helper heartbeat timed out')
                    else:
                        self._stopping.wait(0.005)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                error = exc
            finally:
                self._close_worker()
            if self._stopping.is_set() or not self._worker_failed(error):
                return

    def start(self):
        if sys.platform != 'win32':
            return False
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return self.installed
        self._stopping.clear()
        self._ready.clear()
        self._restart_count = 0
        self.gave_up = False
        self.retries_automatically = True
        self._worker_notice_logged = False
        self._button_down.clear()
        _base._live_hooks.add(self)
        self._dispatcher = thread_registry.spawn('mouse-hook-dispatch', self._dispatch_process, daemon=True)
        self._ready.wait(timeout=2.0)
        return self.installed

    def stop(self):
        self._stopping.set()
        self.suppress_buttons = frozenset()
        self._retry_due = None
        _base._live_hooks.discard(self)
        self._close_worker()
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=2.0)
