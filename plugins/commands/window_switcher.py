"""Window switcher — letter-based window targeting.

Each visible/minimized window gets a frozen letter label (A, B, C...).
All commands use the "window [action]" prefix to avoid conflicts with
existing plugin commands.

Commands:
    "show windows"           - assign letters, show overlays on visible windows
    "window switch B"        - focus B, dismiss overlays
    "window bring B"         - focus B, keep overlays (z-order without losing labels)
    "window move B right"    - move B to right monitor, keep overlays
    "window mute C"          - mute C's process audio
    "window unmute C"        - unmute C's process audio
    "window close D"         - WM_CLOSE D, retire letter D
    "window copy A into B"   - focus A, Ctrl+C, focus B, Ctrl+V
    "window tile A and C"    - arrange A and C side-by-side on current monitor
    "hide windows"           - dismiss overlays, clear mapping
    "read windows"           - TTS reads full letter/title list

Phonetic and NATO alphabet accepted in all commands:
    "window switch bravo" == "window switch B"

Assignments are FROZEN once shown. No reshuffle on focus/z-order changes.
Auto-dismiss after 30 seconds. Any window command resets the 30s timer.
"""

import ctypes
import ctypes.wintypes as wintypes
import re
import struct
import sys
import threading
import time

from samsara.plugin_commands import command

from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara.ui import theme

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

user32   = None  # Kept as Windows adapter seams; resolved on first native use.
kernel32 = None
ole32    = None
_c_void  = ctypes.c_void_p


def _windows_supported():
    return sys.platform == "win32"


def _hresult_type():
    """Return the signed 32-bit Windows LONG used for HRESULT results."""
    return ctypes.c_long


def _native_library(name):
    global user32, kernel32, ole32
    if not _windows_supported():
        return None
    if name == "dwmapi":
        return ctypes.windll.dwmapi
    value = globals()[name]
    if value is None:
        value = getattr(ctypes.windll, name)
        globals()[name] = value
    return value

GWL_EXSTYLE       = -20
GW_OWNER           = 4
WS_EX_TOOLWINDOW   = 0x00000080
WS_EX_APPWINDOW    = 0x00040000
DWMWA_CLOAKED      = 14
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
SW_RESTORE        = 9
SW_SHOW           = 5
WM_CLOSE          = 0x0010

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock          = threading.Lock()
_mapping       = {}     # {"A": (hwnd, title, is_minimized), ...}
_active        = False  # overlays currently showing
_dismiss_timer = None
_app_ref       = None

_manager       = None   # _OverlayManager instance (Qt path)
_manager_lock  = threading.Lock()

_AUTO_DISMISS_S = 30.0

# ---------------------------------------------------------------------------
# Phonetic / NATO letter table
# ---------------------------------------------------------------------------

PHONETIC = {
    # Spoken letter names
    "ay":          "A", "bee":         "B", "see":         "C",
    "dee":         "D", "ee":          "E", "ef":          "F",
    "gee":         "G", "aitch":       "H", "eye":         "I",
    "jay":         "J", "kay":         "K", "el":          "L",
    "em":          "M", "en":          "N", "oh":          "O",
    "pee":         "P", "cue":         "Q", "are":         "R",
    "es":          "S", "tee":         "T", "you":         "U",
    "vee":         "V", "double you":  "W", "ex":          "X",
    "why":         "Y", "zee":         "Z", "zed":         "Z",
    # NATO alphabet
    "alpha":       "A", "bravo":       "B", "charlie":     "C",
    "delta":       "D", "echo":        "E", "foxtrot":     "F",
    "golf":        "G", "hotel":       "H", "india":       "I",
    "juliet":      "J", "kilo":        "K", "lima":        "L",
    "mike":        "M", "november":    "N", "oscar":       "O",
    "papa":        "P", "quebec":      "Q", "romeo":       "R",
    "sierra":      "S", "tango":       "T", "uniform":     "U",
    "victor":      "V", "whiskey":     "W", "x-ray":       "X",
    "yankee":      "Y", "zulu":        "Z",
}

# ---------------------------------------------------------------------------
# Letter parsing
# ---------------------------------------------------------------------------

def _parse_letters(text: str) -> list:
    """Extract window letters from command text in order of appearance.

    Handles NATO phonetics ("bravo"), spoken letter names ("bee"),
    and raw single letters ("B" or "b").
    """
    t = text.lower().strip()
    found = []

    # Longest-first so "double you" beats "you", "x-ray" beats "are"
    for phrase, letter in sorted(PHONETIC.items(), key=lambda x: -len(x[0])):
        pattern = r'\b' + re.escape(phrase) + r'\b'
        m = re.search(pattern, t)
        if m:
            found.append((m.start(), letter))
            # Blank out so single-letter scan below doesn't double-count
            t = t[:m.start()] + ' ' * len(phrase) + t[m.end():]

    # Single standalone letters remaining after phonetic extraction
    for m in re.finditer(r'\b([a-z])\b', t):
        found.append((m.start(), m.group(1).upper()))

    found.sort(key=lambda x: x[0])
    return [letter for _, letter in found]


def _parse_monitor_index(text: str):
    """Return a 1-based monitor index from text, or None."""
    t = text.lower()
    # 820b: directions are never monitor identities. Main is resolved against
    # native primary metadata by overview_monitor_target, not a spatial index.
    from samsara.spoken_numbers import spoken_number_matches
    return next((number for _start, _end, number in spoken_number_matches(t)), None)

# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------

_SKIP_TITLES = frozenset({
    'Program Manager', 'Windows Shell Experience Host',
    'Microsoft Text Input Application',
})


def _alt_tab_eligible(window: dict) -> bool:
    """Whether a window meets the shell's application-window convention.

    This is deliberately structural rather than an executable-name blocklist:
    visible/minimized titled top-level windows survive, except DWM-cloaked
    windows, tool windows, and ordinary owned dialogs.  APPWINDOW explicitly
    opts a tool/owned window back in, matching the Win32 task-switcher
    convention.
    """
    if not window.get("visible") and not window.get("minimized"):
        return False
    if not window.get("title") or window.get("cloaked"):
        return False
    exstyle = int(window.get("exstyle", 0))
    appwindow = bool(exstyle & WS_EX_APPWINDOW)
    if exstyle & WS_EX_TOOLWINDOW and not appwindow:
        return False
    if window.get("owner") and not appwindow:
        return False
    return True


