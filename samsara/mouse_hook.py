"""Win32 low-level mouse hook for Mouse 4/5 bindings (command mode and the
main record hotkey) with per-button suppression.

Allows per-event suppression -- we can consume Mouse 4 clicks (preventing
browser-back) while passing through every other mouse event.

THE HOOK THREAD NEVER DOES WORK (32, 2026-09-13)
------------------------------------------------
A WH_MOUSE_LL callback runs while Windows holds EVERY mouse event for EVERY
application: nothing moves or clicks anywhere until it returns. The old
callback ran dictation's handler synchronously -- start_recording(), audio
ducking, earcons -- and the owner's log shows 607 ms and ~160 ms per Mouse 4
press spent inside it (a 2.5 s ducking-host timeout was possible). His left,
right, wheel and Mouse 5 stopped responding and he had to end the process.

So the callback does three things only -- read the event, decide suppression,
enqueue (button, pressed, timestamp, injected) -- and returns. A dispatcher
thread (thread_registry 'mouse-hook-dispatch') delivers events to
on_button_event and runs the watchdog. The callback takes no Python-level
lock and never logs; it bumps counters the dispatcher reports.

Watchdog: physical Raw Input from a message-only INPUTSINK window provides
independent X-button edges and passive evidence of low-level callback loss.
Cursor repositioning never condemns a hook. No liveness input is injected.
Refresh installs the replacement before removing the old handle and retains
suppression; repeated losses share the reinstall budget and backoff.

Panic release: stop()/release() unhook on the thread that installed the hook
(after its message loop ends); an atexit handler releases every live hook.
The OS also removes a hook when its process exits, so a crashed process
cannot keep the mouse.

1255: ProcessMouseHook is the application default. The native pump runs in a
stdlib-only helper with its own GIL, with buffered asynchronous pipe output.
MouseHook remains the one-release in-process fallback; its ctypes callback
can still be delayed by application GIL contention.
"""

import atexit
import collections
import ctypes
import ctypes.wintypes
import queue
import sys
import threading
import time
import weakref

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

# Win32 constants
WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP   = 0x020C
XBUTTON1 = 0x0001   # Mouse 4
XBUTTON2 = 0x0002   # Mouse 5
WM_QUIT  = 0x0012
WM_HOOK_PING = 0x8000 + 867
WM_HOOK_REFRESH = 0x8000 + 1227
WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x100
RID_INPUT = 0x10000003
RAW_EDGE_WAIT_S = 0.020
RAW_MOTION_LIMIT = 50
LLMHF_INJECTED = 0x0001   # MSLLHOOKSTRUCT.flags: software-generated input

#: Events waiting for the dispatcher beyond this drop the OLDEST.
QUEUE_BOUND = 32
#: A hook callback slower than this is logged at WARNING (Windows removes a
#: hook that exceeds LowLevelHooksTimeout, a few hundred ms by default).
SLOW_CALLBACK_MS = 5.0
#: Fast reinstalls per window, followed by bounded, persistent backoff.
REINSTALL_LIMIT = 3
REINSTALL_WINDOW_S = 60.0
RETRY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
INSTALL_RETRY_S = 300.0
HEALTHY_RESET_S = 60.0
#: How often the dispatcher runs the watchdog.
WATCHDOG_INTERVAL_S = 0.5
# 1227: an idle removed/blocked hook must not swallow the next hold. Only the
# configured hold button is sampled; toggle/command bindings keep hook edges.
BUTTON_POLL_S = 0.05
#: Passive diagnostic cadence (kept for callers of the watchdog helpers).
LIVENESS_CHECK_S = 1.0
#: Compatibility diagnostic interval; raw loss uses RAW_EDGE_WAIT_S / 1 second.
SILENT_CHECKS = 3
#: Maximum wait for the optional message-pump diagnostic (no input involved).
PROBE_WAIT_S = 0.25
ACTIVE_RETRY_S = 30.0
#: Once actual loss is detected during activity, do not wait behind the
#: install-error/probe history. Still bound retries if a thread keeps dying.
ACTIVE_LOSS_RETRY_S = 1.0
INPUT_IDLE_S = 30.0

_perf = time.perf_counter
_sleep = time.sleep
_STOP = object()
_WAKE = object()
_REFRESH = object()
_RAW = object()

