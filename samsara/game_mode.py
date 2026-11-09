"""Session-local game policy. No configuration, desktop or model work at import."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import logging
import threading
import time

from samsara.config_defaults import cfg_get

log = logging.getLogger(__name__)
# PLACEHOLDER COPY (1058)
NOTICE = "Game mode: dictation on CPU, a little slower"
EXIT_GRACE_SECONDS = 45.0
SWITCH_WINDOW_SECONDS = 60.0
MAX_SWITCHES_PER_WINDOW = 4
NON_GAME_PROCESSES = frozenset({
    'vlc', 'mpv', 'chrome', 'brave', 'msedge', 'firefox', 'opera',
    'spotify', 'steam', 'steamwebhelper',
})
NON_GAME_PREFIXES = ('stremio', 'mpc-hc', 'mpc-be', 'potplayer')


def excluded_process(name):
    """Match executable basenames, never window titles or install paths."""
    name = name.replace('\\', '/').rsplit('/', 1)[-1].casefold()
    if name.endswith('.exe'):
        name = name[:-4]
    return name in NON_GAME_PROCESSES or name.startswith(NON_GAME_PREFIXES)


def game_candidate(sample):
    return sample.fullscreen and sample.gpu_busy and not excluded_process(sample.process_name)


@dataclass(frozen=True)
class Sample:
    fullscreen: bool = False
    gpu_busy: bool = False
    pid: int = 0
    hwnd: int = 0
    # True: the detected game is foreground or still rendering in background.
    # None: window inspection failed; do not treat uncertainty as an exit.
    game_present: bool | None = False
    process_name: str = ''
    window_class: str = ''
    tracked_pid: int = 0
    tracked_gpu_busy: bool | None = None


def log_detection(event, source, observation, reason):
    # Class names are structural; never fetch/log window title text.
    log.info('[GAME-MODE] %s reason=%s process=%r window_class=%r '
             'fullscreen=%s gpu_busy=%s foreground_pid=%s tracked_pid=%s tracked_gpu_busy=%s',
             event, reason, source.process_name, source.window_class,
             observation.fullscreen, observation.gpu_busy, observation.pid,
             observation.tracked_pid or source.pid, observation.tracked_gpu_busy)


def partials_enabled(app, session=None):
    return not (getattr(session, '_cpu_final_only', False)
                or getattr(app, 'device_type', None) == 'cpu'
                or getattr(getattr(app, '_game_mode', None), 'active', False) is True)


def _busy(app):
    job = getattr(app, '_hold_decode_job', None)
    return (any(bool(getattr(app, key, False)) for key in (
        'recording', '_hotkey_recording', '_stop_in_flight',
        '_hold_park_finalizing', '_mic_score_active', 'loading_model', '_pending_transcriptions'))
        or getattr(app, '_streaming_session', None) is not None
        or (job is not None and not job.done.is_set()))


class GameMode:
    def __init__(self, app, factory, *, probe=None, notify=None, clock=None):
        self.app, self.factory = app, factory
        self.probe = probe or WindowsGameDetector()
        self.notify = notify or publish
        self.clock = clock or time.monotonic
        self.manual = None
        self.auto_requested = False
        self._sample = Sample()
        self._auto_active = False
        self._auto_source = Sample()
        self._absent_since = None
        self.active = False
        self.resident = None
        self.resident_name = None
        self._original = None
        self._primary_name = app.config.get('model_size', 'unknown model')
        self._active_name = None
        self._announced = False
        self._load_failed = None
        self._loading_name = None
        self._retired = []
        self._switches = deque()
        self._lock = threading.RLock()
        self.stop = threading.Event()

    @property
    def sample(self):
        return self._sample

    @sample.setter
    def sample(self, value):
        """Only fresh detector observations advance the exit debounce."""
        with self._lock:
            self._sample = value
            previous = self._auto_active
            if game_candidate(value):
                self._auto_source = value
                self._auto_active = True
                self._absent_since = None
            elif value.game_present is not False:
                self._absent_since = None
            elif self._auto_active:
                now = self.clock()
                if self._absent_since is None:
                    self._absent_since = now
                if now - self._absent_since >= EXIT_GRACE_SECONDS:
                    self._auto_active = False
            if previous != self._auto_active:
                log_detection('Auto on' if self._auto_active else 'Auto off',
                              self._auto_source, value,
                              'fullscreen-3d' if self._auto_active else 'inactive-grace-expired')

    @property
    def wanted(self):
        if not cfg_get(self.app.config, 'game_mode.enabled'):
            return False
        if self.manual is not None:
            return self.manual
        return self.automatic and self._auto_active

    @property
    def automatic(self):
        return self.manual is None and (self.auto_requested
            or cfg_get(self.app.config, 'game_mode.detect') == 'auto')

    def set_manual(self, value):
        with self._lock:
            self.manual = value
            self.auto_requested = value is None
            self._load_failed = None
        # The monitor performs loading; a spoken command must not block on it.
        self.reconcile()

    def prepare(self):
        """Single background load, admitted at idle; capture never waits for it."""
        with self._lock:
            name = cfg_get(self.app.config, 'game_mode.cpu_model')
            if (not self.wanted or _busy(self.app) or self._loading_name is not None
                    or (self.resident is not None and self.resident_name == name)
                    or self._load_failed == name):
                return False
            if not self.app.model_lock.acquire(blocking=False):
                return False
            self.app.model_lock.release()
            self._loading_name = name
        # No capture, policy or decoder lock spans the native constructor.
        started = time.perf_counter()
        try:
            model = self.factory(name, device='cpu', compute_type='int8',
                                 cpu_threads=4, num_workers=2)
        except Exception:
            with self._lock:
                self._load_failed = name
                self._loading_name = None
            log.warning('[GAME-MODE] CPU model unavailable', exc_info=True)
            self.notify(self.app, self.active, 'Game mode could not load its CPU model; dictation kept its current model')
            return False
        log.info('[GAME-MODE] CPU model loaded model=%s device=cpu compute_type=int8 load_ms=%.1f',
                 name, (time.perf_counter() - started) * 1000)
        with self._lock:
            self._loading_name = None
            if name == cfg_get(self.app.config, 'game_mode.cpu_model'):
                # Only the monitor may retire old native instances. A later
                # before_capture swap must not invoke a native destructor.
                if self.resident is not None:
                    self._retired.append((self.resident, self.clock()))
                self.resident, self.resident_name = model, name
        return True

    def _release_retired(self):
        """Monitor-only cleanup of explicitly replaced CPU model choices."""
        with self._lock:
            if _busy(self.app):
                return
            previous = self._retired
            self._retired = [(model, at) for model, at in previous
                             if model is self.app.model or self.clock() - at < SWITCH_WINDOW_SECONDS]
        # 'previous' and its native objects are released outside all locks.

    def reconcile(self, *, primary=False):
        """Never wait behind a capture or decoder; retry at the next idle."""
        lifecycle = getattr(self.app, '_hold_capture_lifecycle_lock', None)
        if lifecycle is not None and not lifecycle.acquire(blocking=False):
            return False
        changed = False
        notice = None
        try:
            if not self._lock.acquire(blocking=False):
                return False
            try:
                if _busy(self.app):
                    return False
                target = self.wanted and self.resident is not None and not primary
                if target == self.active and (not target or self.app.model is self.resident):
                    return False
                now = self.clock()
                while self._switches and now - self._switches[0] >= SWITCH_WINDOW_SECONDS:
                    self._switches.popleft()
                # Ava/command routing must always retain its primary model.
                # Policy changes (including manual flips) are rate limited.
                if not primary and len(self._switches) >= MAX_SWITCHES_PER_WINDOW:
                    return False
                if not self.app.model_lock.acquire(blocking=False):
                    return False
                try:
                    if _busy(self.app):
                        return False
                    if target:
                        if not self.active:
                            self._original = (self.app.model, self.app.device_type, self.app.compute_type)
                        self.app.model = self.resident
                        self.app.device_type, self.app.compute_type = 'cpu', 'int8'
                        self._active_name = self.resident_name
                    else:
                        self.app.model, self.app.device_type, self.app.compute_type = self._original
                        # Ava may have recovered the primary CUDA model while
                        # dictation was on CPU; retain that real recovery.
                        from samsara.gpu_fallback import ResilientSpeechModel
                        if isinstance(self.app.model, ResilientSpeechModel) and self.app.model._device == 'cpu':
                            self.app.device_type, self.app.compute_type = 'cpu', 'int8'
                        self._original = None
                    self.active = target
                    if not primary:
                        self._switches.append(now)
                finally:
                    self.app.model_lock.release()
                notice = NOTICE if target and not self._announced else None
                self._announced |= target
                changed = True
            finally:
                self._lock.release()
        finally:
            if lifecycle is not None:
                lifecycle.release()
        if changed:
            reason = ('primary-lane' if primary else 'manual' if self.manual is not None
                      else 'auto' if self.automatic else 'disabled')
            log_detection('On' if target else 'Off', self._auto_source, self.sample, reason)
            self.notify(self.app, target, notice)
        return changed

    def runtime_text(self):
        """Describe the selected instance, never an unapplied saved choice."""
        with self._lock:
            name = self._active_name if self.active else self._primary_name
            device = _model_device(self.app.model, self.app.device_type)
            # PLACEHOLDER COPY (1152)
            text = f'Right now: {name} on {device.upper()}'
            if self.active:
                text += ' (game mode)'
            if self._loading_name:
                text += f'. Preparing {self._loading_name}; dictation keeps this model.'
            elif self.wanted != self.active:
                text += '. Game mode change waiting until ready and idle.'
            return text

    @property
    def using_cpu(self):
        """The display describes the selected device, never a pending request."""
        return (self.active and self.app.model is not None
                and _model_device(self.app.model, self.app.device_type) == 'cpu')

    def original_model(self):
        with self._lock:
            return self._original[0] if self.active else self.app.model

    def run(self):
        while not self.stop.is_set() and getattr(self.app, '_running', True):
            try:
                self._release_retired()
                if (cfg_get(self.app.config, 'game_mode.enabled')
                        and self.automatic):
                    self.sample = self.probe()
                loaded = self.prepare()
                if loaded and self.automatic:
                    # A constructor may take seconds; never act on its stale
                    # pre-load foreground sample.
                    self.sample = self.probe()
                self.reconcile()
            except Exception:
                self.sample = Sample(game_present=None)
                log.warning('[GAME-MODE] Policy sample unavailable', exc_info=True)
            self.stop.wait(2.0)


def install(app, factory):
    """Called after the primary model is ready; one monitor per app."""
    existing = getattr(app, '_game_mode', None)
    if existing is not None:
        # A deliberate primary-model reload supersedes the old restore point.
        with existing._lock:
            existing.active, existing._original = False, None
            existing._primary_name = app.config.get('model_size', 'unknown model')
            existing.factory = factory
        existing.notify(app, False)
        return
    from samsara.runtime import thread_registry
    app._game_mode = GameMode(app, factory)
    thread_registry.on_shutdown(app._game_mode.stop.set)
    thread_registry.spawn('game-mode', app._game_mode.run, daemon=True)


def before_capture(app, capture_path='dictation_hold'):
    policy = getattr(app, '_game_mode', None)
    if policy is not None:
        policy.reconcile(primary=capture_path in ('ava_hold', 'command_key'))


def decode_model(app, path):
    """Ava/command recognition keeps its primary model, even during game mode."""
    policy = getattr(app, '_game_mode', None)
    mode = getattr(getattr(app, '_session_mode_manager', None), 'mode', None)
    primary = path in ('ava', 'command_fast') or (
        path in ('session', 'wake', 'wake_debug') and getattr(mode, 'value', None) in ('ava', 'command'))
    if policy is not None and primary:
        return policy.original_model()
    return app.model


def publish(app, active, notice=None):
    from samsara.ui import qt_runtime

    def show():
        # Posts from different callers can arrive out of order. Read what is
        # actually selected when Qt paints, not the old detection request.
        current = getattr(getattr(app, '_game_mode', None), 'using_cpu', active)
        indicator = getattr(app, 'listening_indicator', None)
        setter = getattr(indicator, 'set_game_mode', None)
        if setter is not None:
            setter(current)
        if notice and indicator is not None and active == current:
            indicator.show_outcome(notice, 'accent' if active else 'error', 8000)

    qt_runtime.post(show)


def _model_device(model, fallback):
    from samsara.gpu_fallback import ResilientSpeechModel
    return model._device if isinstance(model, ResilientSpeechModel) else fallback


def observe_decode(app, model, path):
    """Log native call/pull time without changing selection, params or segments.

    The model argument may be the incident observer; app.model is the selected
    speech instance. Keep that identity even if saved settings change later.
    """
    policy = getattr(app, '_game_mode', None)
    if policy is None or not policy.wanted:
        return model
    selected = app.model
    if policy._original is not None and selected is policy._original[0]:
        name = policy._primary_name
        _, device, compute = policy._original
    elif policy.active and selected is policy.app.model:
        name, device, compute = policy._active_name, app.device_type, app.compute_type
    else:
        name, device, compute = policy._primary_name, app.device_type, app.compute_type

    def receipt(elapsed, status):
        # A resilient CUDA instance can recover to CPU during this very call.
        actual_device = _model_device(selected, device)
        native = getattr(selected, 'model', None)
        actual_compute = getattr(native, 'compute_type', None)
        if not isinstance(actual_compute, str):
            actual_compute = 'int8' if actual_device == 'cpu' and device == 'cuda' else compute
        log.info('[GAME-MODE] Decode path=%s model=%s device=%s compute_type=%s '
                 'decode_ms=%.1f status=%s',
                 path, name, actual_device, actual_compute, elapsed * 1000, status)

    class ObservedModel:
        def __getattr__(self, name):
            return getattr(model, name)

        def transcribe(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                segments, info = model.transcribe(*args, **kwargs)
            except BaseException:
                receipt(time.perf_counter() - started, 'error')
                raise
            elapsed = time.perf_counter() - started

            def observed():
                nonlocal elapsed
                status = 'cancelled'
                try:
                    iterator = iter(segments)
                    while True:
                        started = time.perf_counter()
                        try:
                            segment = next(iterator)
                        except StopIteration:
                            status = 'ok'
                            return
                        finally:
                            elapsed += time.perf_counter() - started
                        yield segment
                except GeneratorExit:
                    raise
                except BaseException:
                    status = 'error'
                    raise
                finally:
                    receipt(elapsed, status)

            return observed(), info

    return ObservedModel()


def runtime_text(app):
    policy = getattr(app, '_game_mode', None)
    if policy is not None:
        return policy.runtime_text()
    # PLACEHOLDER COPY (1152): saved settings are not evidence of a loaded model.
    return 'Right now: speech model loading' if getattr(app, 'loading_model', False) else 'Right now: speech model not ready'


def set_manual(app, value):
    policy = getattr(app, '_game_mode', None)
    if policy is None:
        return False
    policy.set_manual(value)
    return True


def add_tray(tray, menu):
    policy = getattr(tray._app, '_game_mode', None)
    if policy is not None:
        tray._add(menu, 'Game mode', lambda checked: set_manual(tray._app, checked),
                  checked=policy.wanted)
        tray._add(menu, 'Game mode: automatic', lambda checked: set_manual(tray._app, None),
                  checked=policy.automatic)


class WindowsGameDetector:
    """Track a detected game's window separately from foreground focus."""
    def __init__(self, foreground=None, present=None, activity=None):
        self.foreground = foreground or windows_sample
        self.present = present or (lambda sample: game_window_present(sample, include_minimized=True))
        self.activity = activity or gpu_busy
        self.tracked = None

    def __call__(self):
        from dataclasses import replace
        sample = self.foreground()
        if game_candidate(sample):
            if self.tracked is None or (self.tracked.pid, self.tracked.hwnd) != (sample.pid, sample.hwnd):
                log_detection('Tracking', sample, sample, 'fullscreen-3d')
            self.tracked = sample
            return replace(sample, game_present=True, tracked_pid=sample.pid, tracked_gpu_busy=True)
        tracked = self.tracked
        if tracked is None:
            return replace(sample, game_present=False)
        present = self.present(tracked)
        busy = None
        if present is True and (sample.pid, sample.hwnd) != (tracked.pid, tracked.hwnd):
            busy = self.activity(tracked.pid)
            present = busy
        elif present is False:
            self.tracked = None
        return replace(sample, game_present=present, tracked_pid=tracked.pid, tracked_gpu_busy=busy)