def _is_cloaked(hwnd) -> bool:
    """True if DWM says the top-level window is not currently presentable."""
    if not _windows_supported():
        return False
    try:
        value = wintypes.DWORD(0)
        result = _native_library("dwmapi").DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return result == 0 and bool(value.value)
    except Exception:
        # Older Windows / unavailable DWM: retain the visible-window fallback.
        return False

def _own_hwnd(app) -> set:
    """Return hwnds to exclude from window listing.

    Samsara's Qt windows are already filtered by title prefix in
    _get_all_windows ('if title.startswith("Samsara"): return True'),
    so no hwnd exclusion is needed here.
    """
    return set()


def _get_all_windows(exclude_hwnds: set = None) -> list:
    """Enumerate all visible and minimized application windows.

    Returns list of (hwnd, title, RECT, is_minimized).
    """
    if not _windows_supported():
        return []
    global user32
    user32 = _native_library("user32")
    exclude = exclude_hwnds or set()
    results = []

    def _cb(hwnd, _):
        if hwnd in exclude:
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        if title_len == 0:
            return True

        buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, buf, title_len + 1)
        title = buf.value
        if not title or title in _SKIP_TITLES:
            return True
        if title.startswith('Samsara'):
            return True

        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        descriptor = {
            "visible": visible,
            "minimized": minimized,
            "title": title,
            "owner": int(user32.GetWindow(hwnd, GW_OWNER) or 0),
            "exstyle": int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)),
            "cloaked": _is_cloaked(hwnd),
        }
        if not _alt_tab_eligible(descriptor):
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        if not minimized:
            w = rect.right  - rect.left
            h = rect.bottom - rect.top
            if w <= 50 or h <= 50:
                return True

        results.append((hwnd, title, rect, minimized))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _native_library("user32").EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def _assign_letters(windows: list) -> dict:
    """Assign letters to windows.

    Visible first, sorted left-to-right (rect.left, then rect.top).
    Minimized after, sorted alphabetically by title.

    Returns {"A": (hwnd, title, is_minimized), ...}
    """
    visible   = [(h, t, r, False) for h, t, r, m in windows if not m]
    minimized = [(h, t, r, True)  for h, t, r, m in windows if m]
    visible.sort(key=lambda x: (x[2].left, x[2].top))
    minimized.sort(key=lambda x: x[1].lower())

    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    mapping  = {}
    for i, (hwnd, title, rect, is_min) in enumerate(visible + minimized):
        if i < 26:
            letter = alphabet[i]
        else:
            # AA, AB, AC ... for the rare case of 27+ windows
            q, r = divmod(i - 26, 26)
            letter = alphabet[min(q, 25)] + alphabet[r]
        mapping[letter] = (hwnd, title, is_min)
    return mapping

# ---------------------------------------------------------------------------
# Overlay management (all functions must be called on the main thread)
# ---------------------------------------------------------------------------

