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

Watchdog: a callback slower than SLOW_CALLBACK_MS is logged at WARNING. A hook
whose thread died is reinstalled. Repeated losses back off without abandoning
recovery. Suppression stays off until callbacks prove the new hook works;
on_hook_failed reports one temporary interruption per episode.

Liveness (35, 2026-09-13): every callback -- move, wheel, any button --
counts. Cursor movement with ZERO callbacks is only a SUSPICION: pointers
driven by SetCursorPos, pen/touch input and another low-level hook that does
not call CallNextHookEx all move the cursor without our hook hearing it (the
owner's watchdog condemned a healthy hook 7 times in 3 minutes on a 1.5 s
sample). A loss needs SILENT_CHECKS consecutive silent checks (>= 3 s) AND a
failed self-test: a zero-motion mouse move injected with SendInput that our
own hook must see within PROBE_WAIT_S.

1142: session input resuming is not mouse-loss evidence. A keyboard event
or a real mouse callback just before the probe can resume the session while
the injected move itself never reaches us. Require silent cursor movement
before acting on a missed probe, including on a replacement hook.

Panic release: stop()/release() unhook on the thread that installed the hook
(after its message loop ends); an atexit handler releases every live hook.
The OS also removes a hook when its process exits, so a crashed process
cannot keep the mouse.

Python note: the callback still needs the GIL to run at all. Keeping it to a
queue put keeps its own cost in microseconds; a thread that holds the GIL for
a long time without releasing it can still delay it, which is what the
slow-callback warning and the dead-hook reinstall are there to surface.
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
LLMHF_INJECTED = 0x0001   # MSLLHOOKSTRUCT.flags: event came from SendInput, not hardware

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
#: Liveness is sampled this often: cursor position vs callbacks since the last check.
LIVENESS_CHECK_S = 1.0
#: Consecutive silent checks (cursor moved, zero callbacks) before the self-test.
#: Measured on the owner's machine (35): while the cursor moves the hook sees
#: 40 callbacks/s at p10, 132/s median, 236/s p90 -- three silent seconds of
#: movement is >= ~120 missing callbacks at the slowest real rate.
SILENT_CHECKS = 3
#: How long the self-test waits for our own injected move to reach the hook.
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
MOUSEEVENTF_MOVE = 0x0001
INPUT_MOUSE = 0
PROBE_TAG = 0x1032

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
        if not foreign:
            return fn
        fresh = _user32['SetWindowsHookExW']
        fresh.argtypes = (ctypes.c_int, LowLevelMouseProc, ctypes.c_void_p, ctypes.wintypes.DWORD)
        fresh.restype = ctypes.c_void_p
        return fresh


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


    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.wintypes.DWORD),
                    ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]


    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


    def _send_zero_move() -> bool:
        """Inject one zero-motion relative mouse move; True if Windows accepted it.
        SendInput is refused (0) on the secure desktop or into higher-integrity
        input queues -- the self-test is then inconclusive, never a loss."""
        inp = _INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_MOVE, 0, PROBE_TAG)
        return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1


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