def game_window_present(sample, *, include_minimized=False):
    """Verify window/PID identity; rendering may keep even a minimized game."""
    import ctypes as c
    from ctypes import wintypes as w
    if not sample.hwnd:
        return False
    user = c.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = (w.HWND,)
    user.IsIconic.argtypes = (w.HWND,)
    user.GetWindowThreadProcessId.argtypes = (w.HWND, c.POINTER(w.DWORD))
    if not user.IsWindow(sample.hwnd):
        return False
    pid = w.DWORD()
    if not user.GetWindowThreadProcessId(sample.hwnd, c.byref(pid)):
        return None
    return pid.value == sample.pid and (include_minimized or not user.IsIconic(sample.hwnd))


def windows_sample():
    """Foreground monitor coverage plus that PID's Windows 3D-engine load.

    PDH includes graphics workloads that NVIDIA's compute-app list omits.
    A 100 ms two-sample counter is taken off the UI thread. Unsupported
    counters fail closed; manual mode remains available.
    """
    import ctypes as c
    from ctypes import wintypes as w
    import sys
    if sys.platform != 'win32':
        return Sample()
    user = c.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    user.MonitorFromWindow.argtypes = (w.HWND, w.DWORD)
    user.MonitorFromWindow.restype = w.HANDLE
    user.GetWindowRect.argtypes = (w.HWND, c.POINTER(w.RECT))
    user.GetWindowThreadProcessId.argtypes = (w.HWND, c.POINTER(w.DWORD))
    user.IsIconic.argtypes = (w.HWND,)
    user.GetClassNameW.argtypes = (w.HWND, w.LPWSTR, c.c_int)

    class Monitor(c.Structure):
        _fields_ = [('size', w.DWORD), ('monitor', w.RECT), ('work', w.RECT), ('flags', w.DWORD)]

    user.GetMonitorInfoW.argtypes = (w.HANDLE, c.POINTER(Monitor))
    hwnd = user.GetForegroundWindow()
    rect, pid = w.RECT(), w.DWORD()
    monitor = Monitor(size=c.sizeof(Monitor))
    if (not hwnd or user.IsIconic(hwnd) or not user.GetWindowRect(hwnd, c.byref(rect))
            or not user.GetMonitorInfoW(user.MonitorFromWindow(hwnd, 2), c.byref(monitor))):
        return Sample()
    user.GetWindowThreadProcessId(hwnd, c.byref(pid))
    full = all(abs(getattr(rect, edge) - getattr(monitor.monitor, edge)) <= 2
               for edge in ('left', 'top', 'right', 'bottom'))
    name = process_name(pid.value)
    window_class = c.create_unicode_buffer(256)
    user.GetClassNameW(hwnd, window_class, len(window_class))
    # Uninspectable processes fail closed; manual mode remains available.
    busy = bool(full and name and not excluded_process(name) and gpu_busy(pid.value))
    return Sample(full, busy, pid.value, int(hwnd), process_name=name, window_class=window_class.value)