def _set_click_through(win_id: int) -> None:
    """Add WS_EX_LAYERED | WS_EX_TRANSPARENT so clicks pass through."""
    if not _windows_supported():
        return
    try:
        api = _native_library("user32")
        style = api.GetWindowLongW(win_id, GWL_EXSTYLE)
        api.SetWindowLongW(
            win_id, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception as e:
        print(f"[WINSW] click-through failed: {e}")


def _make_overlay_manager_class():
    """Return the _OverlayManager class, importing PySide6 lazily."""
    from PySide6.QtCore import QObject, Signal, Slot, Qt
    from PySide6.QtWidgets import QApplication, QWidget, QLabel
    from PySide6.QtGui import QFont

    class _OverlayManager(QObject):
        """Owns Qt overlay widgets. Always lives on the Qt event-loop thread.

        Signals are emitted from any thread; Qt routes them via QueuedConnection
        when sender and receiver are on different threads.
        """
        _show_sig = Signal(object)   # payload: mapping dict
        _hide_sig = Signal()

        def __init__(self):
            super().__init__()
            self._widgets = []
            self._show_sig.connect(self._do_show)
            self._hide_sig.connect(self._do_hide)

        @Slot(object)
        def _do_show(self, mapping):
            for w in self._widgets:
                w.deleteLater()
            self._widgets.clear()

            qt_app = QApplication.instance()
            if qt_app is None:
                return
            dpr = qt_app.devicePixelRatio()

            for letter, (hwnd, title, is_min) in sorted(mapping.items()):
                if is_min:
                    continue
                try:
                    rect = wintypes.RECT()
                    _native_library("user32").GetWindowRect(hwnd, ctypes.byref(rect))

                    pill = 54
                    # Convert physical pixels -> logical pixels for Qt
                    cx = int(rect.left / dpr) + 8
                    cy = int(rect.top  / dpr) + 8

                    win = QWidget(
                        None,
                        Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
                    )
                    win.setAttribute(Qt.WA_TranslucentBackground)
                    win.setFixedSize(pill, pill)
                    win.move(cx, cy)

                    lbl = QLabel(letter, win)
                    lbl.setAlignment(Qt.AlignCenter)
                    lbl.setFont(theme.qfont(theme.TYPE_HERO, weight=QFont.Bold))
                    lbl.setStyleSheet(
                        f"color: {theme.TEXT_PRIMARY};"
                        f" background-color: {theme.tint(theme.BG0, 0.88)};"
                        " border-radius: 8px;"
                    )
                    lbl.setFixedSize(pill, pill)

                    win.setWindowOpacity(0.88)
                    win.show()
                    self._widgets.append(win)
                except Exception as e:
                    print(f"[WINSW] Qt overlay {letter} ({title[:30]}): {e}")

        @Slot()
        def _do_hide(self):
            for w in self._widgets:
                w.deleteLater()
            self._widgets.clear()

        def request_show(self, mapping: dict):
            self._show_sig.emit(mapping)

        def request_hide(self):
            self._hide_sig.emit()

    return _OverlayManager


def _get_manager():
    """Return the singleton _OverlayManager, creating it if needed.

    The manager is pinned to the Qt event-loop thread via moveToThread so that
    its slots always run there regardless of which thread calls request_show/hide.
    Returns None when PySide6 is unavailable or Qt has not started yet.
    """
    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is not None:
            return _manager
        try:
            from PySide6.QtWidgets import QApplication
            qt_app = QApplication.instance()
            if qt_app is None:
                return None
            cls = _make_overlay_manager_class()
            mgr = cls()
            mgr.moveToThread(qt_app.thread())
            _manager = mgr
        except Exception as e:
            print(f"[WINSW] overlay manager init failed: {e}")
    return _manager


def _dismiss_all(clear_mapping: bool = True) -> None:
    """Dismiss overlays, optionally clear mapping. Thread-safe."""
    global _active, _mapping
    mgr = _manager
    if mgr is not None:
        mgr.request_hide()
    _active = False
    if clear_mapping:
        with _lock:
            _mapping = {}
    _cancel_timer()

# ---------------------------------------------------------------------------
# Timer management
# ---------------------------------------------------------------------------

def _cancel_timer() -> None:
    global _dismiss_timer
    if _dismiss_timer is not None:
        try:
            _dismiss_timer.cancel()
        except Exception as e:
            logger.debug(f"_cancel_timer: {e}")
        _dismiss_timer = None


def _reset_timer() -> None:
    """Cancel the existing dismiss timer and start a fresh 30-second one."""
    global _dismiss_timer
    _cancel_timer()
    app = _app_ref
    if app is None:
        return

    def _fire():
        try:
            _dismiss_all(clear_mapping=True)
            print("[WINSW] Auto-dismissed after 30s inactivity")
        except Exception as e:
            logger.debug(f"_fire: {e}")

    t = thread_registry.timer("window_switcher.auto_dismiss", _AUTO_DISMISS_S, _fire, daemon=True)
    _dismiss_timer = t

# ---------------------------------------------------------------------------
# Focus helper
# ---------------------------------------------------------------------------

def _force_focus(hwnd: int) -> bool:
    """Delegate focus activation to the shared lower-level window helper."""
    if not _windows_supported():
        return False
    from plugins.commands.windows import raise_window
    return raise_window(hwnd, activate=True)

# ---------------------------------------------------------------------------
# Monitor helpers (delegates to windows.py)
# ---------------------------------------------------------------------------

def _get_monitors() -> list:
    if not _windows_supported():
        return []
    try:
        from plugins.commands.windows import get_monitors
        return get_monitors()
    except Exception:
        return []


def _move_window(hwnd: int, monitor: dict) -> None:
    if not _windows_supported():
        return
    try:
        from plugins.commands.windows import move_window_to_monitor
        move_window_to_monitor(hwnd, monitor)
    except Exception as e:
        print(f"[WINSW] move_window_to_monitor failed: {e}")

# ---------------------------------------------------------------------------
# Per-process audio mute — Core Audio COM vtable
# ---------------------------------------------------------------------------

def _guid(s: str) -> bytes:
    parts = s.strip('{}').split('-')
    return struct.pack('<IHH',
                       int(parts[0], 16),
                       int(parts[1], 16),
                       int(parts[2], 16),
                       ) + bytes.fromhex(parts[3] + parts[4])


_CLSID_MMDevEnum   = _guid('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
_IID_IMMDevEnum    = _guid('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
_IID_IAudSessMgr2  = _guid('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
_IID_IAudSessCtrl2 = _guid('{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}')
_IID_ISimpleAudVol = _guid('{87CE5498-68D6-44E5-9215-6DA47EF883D8}')
_CLSCTX_ALL        = 23


def _vtfunc(ptr, idx: int, restype, *argtypes):
    """Retrieve and cast a COM vtable function by slot index."""
    vtable  = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(_c_void)))
    fn_ptr  = vtable[0][idx]
    ftype   = ctypes.WINFUNCTYPE(restype, *argtypes)
    return ftype(fn_ptr)


def _get_pid(hwnd: int) -> int:
    if not _windows_supported():
        return 0
    user_api = _native_library("user32")
    pid = wintypes.DWORD(0)
    user_api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _set_process_mute(hwnd: int, mute: bool) -> bool:
    """Mute/unmute all Core Audio sessions belonging to hwnd's PID."""
    if not _windows_supported():
        return False
    global ole32
    ole32 = _native_library("ole32")
    target_pid = _get_pid(hwnd)
    if not target_pid:
        return False

    hr = ole32.CoInitialize(None)
    if hr < 0 and hr != 1:   # S_OK or S_FALSE (already initialised)
        return False

    try:
        # 1. IMMDeviceEnumerator
        enum = _c_void()
        hr = ole32.CoCreateInstance(
            _CLSID_MMDevEnum, None, _CLSCTX_ALL,
            _IID_IMMDevEnum, ctypes.byref(enum))
        if hr != 0 or not enum:
            return False

        # 2. IMMDevice: GetDefaultAudioEndpoint(eRender=0, eMultimedia=1) - slot 4
        get_default = _vtfunc(enum, 4, _hresult_type(),
                               _c_void, ctypes.c_uint, ctypes.c_uint,
                               ctypes.POINTER(_c_void))
        device = _c_void()
        hr = get_default(enum, 0, 1, ctypes.byref(device))
        _vtfunc(enum, 2, ctypes.c_ulong, _c_void)(enum)   # Release
        if hr != 0 or not device:
            return False

        # 3. IAudioSessionManager2: IMMDevice::Activate - slot 3
        activate = _vtfunc(device, 3, _hresult_type(),
                            _c_void, _c_void, ctypes.c_ulong,
                            _c_void, ctypes.POINTER(_c_void))
        mgr2 = _c_void()
        hr = activate(device, _IID_IAudSessMgr2, _CLSCTX_ALL, None,
                      ctypes.byref(mgr2))
        _vtfunc(device, 2, ctypes.c_ulong, _c_void)(device)  # Release
        if hr != 0 or not mgr2:
            return False

        # 4. IAudioSessionEnumerator: GetSessionEnumerator - slot 5
        get_sess_enum = _vtfunc(mgr2, 5, _hresult_type(),
                                 _c_void, ctypes.POINTER(_c_void))
        sess_enum = _c_void()
        hr = get_sess_enum(mgr2, ctypes.byref(sess_enum))
        _vtfunc(mgr2, 2, ctypes.c_ulong, _c_void)(mgr2)   # Release
        if hr != 0 or not sess_enum:
            return False

        # 5. GetCount - slot 3
        get_count = _vtfunc(sess_enum, 3, _hresult_type(),
                             _c_void, ctypes.POINTER(ctypes.c_int))
        count = ctypes.c_int(0)
        get_count(sess_enum, ctypes.byref(count))

        # 6. GetSession - slot 4
        get_session = _vtfunc(sess_enum, 4, _hresult_type(),
                               _c_void, ctypes.c_int,
                               ctypes.POINTER(_c_void))

        matched = 0
        for i in range(count.value):
            ctrl = _c_void()
            if get_session(sess_enum, i, ctypes.byref(ctrl)) != 0 or not ctrl:
                continue

            # 7. QI ctrl → IAudioSessionControl2
            qi = _vtfunc(ctrl, 0, _hresult_type(),
                         _c_void, _c_void, ctypes.POINTER(_c_void))
            ctrl2 = _c_void()
            hr = qi(ctrl, _IID_IAudSessCtrl2, ctypes.byref(ctrl2))
            _vtfunc(ctrl, 2, ctypes.c_ulong, _c_void)(ctrl)  # Release ctrl

            if hr != 0 or not ctrl2:
                continue

            # 8. GetProcessId - slot 14
            get_pid_f = _vtfunc(ctrl2, 14, _hresult_type(),
                                 _c_void, ctypes.POINTER(wintypes.DWORD))
            sess_pid = wintypes.DWORD(0)
            get_pid_f(ctrl2, ctypes.byref(sess_pid))

            if sess_pid.value == target_pid:
                # 9. QI ctrl2 → ISimpleAudioVolume
                qi2 = _vtfunc(ctrl2, 0, _hresult_type(),
                               _c_void, _c_void, ctypes.POINTER(_c_void))
                sav = _c_void()
                if qi2(ctrl2, _IID_ISimpleAudVol, ctypes.byref(sav)) == 0 and sav:
                    # SetMute - slot 5
                    set_mute = _vtfunc(sav, 5, _hresult_type(),
                                       _c_void, wintypes.BOOL, _c_void)
                    set_mute(sav, wintypes.BOOL(1 if mute else 0), None)
                    _vtfunc(sav, 2, ctypes.c_ulong, _c_void)(sav)  # Release
                    matched += 1

            _vtfunc(ctrl2, 2, ctypes.c_ulong, _c_void)(ctrl2)  # Release

        _vtfunc(sess_enum, 2, ctypes.c_ulong, _c_void)(sess_enum)  # Release
        return matched > 0

    except Exception as e:
        print(f"[WINSW] audio COM error: {e}")
        return False

# ---------------------------------------------------------------------------
# TTS helper
# ---------------------------------------------------------------------------

def _speak(app, text: str) -> None:
    try:
        from plugins.commands.ask_ollama import speak as _tts
        _tts(app, text)
    except Exception:
        print(f"[WINSW] {text}")

# ---------------------------------------------------------------------------
# Mapping lookup helper
# ---------------------------------------------------------------------------

def _resolve(app, letter: str):
    """Return (hwnd, title, is_min) for letter, or None with spoken error."""
    letter = letter.upper()
    with _lock:
        entry = _mapping.get(letter)
    if entry is None:
        _speak(app, f"No window labeled {letter}.")
        return None
    return entry


def get_window_by_letter(letter: str):
    """Return the HWND for the given letter, or None if not assigned.

    No side effects — does not speak an error. Used by vision bridge
    and other callers that handle the missing-window case themselves.
    """
    letter = letter.upper()
    with _lock:
        entry = _mapping.get(letter)
    return entry[0] if entry else None

# ---------------------------------------------------------------------------
# "show windows" — entry point
# ---------------------------------------------------------------------------

@command("show windows",
         aliases=["label windows", "window labels", "window overview", "show all windows"],
         pack="window-management", risk_class="read")
def handle_show_windows(app, remainder):
    """Shows live pictures, numbers and layout buttons for your open windows."""
    if not _windows_supported():
        return False
    return _show_overview(app, remainder)

# ---------------------------------------------------------------------------
# "window switch" — focus, dismiss overlays
# ---------------------------------------------------------------------------

@command("window switch",
         aliases=["window focus", "switch to window", "go to window"],
         pack="window-management",
         risk_class="ui", param_schema={"label": {"type": "nato_letter", "required": False}},
)
def handle_window_switch(app, remainder):
    """Brings the window with the letter you name to the front."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window switch B.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    _force_focus(hwnd)
    _dismiss_all(clear_mapping=False)
    _cancel_timer()
    print(f"[WINSW] Switched to: {title}")
    return True

# ---------------------------------------------------------------------------
# "window bring" — focus without dismissing
# ---------------------------------------------------------------------------

@command("window bring",
         aliases=["bring window", "bring forward"],
         pack="window-management",
         risk_class="ui", param_schema={"label": {"type": "nato_letter", "required": False}},
)
def handle_window_bring(app, remainder):
    """Moves the window with the letter you name to the screen you are on."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window bring B.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    _force_focus(hwnd)
    _reset_timer()
    print(f"[WINSW] Brought forward: {title}")
    return True

# ---------------------------------------------------------------------------
# "window move" — reposition to a monitor
# ---------------------------------------------------------------------------

@command("window move",
         aliases=["move window"],
         pack="window-management",
         risk_class="ui", param_schema={"label": {"type": "nato_letter", "required": False}, "monitor": {"type": "monitor", "required": False}},
)
def handle_window_move(app, remainder):
    """Moves the window with the letter you name to another screen."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window move B to monitor 2.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    monitors = _get_monitors()
    if not monitors:
        _speak(app, "No monitors found.")
        return True

    idx = _parse_monitor_index(remainder or '')
    if idx is None:
        # Default: next monitor relative to current window position
        rect = wintypes.RECT()
        _native_library("user32").GetWindowRect(hwnd, ctypes.byref(rect))
        cx = (rect.left + rect.right) // 2
        current_idx = 1
        for m in monitors:
            ml, mt, mr, mb = m['rect']
            if ml <= cx < mr:
                current_idx = m['index']
                break
        # Pick a different monitor
        others = [m for m in monitors if m['index'] != current_idx]
        target = others[0] if others else monitors[0]
    else:
        target = next((m for m in monitors if m['index'] == idx), None)
        if target is None:
            _speak(app, f"No monitor {idx}.")
            return True

    _move_window(hwnd, target)
    _reset_timer()
    print(f"[WINSW] Moved '{title}' to monitor {target.get('index', '?')}")
    return True

# ---------------------------------------------------------------------------
# "window mute" / "window unmute"
# ---------------------------------------------------------------------------

@command("window mute",
         aliases=["mute window"],
         pack="window-management",
         risk_class="ui", param_schema={"label": {"type": "nato_letter", "required": False}},
)
def handle_window_mute(app, remainder):
    """Mutes the audio coming from the window with the letter you name."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window mute C.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    ok = _set_process_mute(hwnd, True)
    print(f"[WINSW] {'Muted' if ok else 'Mute failed for'}: {title}")
    _reset_timer()
    return True


@command("window unmute",
         aliases=["unmute window"],
         pack="window-management",
         risk_class="ui", param_schema={"label": {"type": "nato_letter", "required": False}},
)
def handle_window_unmute(app, remainder):
    """Unmutes the audio from the window with the letter you name."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window unmute C.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    ok = _set_process_mute(hwnd, False)
    print(f"[WINSW] {'Unmuted' if ok else 'Unmute failed for'}: {title}")
    _reset_timer()
    return True

# ---------------------------------------------------------------------------
# "window close"
# ---------------------------------------------------------------------------

@command("window close",
         pack="window-management",
         risk_class="write", param_schema={"label": {"type": "nato_letter", "required": False}},
)
def handle_window_close(app, remainder):
    """Closes the window with the letter you name, asking it to save first."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window close D.")
        return True

    letter = letters[0].upper()
    entry  = _resolve(app, letter)
    if entry is None:
        return True

    hwnd, title, _ = entry
    _native_library("user32").PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # Retire the letter — never reassign within this session
    with _lock:
        _mapping.pop(letter, None)

    _reset_timer()
    print(f"[WINSW] Closed: {title} (letter {letter} retired)")
    return True

# ---------------------------------------------------------------------------
# "window copy"
# ---------------------------------------------------------------------------

@command("window copy",
         aliases=["copy from window", "copy from"],
         pack="window-management",
         risk_class="write", param_schema={"labels": {"type": "nato_letter", "required": False}},
)
def handle_window_copy(app, remainder):
    """Copies the text from one lettered window into another."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if len(letters) < 2:
        _speak(app, "Need two windows. Say window copy A into B.")
        return True

    src_entry = _resolve(app, letters[0])
    dst_entry = _resolve(app, letters[1])
    if src_entry is None or dst_entry is None:
        return True

    src_hwnd, src_title, _ = src_entry
    dst_hwnd, dst_title, _ = dst_entry

    try:
        import win32api
        import win32con
        VK_CONTROL = 0x11
        VK_C       = 0x43
        VK_V       = 0x56

        _force_focus(src_hwnd)
        time.sleep(0.2)
        win32api.keybd_event(VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_C, 0, 0, 0)
        win32api.keybd_event(VK_C, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

        time.sleep(0.3)

        _force_focus(dst_hwnd)
        time.sleep(0.2)
        win32api.keybd_event(VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_V, 0, 0, 0)
        win32api.keybd_event(VK_V, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    except Exception as e:
        print(f"[WINSW] copy failed: {e}")
        return True

    _reset_timer()
    print(f"[WINSW] Copied from '{src_title}' -> '{dst_title}'")
    return True

# ---------------------------------------------------------------------------
# "window tile"
# ---------------------------------------------------------------------------

@command("window tile",
         aliases=["tile windows"],
         pack="window-management",
         risk_class="ui", param_schema={"labels": {"type": "nato_letter", "required": False}},
)
def handle_window_tile(app, remainder):
    """Arranges the lettered windows you name side by side."""
    if not _windows_supported():
        return False
    letters = _parse_letters(remainder or '')
    if len(letters) < 2:
        _speak(app, "Need at least 2 windows. Say window tile A and C.")
        return True

    entries = []
    for l in letters:
        e = _resolve(app, l)
        if e is None:
            return True
        entries.append((l, e))

    # Get current monitor
    try:
        from plugins.commands.windows import get_monitors, get_monitor_under_cursor
        monitors = get_monitors()
        monitor  = get_monitor_under_cursor(monitors)
    except Exception:
        try:
            import win32api
            w = win32api.GetSystemMetrics(0)
            h = win32api.GetSystemMetrics(1)
            monitor = {'rect': (0, 0, w, h), 'width': w, 'height': h}
        except Exception:
            monitor = {'rect': (0, 0, 1920, 1080), 'width': 1920, 'height': 1080}

    ml, mt, mr, mb = monitor['rect']
    total_w = mr - ml
    total_h = mb - mt
    n       = len(entries)
    slot_w  = total_w // n

    try:
        import win32gui
        import win32con
        HWND_TOP      = 0
        SWP_SHOWWINDOW = 0x0040

        for i, (letter, (hwnd, title, is_min)) in enumerate(entries):
            x = ml + i * slot_w
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, HWND_TOP, x, mt, slot_w, total_h,
                                  SWP_SHOWWINDOW)
            print(f"[WINSW] Tiled {letter} ({title[:25]}) "
                  f"at ({x},{mt}) {slot_w}x{total_h}")
    except Exception as e:
        print(f"[WINSW] tile failed: {e}")

    _reset_timer()
    return True

# ---------------------------------------------------------------------------
# "hide windows"
# ---------------------------------------------------------------------------

@command("hide windows",
         aliases=["hide window labels", "dismiss windows"],
         pack="window-management",
         risk_class="ui",
)
def handle_hide_windows(app, remainder):
    """Closes the window overview and clears legacy window labels."""
    if not _windows_supported():
        return False
    global _app_ref
    _app_ref = app
    _hide_overview()
    _dismiss_all(clear_mapping=True)
    print("[WINSW] Dismissed window labels")
    return True

# ---------------------------------------------------------------------------
# "read windows"
# ---------------------------------------------------------------------------

@command("read windows",
         aliases=["list windows", "what windows", "list all windows"],
         pack="window-management",
         risk_class="read",
)
def handle_read_windows(app, remainder):
    """Shows the overview and reads one short app name per breath."""
    if not _windows_supported():
        return False
    if not overview_active(app):
        if not _show_overview(app, remainder):
            return False
    global _overview_reader
    _cancel_overview_read()
    chunks = overview_read_chunks(_overview_rows)
    coordinator = getattr(app, 'audio_coordinator', None)
    if coordinator is None:
        # No audio runtime (e.g. headless callers): bounded text-only fallback.
        for chunk in chunks:
            _speak(app, chunk)
        return True
    from samsara.window_overview_flow import ReadSequence
    reader = ReadSequence(coordinator,
        [(row['number'], chunk) for row, chunk in zip(_overview_rows, chunks)],
        lambda number: _overview_panel().focus_reading(number)
            if _overview_reader is reader else None)
    _overview_reader = reader
    reader.start()
    return True


def _read_lettered_windows(app):
    """Legacy letter readback retained for callers needing letter labels."""
    global _app_ref
    _app_ref = app

    with _lock:
        mapping = dict(_mapping)

    if not mapping:
        # Fresh enumeration without creating overlays
        exclude = _own_hwnd(app)
        windows  = _get_all_windows(exclude)
        mapping  = _assign_letters(windows)

    if not mapping:
        _speak(app, "No windows found.")
        return True

    parts = []
    for letter, (hwnd, title, is_min) in sorted(mapping.items()):
        short = (title.rsplit(' - ', 1)[-1] if ' - ' in title else title)[:40]
        suffix = ", minimized" if is_min else ""
        parts.append(f"{letter}: {short}{suffix}")

    _speak(app, ". ".join(parts) + ".")
    return True


# Queue 820: one frozen snapshot owns thumbnails, speech and follow-up actions.
_overview_rows = []
_overview_app = None
_overview_visible = False
_overview_keep = False
_overview_pending_close = None
_overview_reader = None


def overview_active(app=None):
    return _overview_visible and (app is None or app is _overview_app)


def short_window_title(title, app_name="", limit=40):
    text = " ".join(str(title or "").split())
    for separator in (" - ", " — ", " – "):
        if app_name and text.casefold().endswith((separator + app_name).casefold()):
            text = text[:-(len(separator) + len(app_name))]
            break
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def order_overview_windows(windows, monitors, monitor_for_window):
    """Monitor left-to-right, preserving EnumWindows' z-order within each one."""
    ordered_monitors = sorted(monitors, key=lambda m: (m['rect'][0], m['rect'][1]))
    def rank(window):
        monitor = monitor_for_window(window[0], monitors)
        return next((i for i, item in enumerate(ordered_monitors) if item == monitor),
                    len(ordered_monitors))
    return sorted(windows, key=rank)


def overview_read_chunks(rows):
    chunks = []
    for row in rows:
        app_name = re.sub(r'^(?:Google|Microsoft)\s+', '', row['app'], flags=re.I)
        chunks.append(f"{row['number']}, {app_name}")
    return chunks


def _cancel_overview_read():
    global _overview_reader
    reader, _overview_reader = _overview_reader, None
    if reader is not None:
        reader.cancel()
        _overview_panel().focus_reading(None)


def overview_command_input(app, text, *, replies=False):
    """Dispatcher seam: interrupt readback before any new command or reply."""
    global _overview_pending_close
    if not overview_active(app):
        return False
    _cancel_overview_read()
    normalized = ' '.join(str(text or '').lower().strip(' .!?').split())
    if _overview_pending_close:
        if normalized in {'yes', 'confirm', 'confirm close'}:
            if replies:
                overview_action(app, 0, 'confirm close')
                return True
            return False
        _overview_pending_close = None
        _overview_panel().confirmation('')
        if replies and normalized in {'no', 'cancel'}:
            return True
    if replies and normalized == 'stop':
        return True
    return False


def _overview_panel():
    from samsara.ui.window_overview_qt import get_panel
    return get_panel()


def _overview_monitor(hwnd, monitors):
    # MonitorFromWindow uses the pre-minimize rectangle; GetWindowRect does not.
    if not monitors:
        return None
    try:
        import win32api
        handle = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        monitor = next((m for m in monitors if m.get('handle') == handle), None)
        if monitor is not None:
            return monitor
    except Exception:
        logger.debug("Window overview monitor lookup failed", exc_info=True)
    from plugins.commands.windows import _get_monitor_for_window
    return _get_monitor_for_window(hwnd, monitors)


def _overview_closed():
    global _overview_visible, _overview_pending_close
    _overview_visible = False
    _overview_pending_close = None
    _cancel_overview_read()


def _hide_overview():
    was_visible = _overview_visible
    _overview_closed()
    if was_visible:
        _overview_panel().hide()


def _show_overview(app, remainder=""):
    global _overview_rows, _overview_app, _overview_visible, _overview_keep
    global _overview_pending_close, _mapping
    from plugins.commands import windows as mover, window_cube as cube
    _cancel_overview_read()
    started = time.perf_counter()
    windows = _get_all_windows(_own_hwnd(app))
    monitors = mover.get_monitors()
    ordered = order_overview_windows(windows, monitors, _overview_monitor)
    names = {}
    rows = []
    for number, (hwnd, title, _rect, minimized) in enumerate(ordered, 1):
        pid = _get_pid(hwnd)
        if pid not in names:
            names[pid] = cube._friendly_app_name(hwnd)
        app_name = names[pid]
        monitor = _overview_monitor(hwnd, monitors)
        rows.append(dict(number=number, hwnd=hwnd, pid=pid, title=title,
                         short_title=short_window_title(title, app_name),
                         app=app_name, minimized=minimized, monitor_count=len(monitors),
                         monitor=overview_monitor_number(monitor),
                         primary=bool(monitor and monitor.get('primary'))))
    # Legacy letter commands can still refer to the same enumerated windows.
    with _lock:
        _mapping = _assign_letters(windows)
    _cancel_timer()
    if _manager is not None:
        _manager.request_hide()
    _overview_rows, _overview_app = rows, app
    _overview_pending_close = None
    config = getattr(app, 'config', {}) or {}
    _overview_keep = bool(config.get('window_overview', {}).get('keep_open', False))
    if 'keep it open' in (remainder or '').lower():
        _overview_keep = True
    _overview_visible = True
    try:
        _overview_panel().show(rows,
            lambda number, action, keep: overview_action(app, number, action, keep),
            _overview_closed, _overview_keep,
            listening_indicator=getattr(app, 'listening_indicator', None))
    except Exception:
        _overview_closed()
        logger.exception("Window overview could not open")
        _speak(app, "Could not open the window overview.")
        return False
    logger.info("Window overview snapshot: windows=%d elapsed_ms=%.1f",
                len(rows), (time.perf_counter() - started) * 1000)
    return True


_OVERVIEW_ACTIONS = frozenset({"go", "left", "right", "top left", "top right",
    "bottom left", "bottom right", "maximize", "minimize", "next monitor", "cube", "close"})


def parse_overview_action(text):
    from samsara.spoken_numbers import parse_spoken_number
    text = " ".join((text or "").lower().strip(' .,!?').split())
    keep = text.endswith('keep it open')
    if keep:
        text = text[:-len('keep it open')].strip(' ,')
    if text.startswith('close '):
        number = parse_spoken_number(text[6:])
        return (number, 'close', keep) if number and number > 0 else None
    target = re.fullmatch(r'(.+?) (to|(?:left|right|top left|top right|bottom left|bottom right|maximize) on) (.+)', text)
    if target:
        number = parse_spoken_number(target[1])
        monitor = target[3]
        if (number and number > 0 and
                (parse_spoken_number(monitor) or monitor in {'main', 'primary'})):
            return number, f'{target[2]} {monitor}', keep
        return None
    for action in sorted(_OVERVIEW_ACTIONS - {'close'}, key=len, reverse=True):
        if text.endswith(' ' + action):
            number = parse_spoken_number(text[:-len(action)].strip())
            return (number, action, keep) if number and number > 0 else None
    number = parse_spoken_number(text)
    return (number, 'go', keep) if number and number > 0 else None


def _overview_is_live(row):
    import win32gui
    # Keep the previous choose safety check when a document changes in its slot.
    return bool(win32gui.IsWindow(row['hwnd']) and _get_pid(row['hwnd']) == row['pid']
                and win32gui.GetWindowText(row['hwnd']) == row['title'])


def overview_action(app, number, action='go', keep=None):
    if not _windows_supported():
        return False
    global _overview_pending_close, _overview_keep
    if not overview_active(app):
        return False
    started = time.perf_counter()
    _cancel_overview_read()
    if keep is not None:
        _overview_keep = keep
        _overview_panel().keep_open(keep)
    if action == 'keep open':
        _overview_pending_close = None
        _overview_panel().confirmation('')
        return True
    if action == 'cancel close':
        _overview_pending_close = None
        _overview_panel().confirmation('')
        return True
    if action == 'confirm close':
        pending = _overview_pending_close
        _overview_pending_close = None
        if not pending or time.monotonic() > pending[1]:
            _overview_panel().confirmation("")
            _speak(app, "Close confirmation expired. Say close and the window number again.")
            return True
        number, action = pending[0], 'confirmed close'
        _overview_panel().confirmation('')
    else:
        _overview_pending_close = None
        _overview_panel().confirmation("")
    row = next((r for r in _overview_rows if r['number'] == number), None)
    if row is None or not _overview_is_live(row):
        _speak(app, "That window is no longer available. Say show windows again.")
        return True
    if action == 'select':
        _overview_panel().select(number)
        return True
    if action == 'close':
        _overview_pending_close = (number, time.monotonic() + 15)
        # PLACEHOLDER COPY (820b)
        text = f"Close {number}: {row['app']}? Say yes or cancel."
        _overview_panel().confirmation(text)
        coordinator = getattr(app, 'audio_coordinator', None)
        if coordinator is not None:
            coordinator.cancel_speech()  # do not append behind an old readback
            handle = coordinator.speak('Confirm', category='confirmation', queue_mode='append')
            from samsara.window_overview_flow import observe_audio_start
            observe_audio_start(handle, started)
        else:
            _speak(app, 'Confirm')
        logger.info('Overview close prompt submitted: elapsed_ms=%.1f',
                    (time.perf_counter() - started) * 1000)
        return True
    from plugins.commands import windows as mover
    import win32gui
    import win32con
    hwnd = row['hwnd']
    ready = lambda: _overview_is_live(row)
    success = True
    try:
        target = None
        explicit = re.fullmatch(r'(to|(.+) on) (.+)', action)
        if explicit:
            monitors = mover.get_monitors()
            target = overview_monitor_target(explicit[3], monitors)
            if target is None:
                _speak(app, 'No matching monitor is available.')  # PLACEHOLDER COPY (820b)
                return True
            action = explicit[2] or 'to monitor'
        if action == 'go':
            success = _force_focus(hwnd)
        elif action in {'left', 'right', 'top left', 'top right', 'bottom left', 'bottom right'}:
            # Capture MonitorFromWindow before snap_window restores the window;
            # its restored rectangle may belong to a different monitor.
            target = target or _overview_monitor(hwnd, mover.get_monitors())
            success = bool(target) and mover.snap_window(hwnd, action, preflight=ready,
                                                        target_monitor=target)
        elif action in {'maximize', 'minimize'}:
            if target is not None:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                if not ready() or not mover.move_window_to_monitor(hwnd, target, preflight=ready):
                    return True
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE if action == 'maximize' else win32con.SW_MINIMIZE)
        elif action == 'to monitor':
            if not ready():
                return True
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            success = mover.move_window_to_monitor(hwnd, target, preflight=ready)
        elif action == 'next monitor':
            monitors = mover.get_monitors()
            current = _overview_monitor(hwnd, monitors)
            if len(monitors) < 2 or current not in monitors:
                _speak(app, "No next monitor is available.")
                return True
            target = monitors[(monitors.index(current) + 1) % len(monitors)]
            success = mover.move_window_to_monitor(hwnd, target, preflight=ready)
        elif action == 'cube':
            from plugins.commands.window_cube import handle_show_cube
            handle_show_cube(app, '')
        elif action == 'confirmed close':
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            _overview_rows.remove(row)
            _overview_panel().remove(number)
        else:
            return False
    except Exception:
        logger.exception("Window overview action failed: %s", action)
        success = False
    if success is False:
        _speak(app, "Could not change that window.")
        return True
    if action not in {'go', 'confirmed close', 'cube'}:
        monitors = mover.get_monitors()
        monitor = _overview_monitor(hwnd, monitors)
        row.update(monitor=overview_monitor_number(monitor),
                   primary=bool(monitor and monitor.get('primary')))
        _overview_panel().monitor(number, row['monitor'], row['primary'])
    if not _overview_keep and action != 'confirmed close':
        _hide_overview()
    return True


