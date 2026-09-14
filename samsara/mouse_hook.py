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
Windows removed (LowLevelHooksTimeout) or whose thread died is reinstalled; a
fourth loss inside REINSTALL_WINDOW_S stops suppression, unhooks and calls
on_hook_failed so the app can fall back to the keyboard hotkey.

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
LLMHF_INJECTED = 0x0001   # MSLLHOOKSTRUCT.flags: event came from SendInput, not hardware

#: Events waiting for the dispatcher beyond this drop the OLDEST.
QUEUE_BOUND = 32
#: A hook callback slower than this is logged at WARNING (Windows removes a
#: hook that exceeds LowLevelHooksTimeout, a few hundred ms by default).
SLOW_CALLBACK_MS = 5.0
#: Losses of the hook tolerated per window before giving up.
REINSTALL_LIMIT = 3
REINSTALL_WINDOW_S = 60.0
#: How often the dispatcher runs the watchdog.
WATCHDOG_INTERVAL_S = 0.5
#: Consecutive watchdog intervals with cursor movement but no hook callback
#: before the hook counts as removed (one SetCursorPos jump is not movement
#: over two intervals).
DEAD_HOOK_TICKS = 2

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_perf = time.perf_counter
_STOP = object()


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


def _coerce_suppress_buttons(value) -> frozenset:
    """None -> empty set; 'mouse4' -> {'mouse4'}; any iterable -> its set."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset((value,))
    return frozenset(value)


def _cursor_pos():
    point = ctypes.wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        return None
    return (point.x, point.y)


#: Every started hook, so interpreter exit can release them (atexit below).
_live_hooks = weakref.WeakSet()


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
        on_hook_failed: called as on_hook_failed(reason) on the dispatcher
            thread after the hook was lost too often and has been released.
    """

    def __init__(self, on_button_event, suppress_buttons=None, *, suppress_button='mouse4',
                 on_hook_failed=None):
        self.on_button_event = on_button_event
        self.on_hook_failed = on_hook_failed
        self.suppress_buttons = _coerce_suppress_buttons(
            suppress_button if suppress_buttons is None else suppress_buttons
        )
        self._hook_id = None
        self._thread = None
        self._thread_id = None
        #: Why the hook is not installed, for the app log ('' while it is).
        self.install_error = ''
        self._ready = threading.Event()
        # Ref must stay alive for the lifetime of the hook
        self._proc = LowLevelMouseProc(self._hook_callback)

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
        self._reported = {'dropped': 0, 'slow': 0, 'errors': 0}
        self._drop_burst_open = False
        # Watchdog state.
        self._reinstalls = collections.deque()
        self._watch_due = 0.0
        self._watch_count = 0
        self._watch_pos = None
        self._dead_ticks = 0
        self.gave_up = False
        #: Thread id that ran UnhookWindowsHookEx last (the installing thread
        #: unless the fallback had to be used).
        self.unhooked_on_thread = None

    # ------------------------------------------------------------------
    # Hook callback (called on the hook thread by Windows)
    # ------------------------------------------------------------------

    def _hook_callback(self, n_code, w_param, l_param):
        """Read, decide suppression, enqueue, return. Nothing else, ever."""
        t0 = _perf()
        suppress = False
        try:
            self.callback_count += 1
            if n_code >= 0 and (w_param == WM_XBUTTONDOWN or w_param == WM_XBUTTONUP):
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                name = 'mouse4' if ((info.mouseData >> 16) & 0xFFFF) == XBUTTON1 else 'mouse5'
                events = self._events
                events.put((name, w_param == WM_XBUTTONDOWN, t0, bool(info.flags & LLMHF_INJECTED)))
                if events.qsize() > QUEUE_BOUND:
                    try:
                        events.get_nowait()   # drop the OLDEST; a wedged dispatcher never stalls us
                        self.dropped_events += 1
                    except queue.Empty:
                        pass
                suppress = name in self.suppress_buttons
        except BaseException:
            self.callback_errors += 1
            suppress = False           # broken callback: never eat a click
        finally:
            elapsed = (_perf() - t0) * 1000.0
            self.last_callback_ms = elapsed
            if elapsed > SLOW_CALLBACK_MS:
                self.slow_callbacks += 1
                if elapsed > self.slow_callback_max_ms:
                    self.slow_callback_max_ms = elapsed
        if suppress:
            return 1  # consume -- the OS does not see this X button
        try:
            return _user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)
        except BaseException:
            self.callback_errors += 1
            return 0

    # ------------------------------------------------------------------
    # Dispatcher (its own thread): delivery + watchdog
    # ------------------------------------------------------------------

    def _deliver(self, item) -> None:
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
            delivered += 1

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
        """Why the hook is gone, or '' while it is alive."""
        if not self._hook_id:
            return 'hook handle cleared'
        if self._thread is not None and not self._thread.is_alive():
            return 'hook thread ended'
        pos, count = _cursor_pos(), self.callback_count
        moved = pos is not None and self._watch_pos is not None and pos != self._watch_pos
        if moved and count == self._watch_count:
            self._dead_ticks += 1
        else:
            self._dead_ticks = 0
        self._watch_pos, self._watch_count = pos, count
        if self._dead_ticks >= DEAD_HOOK_TICKS:
            self._dead_ticks = 0
            return 'cursor moving but the hook receives nothing (removed by Windows?)'
        return ''

    def watchdog_tick(self) -> None:
        """Report counters; reinstall a lost hook; give up after too many losses."""
        self._report_counters()
        if self._stopping.is_set() or self.gave_up:
            return
        reason = self._hook_lost()
        if reason:
            self._reinstall(reason)

    def _reinstall(self, reason: str) -> None:
        now = _perf()
        while self._reinstalls and now - self._reinstalls[0] > REINSTALL_WINDOW_S:
            self._reinstalls.popleft()
        if len(self._reinstalls) >= REINSTALL_LIMIT:
            self._give_up(f"{reason}; lost {REINSTALL_LIMIT + 1} times within {REINSTALL_WINDOW_S:.0f}s")
            return
        self._reinstalls.append(now)
        logger.warning("[MOUSE HOOK] hook lost (%s) -- reinstalling (%d/%d this minute)",
                       reason, len(self._reinstalls), REINSTALL_LIMIT)
        self._stop_hook_thread()
        self._start_hook_thread()
        self._watch_pos, self._watch_count, self._dead_ticks = None, self.callback_count, 0
        if not self.installed:
            logger.error(f"[MOUSE HOOK] reinstall failed: {self.install_error or 'unknown reason'}")

    def _give_up(self, reason: str) -> None:
        self.gave_up = True
        self.suppress_buttons = frozenset()      # stop suppressing before anything else
        logger.error(f"[MOUSE HOOK] giving up: {reason}. Suppression off, hook released.")
        self._stopping.set()
        self._stop_hook_thread()
        _live_hooks.discard(self)
        if self.on_hook_failed is not None:
            try:
                self.on_hook_failed(reason)
            except Exception as e:
                logger.exception(f"[MOUSE HOOK] on_hook_failed raised: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Install the hook on a dedicated thread (Win32 requires this) and
        start the dispatcher."""
        if self._dispatcher is None or not self._dispatcher.is_alive():
            self._events = queue.SimpleQueue()   # no stale _STOP from a previous stop()
        self._stopping.clear()
        self.gave_up = False
        self._start_hook_thread()
        self._watch_due = _perf() + WATCHDOG_INTERVAL_S
        self._watch_pos, self._watch_count, self._dead_ticks = None, self.callback_count, 0
        if self._dispatcher is None or not self._dispatcher.is_alive():
            self._dispatcher = thread_registry.spawn('mouse-hook-dispatch', self._dispatch_loop, daemon=True)
        _live_hooks.add(self)

    def _start_hook_thread(self):
        self._ready.clear()
        self._thread = thread_registry.spawn('mouse-hook', self._run, daemon=True)
        self._ready.wait(timeout=2.0)

    @property
    def installed(self) -> bool:
        return bool(self._hook_id) and (self._thread is None or self._thread.is_alive())

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        try:
            self._hook_id = _set_windows_hook_ex()(WH_MOUSE_LL, self._proc, None, 0)
        except (ctypes.ArgumentError, OSError, TypeError) as e:
            self.install_error = f"SetWindowsHookExW raised: {e}"
            logger.error(f"[MOUSE HOOK] {self.install_error}")
            self._ready.set()
            return
        if not self._hook_id:
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
            # GetMessageW blocks until a message arrives; WM_QUIT breaks the loop.
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
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
        self._thread_id = None

    def stop(self):
        """Uninstall the hook, end its message loop and stop the dispatcher."""
        self._stopping.set()
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