if sys.platform == 'win32':
    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32



    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt",          ctypes.wintypes.POINT),
            ("mouseData",   ctypes.wintypes.DWORD),
            ("flags",       ctypes.wintypes.DWORD),
            ("time",        ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]


    # Use LPARAM (not POINTER(MSLLHOOKSTRUCT)) for the third argument.
    # Python 3.13 tightened WINFUNCTYPE type validation; passing a ctypes
    # Pointer type here causes ArgumentError when SetWindowsHookExW is called.
    # The standard Windows HOOKPROC signature passes l_param as a raw pointer
    # integer (LPARAM); we cast it to POINTER(MSLLHOOKSTRUCT) inside the callback.
    LowLevelMouseProc = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )


    def _set_windows_hook_ex():
        """The SetWindowsHookExW to call.

        ctypes.windll.user32 is one shared object per process, and pynput
        (imported by the keyboard listener before this hook starts) declares
        ITS OWN HOOKPROC type in SetWindowsHookExW.argtypes on it. Our
        LowLevelMouseProc is a different WINFUNCTYPE, so calling that shared
        function raises "expected WinFunctionType instance instead of
        WinFunctionType" and the hook silently never installs -- the whole
        reason Mouse 4 "did nothing" (28, 2026-09-13). A fresh pointer from the
        library (CDLL.__getitem__ never caches) carries no foreign contract, and
        we give it ours plus a pointer-sized restype so the hook handle is not
        truncated on x64. pynput's own pointer is left untouched. When nothing
        has declared a contract (or tests have patched the attribute) the
        shared attribute is used as before.
        """
        fn = _user32.SetWindowsHookExW
        argtypes = getattr(fn, 'argtypes', None)
        try:
            foreign = bool(argtypes) and len(argtypes) >= 2 and argtypes[1] is not LowLevelMouseProc
        except TypeError:
            foreign = False
        fresh = _user32['SetWindowsHookExW'] if foreign else fn
        fresh.argtypes = (ctypes.c_int, LowLevelMouseProc, ctypes.c_void_p, ctypes.wintypes.DWORD)
        fresh.restype = ctypes.c_void_p
        return fresh


    def _unhook_windows_hook_ex(handle):
        # Declare this even when no other library has touched user32. Default
        # ctypes int conversion truncates HHOOK values on 64-bit Windows.
        fn = _user32.UnhookWindowsHookEx
        fn.argtypes = (ctypes.c_void_p,)
        fn.restype = ctypes.wintypes.BOOL
        return fn(handle)


    # CallNextHookEx with a 64-bit-safe contract. pynput declares argtypes on the
    # shared user32 function when it is imported (the app always imports it);
    # without them ctypes converts the LPARAM pointer as a 32-bit int, raises
    # OverflowError inside the callback, and the chain to every OLDER low-level
    # hook is cut -- the same starvation that makes a hook look "lost" (35).
    _call_next_fresh = _user32['CallNextHookEx']
    _call_next_fresh.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
    _call_next_fresh.restype = ctypes.c_long   # the hook proc returns c_long
    _CFuncPtr = ctypes._CFuncPtr


    def _call_next_hook_ex(hook_id, n_code, w_param, l_param):
        fn = _user32.CallNextHookEx
        if isinstance(fn, _CFuncPtr) and not fn.argtypes:
            fn = _call_next_fresh
        return fn(hook_id, n_code, w_param, l_param)


    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [('dwType', ctypes.wintypes.DWORD), ('dwSize', ctypes.wintypes.DWORD),
                    ('hDevice', ctypes.c_void_p), ('wParam', ctypes.wintypes.WPARAM)]


    class _RawButtons(ctypes.Structure):
        _fields_ = [('usButtonFlags', ctypes.wintypes.USHORT),
                    ('usButtonData', ctypes.wintypes.USHORT)]


    class _RawButtonUnion(ctypes.Union):
        _anonymous_ = ('buttons',)
        _fields_ = [('ulButtons', ctypes.wintypes.ULONG), ('buttons', _RawButtons)]


    class RAWMOUSE(ctypes.Structure):
        _anonymous_ = ('u',)
        _fields_ = [('usFlags', ctypes.wintypes.USHORT), ('u', _RawButtonUnion),
                    ('ulRawButtons', ctypes.wintypes.ULONG), ('lLastX', ctypes.wintypes.LONG),
                    ('lLastY', ctypes.wintypes.LONG), ('ulExtraInformation', ctypes.wintypes.ULONG)]


    class RAWINPUT(ctypes.Structure):
        _fields_ = [('header', RAWINPUTHEADER), ('mouse', RAWMOUSE)]


    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [('usUsagePage', ctypes.wintypes.USHORT), ('usUsage', ctypes.wintypes.USHORT),
                    ('dwFlags', ctypes.wintypes.DWORD), ('hwndTarget', ctypes.c_void_p)]


    WindowProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.wintypes.UINT,
                                   ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)


    class WNDCLASSW(ctypes.Structure):
        _fields_ = [('style', ctypes.wintypes.UINT), ('lpfnWndProc', WindowProc),
                    ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                    ('hInstance', ctypes.c_void_p), ('hIcon', ctypes.c_void_p),
                    ('hCursor', ctypes.c_void_p), ('hbrBackground', ctypes.c_void_p),
                    ('lpszMenuName', ctypes.c_wchar_p), ('lpszClassName', ctypes.c_wchar_p)]


def _native(library, name, args, result):
    """A private pointer avoids changing another library's ctypes contracts."""
    fn = library[name]
    fn.argtypes, fn.restype = args, result
    return fn


def _foreground_not_elevated():
    """Unknown/access-denied foreground integrity is never loss evidence."""
    process = token = None
    close = _native(_kernel32, 'CloseHandle', (ctypes.c_void_p,), ctypes.wintypes.BOOL)
    try:
        foreground = _native(_user32, 'GetForegroundWindow', (), ctypes.c_void_p)()
        pid = ctypes.wintypes.DWORD()
        if not foreground:
            return False
        _native(_user32, 'GetWindowThreadProcessId', (ctypes.c_void_p, ctypes.c_void_p),
                ctypes.wintypes.DWORD)(foreground, ctypes.byref(pid))
        process = _native(_kernel32, 'OpenProcess',
                          (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD),
                          ctypes.c_void_p)(0x1000, False, pid.value)
        if not process:
            return False
        token = ctypes.c_void_p()
        advapi = ctypes.windll.advapi32
        if not _native(advapi, 'OpenProcessToken',
                       (ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p),
                       ctypes.wintypes.BOOL)(process, 0x0008, ctypes.byref(token)):
            return False
        elevated, size = ctypes.wintypes.DWORD(), ctypes.wintypes.DWORD()
        ok = _native(advapi, 'GetTokenInformation',
                     (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                      ctypes.wintypes.DWORD, ctypes.c_void_p), ctypes.wintypes.BOOL)(
                          token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size))
        return bool(ok) and not elevated.value
    except (OSError, AttributeError, TypeError):
        return False
    finally:
        if token and token.value:
            close(token)
        if process:
            close(process)