def overview_monitor_number(monitor):
    if not monitor:
        return 1
    display = re.search(r'(\d+)$', monitor.get('device', ''))
    return int(display[1]) if display else monitor.get('index', 1)


def overview_monitor_target(text, monitors):
    """An explicit on/to target; numeric identity follows Windows DISPLAYn."""
    if text in {'main', 'primary'}:
        return next((m for m in monitors if m.get('primary')), None)
    index = _parse_monitor_index(text)
    for monitor in monitors:
        display = re.search(r'(\d+)$', monitor.get('device', ''))
        number = int(display[1]) if display else monitor.get('index')
        if number == index:
            return monitor
    return None


def handle_overview_number(app, value, remainder=""):
    if not _windows_supported():
        return False
    if not overview_active(app):
        return False
    parsed = parse_overview_action(f"{value} {remainder or ''}")
    if parsed is None:
        return False
    number, action, keep = parsed
    # Bare-number dispatch selects; explicit choose N retains its Go behavior.
    if action == 'go' and (remainder or '').strip().lower() in {'', 'keep it open'}:
        action = 'select'
    return overview_action(app, number, action, True if keep else None)


@command("read window", aliases=["read"], pack="window-management", risk_class="read",
         scope={"tags": ["window_overview.visible"]},
         param_schema={"number": {"type": "int", "required": True}})
