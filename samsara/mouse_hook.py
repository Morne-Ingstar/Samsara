"""Win32 low-level mouse hook for command-mode button suppression.

Allows per-event suppression — we can consume Mouse 4 clicks
(preventing browser-back) while passing through every other mouse event.

Modeled on the CapsLock keyboard hook used by streaming dictation.
"""

import ctypes
import ctypes.wintypes
import threading

# Win32 constants
WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP   = 0x020C
XBUTTON1 = 0x0001   # Mouse 4
XBUTTON2 = 0x0002   # Mouse 5
WM_QUIT  = 0x0012

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


class MouseHook:
    """Win32 WH_MOUSE_LL hook that optionally suppresses a single X button.

    Args:
        on_button_event: called as on_button_event(button_name, pressed) for
            every XBUTTON event. button_name is 'mouse4' or 'mouse5'.
        suppress_button: 'mouse4', 'mouse5', or None. When set, matching events
            return 1 from the hook so the OS never sees them.
    """

    def __init__(self, on_button_event, suppress_button='mouse4'):
        self.on_button_event = on_button_event
        self.suppress_button = suppress_button
        self._hook_id = None
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        # Ref must stay alive for the lifetime of the hook
        self._proc = LowLevelMouseProc(self._hook_callback)

    # ------------------------------------------------------------------
    # Hook callback (called on the hook thread by Windows)
    # ------------------------------------------------------------------

    def _hook_callback(self, n_code, w_param, l_param):
        if n_code < 0:
            return _user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)

        is_xbutton = w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP)
        if is_xbutton:
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            xbutton = (info.mouseData >> 16) & 0xFFFF
            button_name = 'mouse4' if xbutton == XBUTTON1 else 'mouse5'
            pressed = (w_param == WM_XBUTTONDOWN)

            try:
                self.on_button_event(button_name, pressed)
            except Exception as e:
                print(f"[MOUSE HOOK] callback error: {e}")

            if button_name == self.suppress_button:
                return 1  # consume — OS does not see the click

        return _user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Install the hook on a dedicated thread (Win32 requires this)."""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='mouse-hook')
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        try:
            self._hook_id = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        except (ctypes.ArgumentError, OSError) as e:
            print(f"[CMD MODE] Mouse hook failed: {e}")
            print("[CMD MODE] Mouse button command mode disabled on this system")
            self._ready.set()
            return
        if not self._hook_id:
            print("[MOUSE HOOK] SetWindowsHookExW failed")
            self._ready.set()
            return

        self._ready.set()
        print(f"[MOUSE HOOK] Hook installed (id={self._hook_id})")

        msg = ctypes.wintypes.MSG()
        # GetMessageW blocks until a message arrives; WM_QUIT breaks the loop.
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        """Uninstall the hook and terminate the message loop."""
        if self._hook_id:
            _user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None

        # Post WM_QUIT to break the blocking GetMessageW call on the hook thread
        if self._thread_id is not None:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