def _input_status():
    """(own input desktop, idle seconds, last-input tick), or unknown.

    Poll only from the dispatcher; never switch desktops or inject input here.
    Desktop handles need private, pointer-sized ctypes contracts (like the hook).
    """
    if sys.platform != 'win32':
        return None
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.wintypes.UINT), ('dwTime', ctypes.wintypes.DWORD)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    idle = ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    open_desktop = _user32['OpenInputDesktop']
    open_desktop.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
    open_desktop.restype = ctypes.c_void_p
    desktop = open_desktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desktop:
        return False, idle, info.dwTime
    try:
        thread_desktop = _user32['GetThreadDesktop']
        thread_desktop.argtypes = (ctypes.wintypes.DWORD,)
        thread_desktop.restype = ctypes.c_void_p
        get_name = _user32['GetUserObjectInformationW']
        get_name.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                            ctypes.wintypes.DWORD, ctypes.c_void_p)
        names = []
        for handle in (desktop, thread_desktop(_kernel32.GetCurrentThreadId())):
            name = ctypes.create_unicode_buffer(256)
            needed = ctypes.wintypes.DWORD()
            if not get_name(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return None
            names.append(name.value)
        return names[0] == names[1], idle, info.dwTime
    finally:
        close = _user32['CloseDesktop']
        close.argtypes = (ctypes.c_void_p,)
        close(desktop)


def _coerce_suppress_buttons(value) -> frozenset:
    """None -> empty set; 'mouse4' -> {'mouse4'}; any iterable -> its set."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset((value,))
    return frozenset(value)


def _button_pressed(name):
    """Current high bit only; GetAsyncKeyState's shared edge bit is unreliable."""
    return bool(_user32.GetAsyncKeyState(0x05 if name == 'mouse4' else 0x06) & 0x8000)


def _raise_hook_priority():
    """Above-normal for the tiny pump, never the decode process or realtime."""
    current = _kernel32['GetCurrentThread']
    current.restype = ctypes.c_void_p
    priority = _kernel32['SetThreadPriority']
    priority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    priority.restype = ctypes.wintypes.BOOL
    return bool(priority(current(), 1))  # THREAD_PRIORITY_ABOVE_NORMAL


#: Every started hook, so interpreter exit can release them (atexit below).
_live_hooks = weakref.WeakSet()


def stall_snapshot():
    """Read callback timestamps off-thread without waiting for the hook pump."""
    now = _perf()
    result = []
    for hook in list(_live_hooks):
        started = hook._callback_started_at
        last = hook.last_callback_at
        result.append({
            'thread_id': hook._thread_id,
            'callback_count': hook.callback_count,
            'last_callback_age_s': None if last is None else max(0.0, now - last),
            'callback_running_s': 0.0 if started is None else max(0.0, now - started),
        })
    return tuple(result)


def release_all_hooks() -> None:
    """Uninstall every live mouse hook. Registered with atexit; safe to call twice."""
    for hook in list(_live_hooks):
        try:
            hook.stop()
        except Exception as exc:   # exit path: keep releasing the rest
            logger.error(f"[MOUSE HOOK] atexit release failed: {exc}")


atexit.register(release_all_hooks)


class MouseHook:
    """Win32 WH_MOUSE_LL hook that optionally suppresses X buttons.

    One instance serves every mouse-bound feature (command mode, the main
    record hotkey); the dispatcher routes each event.

    Args:
        on_button_event: called as on_button_event(button_name, pressed) for
            every XBUTTON event, ON THE DISPATCHER THREAD (never the hook
            thread). button_name is 'mouse4' or 'mouse5'.
        suppress_buttons: set of 'mouse4'/'mouse5' whose events return 1 from
            the hook so the OS never sees them. A single name or None is
            accepted and coerced.
        suppress_button: legacy single-button spelling of suppress_buttons,
            used only when suppress_buttons is not given.
        on_hook_failed: called once per interruption on the dispatcher thread.
            Recovery continues until stop()/release().
        on_hook_recovered: called on the dispatcher thread to clear fallback;
            recovery is logged, without a user notification.
        on_hook_interrupted: optional app state callback for each recovered
            blip and install-error escalation; replaces the legacy notice.
    """

    def __init__(self, on_button_event, suppress_buttons=None, *, suppress_button='mouse4',
                 on_hook_failed=None, on_hook_recovered=None, on_hook_interrupted=None,
                 poll_buttons=()):
        self.on_button_event = on_button_event
        self.on_hook_failed = on_hook_failed
        self.on_hook_interrupted = on_hook_interrupted
        self._interruption_reported = False
        self._interruption_install_failed = False
        self.on_hook_recovered = on_hook_recovered
        self.suppress_buttons = _coerce_suppress_buttons(
            suppress_button if suppress_buttons is None else suppress_buttons
        )
        self.requested_suppress_buttons = self.suppress_buttons
        self.poll_buttons = _coerce_suppress_buttons(poll_buttons) & {'mouse4', 'mouse5'}
        self._button_down = {}
        self._button_event_at = {}
        self._poll_armed = set()
        self._polled_down = set()
        self._poll_stamp = {}
        self._button_loss_reason = ''
        self._poll_due = 0.0
        self.callback_max_ms = 0.0
        self._refresh_pending = False
        self._refresh_reason = ''
        self._refresh_backoff = False
        self._recovery_reason = ''
        self._retired_hooks = []
        self._raw_window = None
        self._raw_class = None
        self._raw_instance = None
        self._window_proc = None
        self._raw_activity = collections.deque(maxlen=4096)
        self._raw_edges = collections.deque(maxlen=128)
        self._ll_button_at = {}
        self._physical_callback_at = None
        self.raw_input_error = ''
        self.hook_latency_max_ms = 0
        self._reported_latency_ms = 0
        self.retries_automatically = sys.platform == 'win32'
        self.recovering = False
        self._retry_due = None
        self._backoff_step = 0
        self._episode_notified = False
        self._healthy_since = None
        self._recovery_count = 0
        self._confirm_due = 0.0
        self._pump_sequence = 0
        self._pump_ack = 0
        self._pump_checked_at = None
        self._pump_ok = None
        self._loop_state = 'not started'
        self._last_event = None
        self._probe_event = None
        self._wake_queued = False
        self._activity_active = False
        self._input_paused = False
        self._last_input_tick = None
        self._last_real_input_at = None
        self._probe_input_tick = None
        self._retry_input_tick = None
        self._input_retry_used = False
        self._hook_id = None
        self._thread = None
        self._thread_id = None
        #: Why the hook is not installed, for the app log ('' while it is).
        self.install_error = ''
        self._ready = threading.Event()
        # Ref must stay alive for the lifetime of the hook
        self._proc = LowLevelMouseProc(self._hook_callback) if sys.platform == 'win32' else None
        if sys.platform != 'win32':
            self.install_error = "Windows mouse hooks unavailable on this platform"

        # Hook thread -> dispatcher: the only thing the callback writes to.
        self._events = queue.SimpleQueue()   # C put(): no Python lock, never blocks
        self._dispatcher = None
        self._stopping = threading.Event()
        # Counters bumped by the callback, reported by the dispatcher.
        self.callback_count = 0
        self.dropped_events = 0
        self.slow_callbacks = 0
        self.slow_callback_max_ms = 0.0
        self.callback_errors = 0
        self.last_callback_ms = 0.0
        self.last_callback_at = None
        self._callback_started_at = None
        self._hold_recovery_count = 0
        self._hold_healthy_at = 0.0
        self._reported = {'dropped': 0, 'slow': 0, 'errors': 0}
        self._drop_burst_open = False
        # Watchdog state.
        self._reinstalls = collections.deque()
        self._watch_due = 0.0
        self._live_due = 0.0
        self._watch_count = 0
        self._watch_pos = None
        self._silent_checks = 0
        #: Self-tests the hook answered while the cursor moved silently (false alarms avoided).
        self.false_alarms = 0
        self._false_alarm_logged = False
        self.gave_up = False
        #: Thread id that ran UnhookWindowsHookEx last (the installing thread
        #: unless the fallback had to be used).
        self.unhooked_on_thread = None

    # ------------------------------------------------------------------
    # Hook callback (called on the hook thread by Windows)
    # ------------------------------------------------------------------

    def _hook_callback(self, n_code, w_param, l_param):
        """Time the entire callback, including a downstream native hook wait."""
        t0 = _perf()
        injected = True
        self._callback_started_at = self.last_callback_at = t0
        try:
            suppress = False
            try:
                self.callback_count += 1   # EVERY callback is liveness: move, wheel, any button (35)
                if n_code >= 0:
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    injected = bool(info.flags & LLMHF_INJECTED)
                    self._last_event = (t0, int(w_param), int(info.flags))
                    # Windows stamped this before Python could acquire the
                    # GIL. Unsigned subtraction also handles tick rollover.
                    latency = (((int(_kernel32.GetTickCount()) - int(info.time)) & 0xFFFFFFFF)
                               if info.time else 0)
                    self.hook_latency_max_ms = max(self.hook_latency_max_ms, latency)
                    if not injected:
                        self._physical_callback_at = t0
                    if self.recovering and not self._wake_queued:
                        self._wake_queued = True
                        self._events.put(_WAKE)
                if n_code >= 0 and (w_param == WM_XBUTTONDOWN or w_param == WM_XBUTTONUP):
                    name = 'mouse4' if ((info.mouseData >> 16) & 0xFFFF) == XBUTTON1 else 'mouse5'
                    if not injected:
                        self._ll_button_at[name, w_param == WM_XBUTTONDOWN] = t0
                    polled_release = w_param == WM_XBUTTONUP and name in self._polled_down
                    events = self._events
                    events.put((name, w_param == WM_XBUTTONDOWN, t0, bool(info.flags & LLMHF_INJECTED)))
                    if events.qsize() > QUEUE_BOUND:
                        try:
                            events.get_nowait()   # drop the OLDEST; a wedged dispatcher never stalls us
                            self.dropped_events += 1
                        except queue.Empty:
                            pass
                    # The first real press already proves life. Suppress it
                    # now; app recovery runs before its dispatcher delivery.
                    buttons = (self.requested_suppress_buttons if self.recovering and not injected
                               else self.suppress_buttons)
                    suppress = name in buttons and not polled_release
                    # A sampled down reached Windows. Let its real up through
                    # too, otherwise async state can stay down after recovery.
            except BaseException:
                self.callback_errors += 1
                suppress = False           # broken callback: never eat a click
            if suppress:
                return 1  # consume -- the OS does not see this X button
            try:
                return _call_next_hook_ex(self._hook_id, n_code, w_param, l_param)
            except BaseException:
                self.callback_errors += 1
                return 0
        finally:
            elapsed = (_perf() - t0) * 1000.0
            self._callback_started_at = None
            self.last_callback_ms = elapsed
            self.callback_max_ms = max(self.callback_max_ms, elapsed)
            if elapsed > SLOW_CALLBACK_MS:
                self.slow_callbacks += 1
                if elapsed > self.slow_callback_max_ms:
                    self.slow_callback_max_ms = elapsed
            elif n_code >= 0 and not injected and w_param != WM_XBUTTONUP:
                self._hold_healthy_at = _perf()

    def hold_health(self):
        """Lock-free counters for the independent active-hold release worker."""
        running = self._callback_started_at
        elapsed = 0.0 if running is None else max(0.0, (_perf() - running) * 1000)
        return ((self.slow_callbacks, self._hold_recovery_count),
                self.recovering or elapsed > SLOW_CALLBACK_MS,
                self._hold_healthy_at, max(elapsed, self.last_callback_ms))

    # ------------------------------------------------------------------
    # Dispatcher (its own thread): delivery + watchdog
    # ------------------------------------------------------------------

    def _deliver(self, item, *, polled=False, raw=False) -> None:
        self._recover_from_callbacks()
        if item is _WAKE:
            return
        if item[0] is _REFRESH:
            _, reason, new_hook, removed, error = item
            logger.info('[MOUSE HOOK] hook refresh cause=%s installed=%s '
                        'old_removed=%s error=%s callback_max_ms=%.3f',
                        reason, bool(new_hook), removed, error, self.callback_max_ms)
            if self._retired_hooks:
                self._schedule_retry(f'old hook cleanup failed (error={error})', refresh=True)
            elif not new_hook:
                self._schedule_retry(f'hook refresh failed (error={error})', install_failed=True, refresh=True)
            return
        if item[0] is _RAW:
            self._deliver(item[1:], raw=True)
            return
        name, pressed, stamp, injected = item
        if raw:
            self._polled_down.discard(name)
            self._poll_stamp[name] = max(stamp, self._poll_stamp.get(name, stamp))
            if pressed:
                self._poll_armed.add(name)
            else:
                self._poll_armed.discard(name)
        # Both native sources describe the same edge. Track every binding,
        # including toggle/command buttons, rather than only polled holds.
        if not hasattr(self, '_button_down'):
            self._button_down = {}  # partial hosts may deliver a release before initialization
        duplicate = self._button_down.get(name) is pressed
        if name in getattr(self, 'poll_buttons', ()) and (not injected or not pressed):
            if not polled:
                # Only downs may be stale: a delivered up always ends a hold,
                # including an injected up or one older than a sampled edge.
                if pressed and not raw and stamp <= self._poll_stamp.get(name, float('-inf')):
                    return
                self._button_event_at[name] = stamp
                self._button_loss_reason = ''
                if pressed:
                    self._poll_armed.add(name)
                    if name in self._polled_down:
                        return
                else:
                    self._polled_down.discard(name)
                    # Async state can still say down after the hook up. Do
                    # not start another hold until polling observes an up.
                    self._poll_armed.discard(name)
                    self._poll_stamp[name] = max(stamp, self._poll_stamp.get(name, stamp))
        if not injected or not pressed:
            self._button_down[name] = pressed
        if duplicate:
            return
        logger.debug("[MOUSE] hook %s pressed=%s injected=%s queued=%.1fms",
                     name, pressed, injected, (_perf() - stamp) * 1000.0)
        try:
            self.on_button_event(name, pressed)
        except Exception as e:
            # In the log, not just the console: a handler that raises is
            # otherwise indistinguishable from a hook that never fires.
            logger.exception(f"[MOUSE HOOK] callback error: {e}")

    def dispatch_pending(self) -> int:
        """Deliver every queued event on the CALLING thread; returns how many.
        The dispatcher uses the same path; tests call it directly."""
        delivered = 0
        while True:
            try:
                item = self._events.get_nowait()
            except queue.Empty:
                return delivered
            if item is _STOP:
                continue
            self._deliver(item)
            delivered += item is not _WAKE and item[0] is not _REFRESH

    def _dispatch_loop(self) -> None:
        while True:
            try:
                item = self._events.get(timeout=RAW_EDGE_WAIT_S)
            except queue.Empty:
                item = None
            if item is _STOP or self._stopping.is_set():
                return
            if item is not None:
                self._deliver(item)
            now = _perf()
            self._poll_if_due()
            if now >= self._watch_due or self._raw_edges:
                self._watch_due = now + WATCHDOG_INTERVAL_S
                try:
                    self.watchdog_tick()
                except Exception as e:
                    logger.exception(f"[MOUSE HOOK] watchdog error: {e}")

    def _poll_if_due(self) -> None:
        if _perf() >= self._poll_due:
            self._poll_due = _perf() + BUTTON_POLL_S
            try:
                self._poll_hold_buttons()
            except Exception:
                logger.exception('[MOUSE HOOK] physical button sample failed')

    def _poll_hold_buttons(self) -> None:
        """Dispatcher-only fallback for a MISSED press and its paired release.

        Suppressed hook presses need not set the async-state high bit. Never
        infer their release from zero here: the existing active-hold worker
        owns that case. A sampled press passed through Windows, so its up can
        safely be sampled as well. No model/audio work runs on the hook pump.
        """
        if not self.poll_buttons or self._stopping.is_set():
            return
        status = _input_status()
        if status is None or not status[0]:
            return  # async zero on another/secure desktop is not a release
        self.dispatch_pending()
        for name in sorted(self.poll_buttons):
            stamp = _perf()
            down = _button_pressed(name)
            if not self._events.empty():
                return  # a button edge raced the native sample; drain next tick
            if not down:
                self._poll_armed.add(name)
            if name not in self._poll_armed:
                continue  # attached while already held; require a fresh edge
            if stamp - self._button_event_at.get(name, float('-inf')) < BUTTON_POLL_S:
                continue  # let a just-queued hook edge win
            if down and not self._button_down.get(name, False):
                self._polled_down.add(name)
                self._button_loss_reason = f'physical {name} press missed hook callback'
            elif down or name not in self._polled_down:
                continue
            else:
                self._polled_down.discard(name)
            self._poll_stamp[name] = stamp
            logger.warning('[MOUSE HOOK] physical fallback button=%s pressed=%s hook_age_s=%s',
                           name, down, None if self.last_callback_at is None
                           else round(stamp - self.last_callback_at, 3))
            self._deliver((name, down, stamp, False), polled=True)

    def _report_counters(self) -> None:
        if self.hook_latency_max_ms > max(SLOW_CALLBACK_MS, self._reported_latency_ms):
            self._reported_latency_ms = self.hook_latency_max_ms
            logger.warning('[MOUSE HOOK] event-to-Python latency=%d ms '
                           '(Windows event time includes GIL wait)', self.hook_latency_max_ms)
        if self.slow_callbacks > self._reported['slow']:
            logger.warning("[MOUSE HOOK] %d hook callback(s) exceeded %.0f ms (slowest %.2f ms) -- "
                           "every mouse event system-wide waits on the callback",
                           self.slow_callbacks - self._reported['slow'], SLOW_CALLBACK_MS,
                           self.slow_callback_max_ms)
            self._reported['slow'] = self.slow_callbacks
            self.slow_callback_max_ms = 0.0
        if self.dropped_events > self._reported['dropped']:
            if not self._drop_burst_open:
                logger.warning("[MOUSE HOOK] dispatcher behind: dropping oldest mouse events "
                               "(queue bound %d)", QUEUE_BOUND)
                self._drop_burst_open = True
            if self._events.qsize() == 0:
                self._reported['dropped'] = self.dropped_events
                self._drop_burst_open = False
        if self.callback_errors > self._reported['errors']:
            logger.warning("[MOUSE HOOK] %d hook callback error(s) (events passed through)",
                           self.callback_errors - self._reported['errors'])
            self._reported['errors'] = self.callback_errors

    def _hook_lost(self, now=None) -> str:
        """Why the hook is gone, or '' while it is alive (or merely unproven)."""
        if not self._hook_id:
            return 'hook handle cleared'
        if self._thread is not None and not self._thread.is_alive():
            return 'hook thread ended'
        if self._button_loss_reason:
            reason, self._button_loss_reason = self._button_loss_reason, ''
            return reason
        now = _perf() if now is None else now
        reason = ''
        while self._raw_edges and now - self._raw_edges[0][2] >= RAW_EDGE_WAIT_S:
            name, pressed, stamp = self._raw_edges.popleft()
            seen = self._ll_button_at.get((name, pressed), float('-inf'))
            if abs(seen - stamp) > RAW_EDGE_WAIT_S:
                reason = f'physical raw {name} edge missed hook callback within 20 ms'
        while self._raw_activity and now - self._raw_activity[0] > 1.0:
            self._raw_activity.popleft()
        if (not reason and len(self._raw_activity) >= RAW_MOTION_LIMIT
                and (self._physical_callback_at is None
                     or self._physical_callback_at < self._raw_activity[0])):
            reason = '50 physical raw mouse events in 1 s with no hook callbacks'
        if reason:
            # Consume this evidence once. Fresh physical input must corroborate
            # another loss; an inaccessible/elevated foreground is inconclusive.
            self._raw_activity.clear()
            before = self.callback_count
            if _foreground_not_elevated() and self.callback_count == before:
                return reason
        return ''

    def _request_refresh(self, reason, *, retry=False):
        """Renew a suspect handle on its own pump, without stopping delivery."""
        if (self._refresh_pending or self._stopping.is_set()
                or self._thread_id is None or self._retry_due is not None):
            return
        now = _perf()
        while self._reinstalls and now - self._reinstalls[0] > REINSTALL_WINDOW_S:
            self._reinstalls.popleft()
        if not retry and (len(self._reinstalls) >= REINSTALL_LIMIT or self._backoff_step):
            self._schedule_retry(reason, refresh=True)
            return
        self._reinstalls.append(now)
        self._refresh_pending = True
        self._refresh_reason = reason
        if not _user32.PostThreadMessageW(self._thread_id, WM_HOOK_REFRESH, 0, 0):
            self._refresh_pending = False
            logger.warning('[MOUSE HOOK] hook refresh request failed cause=%s', reason)
            self._schedule_retry(reason, install_failed=True, refresh=True)

    def _refresh_on_hook_thread(self):
        """No logging, waits or application work while the native hook is live.

        Install first. Keep the old handle listening if installation fails.
        Native unhook evidence is reported later by the dispatcher; successful
        unhook does not prove the old hook delivered input or why it was silent.
        """
        reason = self._refresh_reason
        try:
            if self._stopping.is_set():
                return
            old_hook = self._hook_id
            new_hook = _set_windows_hook_ex()(WH_MOUSE_LL, self._proc, None, 0)
            if new_hook:
                # Record the old handle BEFORE native cleanup: even a ctypes
                # exception must not orphan it after the new handle is live.
                if old_hook:
                    self._retired_hooks.append(old_hook)
                self._hook_id = new_hook
                removed, error = None, 0
                for handle in tuple(self._retired_hooks):
                    removed = bool(_unhook_windows_hook_ex(handle))
                    error = 0 if removed else _kernel32.GetLastError()
                    if removed or error == 1404:  # ERROR_INVALID_HOOK_HANDLE
                        self._retired_hooks.remove(handle)
            else:
                removed, error = None, _kernel32.GetLastError()
            self._events.put((_REFRESH, reason, new_hook, removed, error))
        except (ctypes.ArgumentError, OSError, TypeError):
            self._events.put((_REFRESH, reason, None, None, 'native exception'))
        finally:
            self._refresh_pending = False

    def _self_test(self) -> str:
        """Compatibility diagnostic: passive observation, never inject input."""
        alive = self.callback_count != self._recovery_count
        verdict = 'alive' if alive else 'inconclusive'
        logger.info('[MOUSE HOOK] passive check=%s callbacks=%d loop=%s',
                    verdict, self.callback_count, self._loop_state)
        return verdict

    def _pump_responsive(self) -> bool:
        """Measure a fresh round trip through the installing thread's pump."""
        self._pump_checked_at = _perf()
        self._pump_ok = False
        if self._thread_id is None:
            return False
        self._pump_sequence += 1
        sequence = self._pump_sequence
        started = _perf()
        if not _user32.PostThreadMessageW(self._thread_id, WM_HOOK_PING, sequence, 0):
            return False
        while _perf() - started < PROBE_WAIT_S:
            if self._pump_ack == sequence:
                self._pump_ok = True
                return True
            self._poll_if_due()
            _sleep(0.01)
        return False

    def _recover_from_callbacks(self) -> None:
        """Dispatcher only: recover before routing the first returning press."""
        if self.recovering and self.installed and self.callback_count != self._recovery_count:
            self._recovered()

    def watchdog_tick(self) -> None:
        """Report counters and keep retrying until explicitly stopped."""
        self._report_counters()
        if self._stopping.is_set():
            return
        now = _perf()
        self._recover_from_callbacks()
        # No installing thread means no session to sample (also supports
        # non-Windows callers). Failed native installs retain their thread id.
        status = _input_status() if self._thread_id is not None else None
        resumed = False
        if status is not None:
            available, idle, tick = status
            if tick == self._probe_input_tick and self._last_real_input_at is not None:
                idle = max(idle, now - self._last_real_input_at)
            else:
                self._last_real_input_at = now - idle
                self._last_input_tick = tick
            self._activity_active = available and idle < INPUT_IDLE_S
            if not self._activity_active:
                self._input_paused = True
                self._watch_pos, self._watch_count, self._silent_checks = None, self.callback_count, 0
                self._input_retry_used = False
                return
            resumed, self._input_paused = self._input_paused, False
            if self._retry_due is not None:
                if not self._refresh_backoff:
                    self._retry_due = min(self._retry_due, now + ACTIVE_RETRY_S)
                if not self._refresh_backoff and (resumed or (
                        not self._input_retry_used and self._last_input_tick != self._retry_input_tick)):
                    self._retry_due = now
                    self._input_retry_used = True
            elif resumed:
                self._confirm_due = now
        # Returning from idle is not mouse-loss evidence. Only passive raw
        # traffic, missed button sampling or a dead pump can establish loss.
        if self.recovering:
            self._begin_recovery(self.install_error or 'mouse hook interrupted')
        if self.recovering and self.installed:
            if self.callback_count != self._recovery_count:
                self._recovered()
            elif self._retry_due is None and now >= self._confirm_due:
                self._confirm_due = now + SILENT_CHECKS * LIVENESS_CHECK_S
                verdict = self._self_test()
                if verdict == 'alive':
                    self._recovered()
                else:
                    self._recover_from_callbacks()  # a late event outranks the probe
                # A missed replacement probe alone cannot consume reinstalls
                # or enter backoff. Keep listening; check actual loss below.
        elif self._healthy_since is not None and now - self._healthy_since >= HEALTHY_RESET_S:
            self._episode_notified = False
            self._backoff_step = 0
            self._healthy_since = None
        if self._retry_due is not None:
            if now >= self._retry_due:
                self._retry_due = None
                if self.installed and self._thread_id is not None:
                    self._request_refresh(self._recovery_reason or 'scheduled retry', retry=True)
                else:
                    self._stop_hook_thread()
                    self._attempt_install()
            return
        reason = self._hook_lost()
        if reason:
            self._reinstall(reason, active_input=self._activity_active)

    def _reinstall(self, reason: str, *, active_input=False) -> None:
        if self.recovering and self.installed and self.callback_count != self._recovery_count:
            self._recovered()  # a late event outranks a missed probe
            return
        self._begin_recovery(reason)
        if self._stopping.is_set():
            return
        if self.installed and self._thread_id is not None:
            self._request_refresh(reason)
            return
        now = _perf()
        while self._reinstalls and now - self._reinstalls[0] > REINSTALL_WINDOW_S:
            self._reinstalls.popleft()
        if len(self._reinstalls) >= REINSTALL_LIMIT or self._backoff_step:
            # Keep the replacement and suppression while bounded retry waits.
            self._schedule_retry(reason, active_loss=active_input)
            return
        self._stop_hook_thread()
        self._reinstalls.append(now)
        logger.warning("[MOUSE HOOK] hook lost (%s) -- reinstalling (%d/%d this minute)",
                       reason, len(self._reinstalls), REINSTALL_LIMIT)
        self._attempt_install()

    def _attempt_install(self) -> None:
        if self._stopping.is_set():
            return
        logger.info('[MOUSE HOOK] reinstall attempt cause=%s',
                    self._recovery_reason or self.install_error or 'hook thread not ready')
        self._recovery_count = self.callback_count
        self._start_hook_thread()
        self._poll_hold_buttons()  # a press spanning native reinstall is retained
        self._watch_pos, self._watch_count, self._silent_checks = None, self.callback_count, 0
        self._live_due = 0.0
        self._confirm_due = _perf() + SILENT_CHECKS * LIVENESS_CHECK_S
        if not self.installed:
            logger.error(f"[MOUSE HOOK] reinstall failed: {self.install_error or 'unknown reason'}")
            if self.install_error:
                self._begin_recovery(self.install_error)
            self._schedule_retry(self.install_error or 'hook thread not ready', install_failed=True)

    def _schedule_retry(self, reason: str, *, install_failed=False, active_loss=False, refresh=False) -> None:
        if self._retry_due is not None or self._stopping.is_set():
            return
        delay = (INSTALL_RETRY_S if install_failed else
                 RETRY_BACKOFF_S[min(self._backoff_step, len(RETRY_BACKOFF_S) - 1)])
        if self._activity_active:
            delay = min(delay, ACTIVE_RETRY_S)
        if active_loss and not install_failed:
            delay = min(delay, ACTIVE_LOSS_RETRY_S)
        self._backoff_step += 1
        self._retry_due = _perf() + delay
        self._refresh_backoff = refresh
        self._retry_input_tick = self._last_input_tick
        logger.warning("[MOUSE HOOK] %s; suppression retained, retry in %.0fs", reason, delay)

    def _begin_recovery(self, reason: str) -> None:
        if not self.recovering:
            self._recovery_reason = reason
            self._hold_recovery_count += 1
            self._recovery_count = self.callback_count
            self._wake_queued = False
            self._input_retry_used = False
        self.recovering = True
        self._healthy_since = None
        install_failed = bool(self.install_error)
        if self.on_hook_interrupted is not None:
            # App state must follow every loss, independently of the legacy
            # callback's full-healthy-minute suppression and retry backoff.
            if (not self._interruption_reported
                    or (install_failed and not self._interruption_install_failed)):
                self._interruption_reported = True
                self._interruption_install_failed = install_failed
                try:
                    self.on_hook_interrupted(reason)
                except Exception:
                    logger.exception('[MOUSE HOOK] interruption callback raised')
        elif not self._episode_notified and self.on_hook_failed is not None:
            self._episode_notified = True
            try:
                self.on_hook_failed(reason)
            except Exception as e:
                logger.exception(f"[MOUSE HOOK] on_hook_failed raised: {e}")

    def _recovered(self) -> None:
        if self._stopping.is_set():
            return
        self.recovering = False
        self._wake_queued = False
        if not self._retired_hooks:
            self._retry_due = None
            self._refresh_backoff = False
        self.install_error = ''
        # 1019: app state follows each recovered blip; legacy notice/backoff
        # still reset only after a full healthy minute.
        self._interruption_reported = False
        self._interruption_install_failed = False
        self.suppress_buttons = self.requested_suppress_buttons
        self._healthy_since = _perf()
        logger.info("[MOUSE HOOK] recovered; suppression retained")
        if self.on_hook_recovered is not None:
            try:
                self.on_hook_recovered()
            except Exception:
                logger.exception("[MOUSE HOOK] recovery callback raised")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Install the hook on a dedicated thread (Win32 requires this) and
        start the dispatcher."""
        if sys.platform != 'win32':
            self.install_error = "Windows mouse hooks unavailable on this platform"
            return False
        if self._dispatcher is None or not self._dispatcher.is_alive():
            self._events = queue.SimpleQueue()   # no stale _STOP from a previous stop()
        self._stopping.clear()
        self._button_down.clear()
        self._raw_activity.clear()
        self._raw_edges.clear()
        self._ll_button_at.clear()
        self._physical_callback_at = None
        self._refresh_backoff = False
        self._button_event_at.clear()
        self._poll_armed.clear()
        self._polled_down.clear()
        self._poll_stamp.clear()
        self._button_loss_reason = ''
        self._poll_due = 0.0
        self._refresh_pending = False
        self._recovery_reason = ''
        self._poll_hold_buttons()  # arm only buttons currently released
        self.gave_up = False
        self.recovering = False
        self._retry_due = None
        self._backoff_step = 0
        self._reinstalls.clear()
        self._episode_notified = False
        self._interruption_reported = False
        self._interruption_install_failed = False
        self._healthy_since = None
        self._recovery_count = self.callback_count
        self.suppress_buttons = self.requested_suppress_buttons
        self._start_hook_thread()
        if not self.installed:
            self.recovering = True
            self._schedule_retry(self.install_error or 'hook thread not ready', install_failed=True)
        self._watch_due = _perf() + WATCHDOG_INTERVAL_S
        self._live_due = 0.0
        self._watch_pos, self._watch_count, self._silent_checks = None, self.callback_count, 0
        if self._dispatcher is None or not self._dispatcher.is_alive():
            self._dispatcher = thread_registry.spawn('mouse-hook-dispatch', self._dispatch_loop, daemon=True)
        _live_hooks.add(self)

    def _start_hook_thread(self):
        # A starved old thread may outlive the join timeout. Never overlap it
        # with a replacement that its finally block could accidentally unhook.
        if self._stopping.is_set() or (self._thread is not None and self._thread.is_alive()):
            return
        self._ready.clear()
        self._thread = thread_registry.spawn('mouse-hook', self._run, daemon=True)
        self._ready.wait(timeout=2.0)
        if self.installed:
            logger.info('[MOUSE HOOK] Hook installed (id=%s, thread=%s)',
                        self._hook_id, self._thread_id)

    def _raw_mouse(self, packet):
        """Pump only: count physical input and enqueue X-button edges."""
        if (packet.header.dwType != 0 or not packet.header.hDevice
                or self._stopping.is_set()):
            return
        mouse = packet.mouse
        flags = mouse.usButtonFlags
        if not (mouse.lLastX or mouse.lLastY or flags):
            return
        stamp = _perf()
        self._raw_activity.append(stamp)
        for name, down, up in (('mouse4', 0x0040, 0x0080), ('mouse5', 0x0100, 0x0200)):
            for mask, pressed in ((down, True), (up, False)):
                if flags & mask:
                    self._raw_edges.append((name, pressed, stamp))
                    self._events.put((_RAW, name, pressed, stamp, False))

    def _raw_window_proc(self, hwnd, message, w_param, l_param):
        try:
            if message == WM_INPUT:
                size = ctypes.wintypes.UINT(ctypes.sizeof(RAWINPUT))
                packet = RAWINPUT()
                read = _native(_user32, 'GetRawInputData',
                               (ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.wintypes.UINT), ctypes.wintypes.UINT)(
                                    l_param, RID_INPUT, ctypes.byref(packet), ctypes.byref(size),
                                    ctypes.sizeof(RAWINPUTHEADER))
                if read == ctypes.sizeof(RAWINPUT) and packet.header.dwSize >= read:
                    self._raw_mouse(packet)
        except (OSError, ValueError, TypeError, ctypes.ArgumentError):
            self.callback_errors += 1
        # Required for foreground WM_INPUT cleanup, including malformed input.
        return _native(_user32, 'DefWindowProcW',
                       (ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                        ctypes.wintypes.LPARAM), ctypes.c_ssize_t)(hwnd, message, w_param, l_param)

    def _start_raw_input(self):
        """Own a message-only INPUTSINK window on the installing thread."""
        self._window_proc = WindowProc(self._raw_window_proc)
        self._raw_class = f'SamsaraMouseRaw_{id(self):x}'
        wc = WNDCLASSW()
        self._raw_instance = _native(_kernel32, 'GetModuleHandleW', (ctypes.c_wchar_p,),
                                     ctypes.c_void_p)(None)
        wc.hInstance = self._raw_instance
        wc.lpfnWndProc, wc.lpszClassName = self._window_proc, self._raw_class
        atom = _native(_user32, 'RegisterClassW', (ctypes.POINTER(WNDCLASSW),),
                       ctypes.wintypes.ATOM)(ctypes.byref(wc))
        if not atom:
            self._raw_class = None
            raise OSError('Raw Input window class registration failed')
        self._raw_window = _native(_user32, 'CreateWindowExW',
                                  (ctypes.wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   ctypes.wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p), ctypes.c_void_p)(
                                       0, self._raw_class, '', 0, 0, 0, 0, 0, -3, None,
                                       self._raw_instance, None)
        if not self._raw_window:
            raise OSError('Raw Input message-only window creation failed')
        device = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, self._raw_window)
        if not _native(_user32, 'RegisterRawInputDevices',
                       (ctypes.POINTER(RAWINPUTDEVICE), ctypes.wintypes.UINT, ctypes.wintypes.UINT),
                       ctypes.wintypes.BOOL)(ctypes.byref(device), 1, ctypes.sizeof(device)):
            raise OSError('Raw Input mouse registration failed')

    def _stop_raw_input(self):
        if self._raw_window:
            device = RAWINPUTDEVICE(1, 2, 1, None)  # RIDEV_REMOVE requires NULL target
            _native(_user32, 'RegisterRawInputDevices',
                    (ctypes.POINTER(RAWINPUTDEVICE), ctypes.wintypes.UINT, ctypes.wintypes.UINT),
                    ctypes.wintypes.BOOL)(ctypes.byref(device), 1, ctypes.sizeof(device))
            _native(_user32, 'DestroyWindow', (ctypes.c_void_p,), ctypes.wintypes.BOOL)(self._raw_window)
            self._raw_window = None
        if self._raw_class:
            _native(_user32, 'UnregisterClassW', (ctypes.c_wchar_p, ctypes.c_void_p),
                    ctypes.wintypes.BOOL)(self._raw_class, self._raw_instance)
            self._raw_class = None

    @property
    def installed(self) -> bool:
        return bool(self._hook_id) and (self._thread is None or self._thread.is_alive())

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        try:
            logger.info('[MOUSE HOOK] pump priority above_normal=%s', _raise_hook_priority())
        except (OSError, AttributeError, TypeError):
            logger.warning('[MOUSE HOOK] could not raise pump priority', exc_info=True)
        self._loop_state = 'installing'
        try:
            self._hook_id = _set_windows_hook_ex()(WH_MOUSE_LL, self._proc, None, 0)
        except (ctypes.ArgumentError, OSError, TypeError) as e:
            self.install_error = f"SetWindowsHookExW raised: {e}"
            self._loop_state = 'install failed'
            logger.error(f"[MOUSE HOOK] {self.install_error}")
            self._ready.set()
            return
        if not self._hook_id:
            self._loop_state = 'install failed'
            self.install_error = (f"SetWindowsHookExW returned 0 "
                                  f"(GetLastError={ctypes.get_last_error() or _kernel32.GetLastError()})")
            logger.error(f"[MOUSE HOOK] {self.install_error}")
            self._ready.set()
            return

        self.install_error = ''
        try:
            self._start_raw_input()
            self.raw_input_error = ''
        except (OSError, TypeError, ctypes.ArgumentError) as exc:
            self.raw_input_error = str(exc)
            self._stop_raw_input()
            logger.error('[MOUSE HOOK] passive Raw Input unavailable: %s', exc)
        self._ready.set()

        msg = ctypes.wintypes.MSG()
        try:
            if self._stopping.is_set():
                return
            # GetMessageW blocks until a message arrives; WM_QUIT breaks the loop.
            self._loop_state = 'GetMessageW'
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOOK_PING:
                    self._pump_ack = int(msg.wParam)
                    continue
                if msg.message == WM_HOOK_REFRESH:
                    self._refresh_on_hook_thread()
                    continue
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._loop_state = 'ended'
            # Uninstall on the thread that installed it, however the loop ended.
            self._unhook('installing thread')
            self._stop_raw_input()
            logger.info("[MOUSE HOOK] message loop ended")

    def _unhook(self, where: str) -> None:
        hook_id, self._hook_id = self._hook_id, None
        handles, self._retired_hooks = self._retired_hooks, []
        if hook_id:
            handles.append(hook_id)
        for hook_id in handles:
            try:
                removed = _unhook_windows_hook_ex(hook_id)
                error = 0 if removed else _kernel32.GetLastError()
            except (ctypes.ArgumentError, OSError, TypeError):
                removed, error = False, 'native exception'
            if not removed and error != 1404:
                self._retired_hooks.append(hook_id)
            self.unhooked_on_thread = _kernel32.GetCurrentThreadId()
            logger.info('[MOUSE HOOK] UnhookWindowsHookEx(id=%s) on the %s '
                        '(thread=%s) removed=%s error=%s callback_max_ms=%.3f',
                        hook_id, where, self.unhooked_on_thread, bool(removed), error,
                        self.callback_max_ms)

    def _stop_hook_thread(self, timeout: float = 2.0) -> None:
        thread = self._thread
        # Post WM_QUIT to break the blocking GetMessageW call on the hook thread;
        # the thread unhooks itself on the way out (_run's finally).
        if self._thread_id is not None:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if self._hook_id or self._retired_hooks:
            # The installing thread did not get to it (hung or never looped):
            # never keep a hook alive -- unhook from here and say so.
            logger.warning("[MOUSE HOOK] hook thread did not unhook in %.1fs; unhooking from %s",
                           timeout, threading.current_thread().name)
            self._unhook(f"fallback thread {threading.current_thread().name!r}")
        if thread is None or not thread.is_alive():
            self._thread_id = None

    def stop(self):
        """Uninstall the hook, end its message loop and stop the dispatcher."""
        self._stopping.set()
        self.suppress_buttons = frozenset()
        self._retry_due = None
        _live_hooks.discard(self)
        self._stop_hook_thread()
        self._events.put(_STOP)
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher.is_alive() and dispatcher is not threading.current_thread():
            dispatcher.join(timeout=2.0)

    def release(self, why: str = 'released by the user') -> None:
        """Panic release: stop suppressing and uninstall NOW."""
        self.suppress_buttons = frozenset()
        logger.warning(f"[MOUSE HOOK] {why}: suppression off, unhooking")
        self.stop()


# Keep the public backend import without a circular dependency on its base.
def __getattr__(name):
    if name == 'ProcessMouseHook':
        from samsara.mouse_hook_process import ProcessMouseHook
        return ProcessMouseHook
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