def handle_read_window(app, remainder):
    if not _windows_supported():
        return False
    """Reads the full title of one window in the overview."""
    from samsara.spoken_numbers import parse_spoken_number
    if not overview_active(app):
        return False
    number = parse_spoken_number(remainder)
    row = next((r for r in _overview_rows if r['number'] == number), None)
    _speak(app, row['title'] if row else "Choose a window number from the overview.")
    return True


@command("confirm close", pack="window-management", risk_class="destructive",
         ai_visible=False, scope={"tags": ["window_overview.visible"]})
def handle_confirm_overview_close(app, remainder):
    if not _windows_supported():
        return False
    """Confirms the window close just requested in the overview."""
    return overview_action(app, 0, 'confirm close') if not remainder else False


handle_confirm_overview_close.handles_own_confirmation = True


@command("keep it open", pack="window-management", risk_class="ui",
         scope={"tags": ["window_overview.visible"]})
def handle_keep_overview_open(app, remainder):
    if not _windows_supported():
        return False
    """Keeps the overview open after window actions for this session."""
    global _overview_keep
    if not overview_active(app) or remainder:
        return False
    return overview_action(app, 0, 'keep open', True)


def handle_cancel_overview(app, remainder):
    if not _windows_supported():
        return False
    """Closes the window overview, or cancels recording when no overview is open."""
    if remainder:
        return False
    if overview_active(app):
        _hide_overview()
    else:
        app.cancel_recording()
    return True


