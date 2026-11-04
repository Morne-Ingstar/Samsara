"""Session-local game policy. No configuration, desktop or model work at import."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading

from samsara.config_defaults import cfg_get

log = logging.getLogger(__name__)
# PLACEHOLDER COPY (1058)
NOTICE = "Game mode: dictation on CPU, a little slower"


@dataclass(frozen=True)
class Sample:
    fullscreen: bool = False
    gpu_busy: bool = False
    pid: int = 0


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
    def __init__(self, app, factory, *, probe=None, notify=None):
        self.app, self.factory = app, factory
        self.probe = probe or windows_sample
        self.notify = notify or publish
        self.manual = None
        self.auto_requested = False
        self.sample = Sample()
        self.active = False
        self.resident = None
        self.resident_name = None
        self._original = None
        self._announced = False
        self._load_failed = None
        self._lock = threading.RLock()
        self.stop = threading.Event()

    @property
    def wanted(self):
        if not cfg_get(self.app.config, 'game_mode.enabled'):
            return False
        if self.manual is not None:
            return self.manual
        return self.automatic and self.sample.fullscreen and self.sample.gpu_busy

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
        """Only the monitor loads. Failed loads retain the working model."""
        name = cfg_get(self.app.config, 'game_mode.cpu_model')
        if not self.wanted or (self.resident is not None and self.resident_name == name):
            return
        if self._load_failed == name:
            return
        try:
            model = self.factory(name, device='cpu', compute_type='int8',
                                 cpu_threads=4, num_workers=2)
        except Exception:
            self._load_failed = name
            log.warning('[GAME-MODE] CPU model unavailable', exc_info=True)
            self.notify(self.app, False, 'Game mode could not load its CPU model; dictation kept its current model')
            return
        with self._lock:
            self.resident, self.resident_name = model, name

    def reconcile(self, *, primary=False):
        """Never wait behind a capture or decoder; retry at the next idle."""
        lifecycle = getattr(self.app, '_hold_capture_lifecycle_lock', None)
        if lifecycle is not None and not lifecycle.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if _busy(self.app):
                    return False
                target = self.wanted and self.resident is not None and not primary
                if target == self.active and (not target or self.app.model is self.resident):
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
                    else:
                        self.app.model, self.app.device_type, self.app.compute_type = self._original
                        # Ava may have recovered the primary CUDA model while
                        # dictation was on CPU; retain that real recovery.
                        from samsara.gpu_fallback import ResilientSpeechModel
                        if isinstance(self.app.model, ResilientSpeechModel) and self.app.model._device == 'cpu':
                            self.app.device_type, self.app.compute_type = 'cpu', 'int8'
                        self._original = None
                    self.active = target
                finally:
                    self.app.model_lock.release()
                notice = NOTICE if target and not self._announced else None
                self._announced |= target
                self.notify(self.app, target, notice)
                return True
        finally:
            if lifecycle is not None:
                lifecycle.release()

    def original_model(self):
        with self._lock:
            return self._original[0] if self.active else self.app.model

    def run(self):
        while not self.stop.is_set() and getattr(self.app, '_running', True):
            try:
                if (cfg_get(self.app.config, 'game_mode.enabled')
                        and self.automatic):
                    self.sample = self.probe()
                self.prepare()
                self.reconcile()
            except Exception:
                log.warning('[GAME-MODE] Policy sample unavailable', exc_info=True)
            self.stop.wait(2.0)


def install(app, factory):
    """Called after the primary model is ready; one monitor per app."""
    existing = getattr(app, '_game_mode', None)
    if existing is not None:
        # A deliberate primary-model reload supersedes the old restore point.
        with existing._lock:
            existing.active, existing._original = False, None
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
        indicator = getattr(app, 'listening_indicator', None)
        setter = getattr(indicator, 'set_game_mode', None)
        if setter is not None:
            setter(active)
        if notice and indicator is not None:
            indicator.show_outcome(notice, 'accent' if active else 'error', 8000)

    qt_runtime.post(show)


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
    return Sample(full, full and gpu_busy(pid.value), pid.value)


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