def _cursor_pos():
    if sys.platform != 'win32':
        return None
    point = ctypes.wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        return None
    return (point.x, point.y)


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
                 on_hook_failed=None, on_hook_recovered=None, on_hook_interrupted=None):
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
                    if info.dwExtraInfo == PROBE_TAG:
                        self._probe_event = self._last_event
                    if self.recovering and not self._wake_queued:
                        self._wake_queued = True
                        self._events.put(_WAKE)
                if n_code >= 0 and (w_param == WM_XBUTTONDOWN or w_param == WM_XBUTTONUP):
                    name = 'mouse4' if ((info.mouseData >> 16) & 0xFFFF) == XBUTTON1 else 'mouse5'
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
                    suppress = name in buttons
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

    def _deliver(self, item) -> None:
        self._recover_from_callbacks()
        if item is _WAKE:
            return
        name, pressed, stamp, injected = item
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
            delivered += item is not _WAKE

    def _dispatch_loop(self) -> None:
        while True:
            try:
                item = self._events.get(timeout=WATCHDOG_INTERVAL_S)
            except queue.Empty:
                item = None
            if item is _STOP or self._stopping.is_set():
                return
            if item is not None:
                self._deliver(item)
            now = _perf()
            if now >= self._watch_due:
                self._watch_due = now + WATCHDOG_INTERVAL_S
                try:
                    self.watchdog_tick()
                except Exception as e:
                    logger.exception(f"[MOUSE HOOK] watchdog error: {e}")

    def _report_counters(self) -> None:
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

    def _hook_lost(self) -> str:
        """Why the hook is gone, or '' while it is alive (or merely unproven)."""
        if not self._hook_id:
            return 'hook handle cleared'
        if self._thread is not None and not self._thread.is_alive():
            return 'hook thread ended'
        now = _perf()
        if now < self._live_due:
            return ''
        self._live_due = now + LIVENESS_CHECK_S
        pos, count = _cursor_pos(), self.callback_count
        moved = pos is not None and self._watch_pos is not None and pos != self._watch_pos
        if moved and count == self._watch_count:
            self._silent_checks += 1
        else:
            self._silent_checks = 0
            self._false_alarm_logged = False
        self._watch_pos, self._watch_count = pos, count
        if self._silent_checks < SILENT_CHECKS:
            return ''
        self._silent_checks = 0
        before = self.callback_count
        verdict = self._self_test()
        if self.callback_count != before:
            verdict = 'alive'  # real input arriving at the verdict boundary wins
        self._watch_count = self.callback_count     # the probe's own callback is not user traffic
        if verdict == 'alive':
            self.false_alarms += 1
            if not self._false_alarm_logged:
                logger.info("[MOUSE HOOK] cursor moved for %d checks with no hook callbacks, but the hook "
                            "answered its self-test -- the movement did not come through the low-level "
                            "mouse hook chain (SetCursorPos pointer, pen/touch, or another hook not "
                            "calling CallNextHookEx); not a loss", SILENT_CHECKS)
                self._false_alarm_logged = True
            return ''
        if verdict == 'inconclusive':
            logger.debug("[MOUSE HOOK] silent cursor movement; self-test inconclusive "
                         "(injection refused or dispatcher/message pump delayed)")
            return ''
        return (f'no hook callbacks for {SILENT_CHECKS} checks of cursor movement and the hook missed '
                f'its own injected self-test')

    def _self_test(self) -> str:
        """'alive' | 'dead' | 'inconclusive': inject a zero-motion move and wait
        for any callback to arrive.

        'dead' means only that the probe missed a responsive hook thread.
        It needs independent mouse-loss evidence before replacing a hook.
        SendInput acceptance does not prove delivery through the hook chain.
        """
        before = self.callback_count
        started = _perf()
        self._probe_event = None
        accepted = False
        verdict = 'inconclusive'
        try:
            accepted = _send_zero_move()
            if accepted:
                deadline = started + PROBE_WAIT_S
                while self.callback_count == before and _perf() < deadline:
                    _sleep(0.01)
                if self.callback_count != before:
                    verdict = 'alive'
                elif _perf() - started <= PROBE_WAIT_S * 2 and self._pump_responsive():
                    verdict = 'alive' if self.callback_count != before else 'dead'
        except Exception:
            logger.debug('[MOUSE HOOK] self-test injection raised', exc_info=True)
        finally:
            # SendInput itself can reset GetLastInputInfo. Do not let our
            # probes look like a returning user or keep an idle session active.
            if accepted and self._last_real_input_at is not None:
                status = _input_status()
                if status is not None:
                    self._probe_input_tick = status[2]
            event = self._last_event
            probe = self._probe_event
            logger.info('[MOUSE HOOK] self-test=%s accepted=%s elapsed_ms=%.1f callbacks=%d '
                        'tag_seen=%s probe_flags=%s probe_injected=%s probe_after_ms=%s '
                        'last_message=%s last_flags=%s callback_age_s=%s '
                        'thread=%s loop=%s pump_ok=%s pump_age_s=%s callback_max_ms=%.2f',
                        verdict, accepted, (_perf() - started) * 1000, self.callback_count - before,
                        probe is not None, None if probe is None else probe[2],
                        None if probe is None else bool(probe[2] & LLMHF_INJECTED),
                        None if probe is None else round((probe[0] - started) * 1000, 1),
                        None if event is None else event[1], None if event is None else event[2],
                        None if self.last_callback_at is None else round(_perf() - self.last_callback_at, 3),
                        self._thread_id, self._loop_state, self._pump_ok,
                        None if self._pump_checked_at is None else round(_perf() - self._pump_checked_at, 3),
                        self.slow_callback_max_ms)
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
                self._retry_due = min(self._retry_due, now + ACTIVE_RETRY_S)
                if resumed or (not self._input_retry_used and self._last_input_tick != self._retry_input_tick):
                    self._retry_due = now
                    self._input_retry_used = True
            elif resumed:
                self._confirm_due = now
        # 1142: GetLastInputInfo includes keyboard/injected input. Returning
        # from idle is not evidence of missing mouse callbacks; _hook_lost
        # below must corroborate a failed probe with silent mouse movement.
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
        now = _perf()
        while self._reinstalls and now - self._reinstalls[0] > REINSTALL_WINDOW_S:
            self._reinstalls.popleft()
        if len(self._reinstalls) >= REINSTALL_LIMIT or self._backoff_step:
            # Keep the replacement listening with suppression off: a failed
            # injection must never prevent a real returning event reaching us.
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
        self._recovery_count = self.callback_count
        self._start_hook_thread()
        self._watch_pos, self._watch_count, self._silent_checks = None, self.callback_count, 0
        self._live_due = 0.0
        self._confirm_due = _perf() + SILENT_CHECKS * LIVENESS_CHECK_S
        if not self.installed:
            logger.error(f"[MOUSE HOOK] reinstall failed: {self.install_error or 'unknown reason'}")
            if self.install_error:
                self._begin_recovery(self.install_error)
            self._schedule_retry(self.install_error or 'hook thread not ready', install_failed=True)

    def _schedule_retry(self, reason: str, *, install_failed=False, active_loss=False) -> None:
        delay = (INSTALL_RETRY_S if install_failed else
                 RETRY_BACKOFF_S[min(self._backoff_step, len(RETRY_BACKOFF_S) - 1)])
        if self._activity_active:
            delay = min(delay, ACTIVE_RETRY_S)
        if active_loss and not install_failed:
            delay = min(delay, ACTIVE_LOSS_RETRY_S)
        self._backoff_step += 1
        self._retry_due = _perf() + delay
        self._retry_input_tick = self._last_input_tick
        logger.warning("[MOUSE HOOK] %s; suppression off, retry in %.0fs", reason, delay)

    def _begin_recovery(self, reason: str) -> None:
        if not self.recovering:
            self._hold_recovery_count += 1
            self._recovery_count = self.callback_count
            self._wake_queued = False
            self._input_retry_used = False
        self.suppress_buttons = frozenset()  # first: never eat a click while unhealthy
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
        self._retry_due = None
        self.install_error = ''
        # 1019: app state follows each recovered blip; legacy notice/backoff
        # still reset only after a full healthy minute.
        self._interruption_reported = False
        self._interruption_install_failed = False
        self.suppress_buttons = self.requested_suppress_buttons
        self._healthy_since = _perf()
        logger.info("[MOUSE HOOK] recovered; suppression restored")
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
            self.suppress_buttons = frozenset()
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

    @property
    def installed(self) -> bool:
        return bool(self._hook_id) and (self._thread is None or self._thread.is_alive())

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
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
        self._ready.set()
        logger.info(f"[MOUSE HOOK] Hook installed (id={self._hook_id}, thread={self._thread_id})")

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
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._loop_state = 'ended'
            # Uninstall on the thread that installed it, however the loop ended.
            self._unhook('installing thread')
            logger.info("[MOUSE HOOK] message loop ended")

    def _unhook(self, where: str) -> None:
        hook_id, self._hook_id = self._hook_id, None
        if hook_id:
            _user32.UnhookWindowsHookEx(hook_id)
            self.unhooked_on_thread = _kernel32.GetCurrentThreadId()
            logger.info(f"[MOUSE HOOK] UnhookWindowsHookEx(id={hook_id}) on the {where} "
                        f"(thread={self.unhooked_on_thread})")

    def _stop_hook_thread(self, timeout: float = 2.0) -> None:
        thread = self._thread
        # Post WM_QUIT to break the blocking GetMessageW call on the hook thread;
        # the thread unhooks itself on the way out (_run's finally).
        if self._thread_id is not None:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if self._hook_id:
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