def _register_overview_numbers():
    from samsara.spoken_numbers import NUMBER_WORDS
    words = {value: word for word, value in NUMBER_WORDS.items()}
    for number in range(1, 100):
        word = words.get(number) or words[number // 10 * 10] + ' ' + words[number % 10]
        def close(app, remainder, value=number):
            if remainder:
                return False
            return overview_action(app, value, 'close')
        close.__name__ = close.__qualname__ = f'handle_overview_close_{number}'
        close.__doc__ = f'Asks before closing window {number} in the overview.'
        close.handles_own_confirmation = True
        command(f'close {number}', aliases=[f'close {word}'], pack='window-management',
                risk_class='destructive', ai_visible=False,
                scope={'tags': ['window_overview.visible']})(close)
        # One through nine already belong to cube's shared number commands.
        if number < 10:
            continue
        def pick(app, remainder, value=number):
            return handle_overview_number(app, value, remainder)
        pick.__name__ = pick.__qualname__ = f'handle_overview_number_{number}'
        pick.__doc__ = f'Selects window {number} from the open overview.'
        command(f'window {word}', aliases=[f'window {number}', word, str(number)],
                pack='window-cube-numbers',
                risk_class='ui', ai_visible=False,
                scope={'tags': ['window_overview.visible']})(pick)


from samsara import command_scope as _overview_scope
_overview_scope.register_tag_source('window_overview.visible', overview_active)
_register_overview_numbers()