def process_name(pid):
    import psutil
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return ''


def gpu_busy(pid):
    import ctypes as c
    from ctypes import wintypes as w
    import time

    class ValueUnion(c.Union):
        _fields_ = [('number', c.c_double), ('large', c.c_longlong)]

    class Value(c.Structure):
        _anonymous_ = ('value',)
        _fields_ = [('status', w.DWORD), ('value', ValueUnion)]

    class Item(c.Structure):
        _fields_ = [('name', w.LPWSTR), ('value', Value)]

    pdh = c.WinDLL('pdh')
    query, counter = w.HANDLE(), w.HANDLE()
    pdh.PdhOpenQueryW.argtypes = (w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE))
    pdh.PdhAddEnglishCounterW.argtypes = (w.HANDLE, w.LPCWSTR, c.c_size_t, c.POINTER(w.HANDLE))
    pdh.PdhCollectQueryData.argtypes = (w.HANDLE,)
    pdh.PdhCloseQuery.argtypes = (w.HANDLE,)
    pdh.PdhGetFormattedCounterArrayW.argtypes = (w.HANDLE, w.DWORD, c.POINTER(w.DWORD), c.POINTER(w.DWORD), c.c_void_p)
    if pdh.PdhOpenQueryW(None, 0, c.byref(query)):
        return False
    try:
        path = r'\GPU Engine(*)\Utilization Percentage'
        if pdh.PdhAddEnglishCounterW(query, path, 0, c.byref(counter)):
            return False
        pdh.PdhCollectQueryData(query)
        time.sleep(.1)
        if pdh.PdhCollectQueryData(query):
            return False
        size, count = w.DWORD(), w.DWORD()
        pdh.PdhGetFormattedCounterArrayW(counter, 0x200, c.byref(size), c.byref(count), None)
        if not size.value:
            return False
        buffer = c.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(counter, 0x200, c.byref(size), c.byref(count), buffer):
            return False
        items = c.cast(buffer, c.POINTER(Item))
        return any(items[i].value.status in (0, 1) and items[i].value.number >= 10
                   and items[i].name.startswith(f'pid_{pid}_') and 'engtype_3D' in items[i].name
                   for i in range(count.value))
    finally:
        pdh.PdhCloseQuery(query)
