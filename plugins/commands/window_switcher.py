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
import threading
import time

from samsara.plugin_commands import command

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

user32  = ctypes.windll.user32
ole32   = ctypes.windll.ole32
_c_void = ctypes.c_void_p
HRESULT = ctypes.HRESULT

GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
SW_RESTORE        = 9
WM_CLOSE          = 0x0010

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock          = threading.Lock()
_overlays      = []     # list of tk.Toplevel (Tkinter fallback)
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
    if any(w in t for w in ('left', 'main', 'primary', 'monitor 1', '1st', 'first')):
        return 1
    if any(w in t for w in ('right', 'second', 'monitor 2', '2nd')):
        return 2
    if any(w in t for w in ('middle', 'center', 'third', 'monitor 3', '3rd')):
        return 3
    m = re.search(r'monitor\s*(\d+)', t)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(\d)\b', t)
    if m:
        return int(m.group(1))
    return None

# ---------------------------------------------------------------------------
# Window enumeration
# ---------------------------------------------------------------------------

_SKIP_TITLES = frozenset({
    'Program Manager', 'Windows Shell Experience Host',
    'Microsoft Text Input Application',
})

def _own_hwnd(app) -> set:
    """Return hwnds of Samsara's own Tkinter windows (to exclude from listing)."""
    hwnds = set()
    if app is None:
        return hwnds
    try:
        root = app.root
        hwnds.add(root.winfo_id())
        for child in root.winfo_children():
            try:
                hwnds.add(child.winfo_id())
            except Exception:
                pass
    except Exception:
        pass
    return hwnds


def _get_all_windows(exclude_hwnds: set = None) -> list:
    """Enumerate all visible and minimized application windows.

    Returns list of (hwnd, title, RECT, is_minimized).
    """
    exclude = exclude_hwnds or set()
    results = []

    def _cb(hwnd, _):
        if hwnd in exclude:
            return True
        visible   = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        if not visible and not minimized:
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
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
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
    try:
        style = ctypes.windll.user32.GetWindowLongW(win_id, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            win_id, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception as e:
        print(f"[WINSW] click-through failed: {e}")


def _create_overlays(app, mapping: dict) -> None:
    """Create one Toplevel label per visible window. Main-thread only."""
    global _overlays
    _destroy_overlays_sync()

    try:
        import tkinter as tk
    except ImportError:
        return

    for letter, (hwnd, title, is_min) in sorted(mapping.items()):
        if is_min:
            continue
        try:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            pill_w, pill_h = 54, 54
            cx = (rect.left + rect.right) // 2 - pill_w // 2
            cy = rect.top + 10

            win = tk.Toplevel(app.root)
            win.overrideredirect(True)
            win.attributes('-topmost', True)
            win.attributes('-alpha', 0.88)
            win.configure(bg='#1a1a1a')
            win.geometry(f"{pill_w}x{pill_h}+{cx}+{cy}")

            lbl = tk.Label(win, text=letter,
                           font=('Segoe UI', 26, 'bold'),
                           fg='white', bg='#1a1a1a')
            lbl.place(relx=0.5, rely=0.5, anchor='center')

            win.update_idletasks()
            _set_click_through(win.winfo_id())

            _overlays.append(win)
        except Exception as e:
            print(f"[WINSW] overlay for {letter} ({title[:30]}): {e}")


def _destroy_overlays_sync() -> None:
    """Destroy all overlays. Main-thread only."""
    global _overlays
    for win in list(_overlays):
        try:
            win.destroy()
        except Exception:
            pass
    _overlays.clear()


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
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))

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
                    lbl.setFont(QFont("Segoe UI", 22, QFont.Bold))
                    lbl.setStyleSheet(
                        "color: white;"
                        " background-color: rgba(26,26,26,224);"
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
    """Dismiss overlays, optionally clear mapping. Main-thread only."""
    global _active, _mapping
    _destroy_overlays_sync()
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
        except Exception:
            pass
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
            app.root.after(0, lambda: _dismiss_all(clear_mapping=True))
            print("[WINSW] Auto-dismissed after 30s inactivity")
        except Exception:
            pass

    t = threading.Timer(_AUTO_DISMISS_S, _fire)
    t.daemon = True
    t.start()
    _dismiss_timer = t

# ---------------------------------------------------------------------------
# Focus helper
# ---------------------------------------------------------------------------

def _force_focus(hwnd: int) -> None:
    """Restore if minimized, then force foreground."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    current     = user32.GetForegroundWindow()
    current_tid = user32.GetWindowThreadProcessId(current, None)
    target_tid  = user32.GetWindowThreadProcessId(hwnd,    None)
    if current_tid != target_tid:
        user32.AttachThreadInput(current_tid, target_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(current_tid, target_tid, False)
    else:
        user32.SetForegroundWindow(hwnd)

# ---------------------------------------------------------------------------
# Monitor helpers (delegates to windows.py)
# ---------------------------------------------------------------------------

def _get_monitors() -> list:
    try:
        from plugins.commands.windows import get_monitors
        return get_monitors()
    except Exception:
        return []


def _move_window(hwnd: int, monitor: dict) -> None:
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
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _set_process_mute(hwnd: int, mute: bool) -> bool:
    """Mute/unmute all Core Audio sessions belonging to hwnd's PID."""
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
        get_default = _vtfunc(enum, 4, HRESULT,
                               _c_void, ctypes.c_uint, ctypes.c_uint,
                               ctypes.POINTER(_c_void))
        device = _c_void()
        hr = get_default(enum, 0, 1, ctypes.byref(device))
        _vtfunc(enum, 2, ctypes.c_ulong, _c_void)(enum)   # Release
        if hr != 0 or not device:
            return False

        # 3. IAudioSessionManager2: IMMDevice::Activate - slot 3
        activate = _vtfunc(device, 3, HRESULT,
                            _c_void, _c_void, ctypes.c_ulong,
                            _c_void, ctypes.POINTER(_c_void))
        mgr2 = _c_void()
        hr = activate(device, _IID_IAudSessMgr2, _CLSCTX_ALL, None,
                      ctypes.byref(mgr2))
        _vtfunc(device, 2, ctypes.c_ulong, _c_void)(device)  # Release
        if hr != 0 or not mgr2:
            return False

        # 4. IAudioSessionEnumerator: GetSessionEnumerator - slot 5
        get_sess_enum = _vtfunc(mgr2, 5, HRESULT,
                                 _c_void, ctypes.POINTER(_c_void))
        sess_enum = _c_void()
        hr = get_sess_enum(mgr2, ctypes.byref(sess_enum))
        _vtfunc(mgr2, 2, ctypes.c_ulong, _c_void)(mgr2)   # Release
        if hr != 0 or not sess_enum:
            return False

        # 5. GetCount - slot 3
        get_count = _vtfunc(sess_enum, 3, HRESULT,
                             _c_void, ctypes.POINTER(ctypes.c_int))
        count = ctypes.c_int(0)
        get_count(sess_enum, ctypes.byref(count))

        # 6. GetSession - slot 4
        get_session = _vtfunc(sess_enum, 4, HRESULT,
                               _c_void, ctypes.c_int,
                               ctypes.POINTER(_c_void))

        matched = 0
        for i in range(count.value):
            ctrl = _c_void()
            if get_session(sess_enum, i, ctypes.byref(ctrl)) != 0 or not ctrl:
                continue

            # 7. QI ctrl → IAudioSessionControl2
            qi = _vtfunc(ctrl, 0, HRESULT,
                         _c_void, _c_void, ctypes.POINTER(_c_void))
            ctrl2 = _c_void()
            hr = qi(ctrl, _IID_IAudSessCtrl2, ctypes.byref(ctrl2))
            _vtfunc(ctrl, 2, ctypes.c_ulong, _c_void)(ctrl)  # Release ctrl

            if hr != 0 or not ctrl2:
                continue

            # 8. GetProcessId - slot 14
            get_pid_f = _vtfunc(ctrl2, 14, HRESULT,
                                 _c_void, ctypes.POINTER(wintypes.DWORD))
            sess_pid = wintypes.DWORD(0)
            get_pid_f(ctrl2, ctypes.byref(sess_pid))

            if sess_pid.value == target_pid:
                # 9. QI ctrl2 → ISimpleAudioVolume
                qi2 = _vtfunc(ctrl2, 0, HRESULT,
                               _c_void, _c_void, ctypes.POINTER(_c_void))
                sav = _c_void()
                if qi2(ctrl2, _IID_ISimpleAudVol, ctypes.byref(sav)) == 0 and sav:
                    # SetMute - slot 5
                    set_mute = _vtfunc(sav, 5, HRESULT,
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

# ---------------------------------------------------------------------------
# "show windows" — entry point
# ---------------------------------------------------------------------------

@command("show windows",
         aliases=["label windows", "window labels"],
         pack="window-management")
def handle_show_windows(app, remainder):
    global _mapping, _active, _app_ref

    exclude = _own_hwnd(app)
    windows = _get_all_windows(exclude)
    if not windows:
        print("[WINSW] No windows found")
        return True

    new_mapping = _assign_letters(windows)

    with _lock:
        _mapping = new_mapping
        _active  = True
    _app_ref = app

    mgr = _get_manager()
    if mgr is not None:
        mgr.request_show(new_mapping)
    else:
        app.root.after(0, lambda: _create_overlays(app, new_mapping))
    _reset_timer()

    visible_count = sum(1 for _, _, m in new_mapping.values() if not m)
    print(f"[WINSW] {len(new_mapping)} windows labeled "
          f"({visible_count} visible, {len(new_mapping) - visible_count} minimized): "
          + ", ".join(
              f"{l}={t[:20]}{'(min)' if m else ''}"
              for l, (_, t, m) in sorted(new_mapping.items())))
    return True

# ---------------------------------------------------------------------------
# "window switch" — focus, dismiss overlays
# ---------------------------------------------------------------------------

@command("window switch",
         aliases=["window focus", "switch to window", "go to window"],
         pack="window-management")
def handle_window_switch(app, remainder):
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window switch B.")
        return True

    entry = _resolve(app, letters[0])
    if entry is None:
        return True

    hwnd, title, _ = entry
    _force_focus(hwnd)
    app.root.after(0, lambda: _dismiss_all(clear_mapping=False))
    _cancel_timer()
    print(f"[WINSW] Switched to: {title}")
    return True

# ---------------------------------------------------------------------------
# "window bring" — focus without dismissing
# ---------------------------------------------------------------------------

@command("window bring",
         aliases=["bring window", "bring forward"],
         pack="window-management")
def handle_window_bring(app, remainder):
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
         pack="window-management")
def handle_window_move(app, remainder):
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
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
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
         pack="window-management")
def handle_window_mute(app, remainder):
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
         pack="window-management")
def handle_window_unmute(app, remainder):
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
         pack="window-management")
def handle_window_close(app, remainder):
    letters = _parse_letters(remainder or '')
    if not letters:
        _speak(app, "Which window? Say window close D.")
        return True

    letter = letters[0].upper()
    entry  = _resolve(app, letter)
    if entry is None:
        return True

    hwnd, title, _ = entry
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

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
         pack="window-management")
def handle_window_copy(app, remainder):
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
         pack="window-management")
def handle_window_tile(app, remainder):
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
         pack="window-management")
def handle_hide_windows(app, remainder):
    global _app_ref
    _app_ref = app
    app.root.after(0, lambda: _dismiss_all(clear_mapping=True))
    print("[WINSW] Dismissed window labels")
    return True

# ---------------------------------------------------------------------------
# "read windows"
# ---------------------------------------------------------------------------

@command("read windows",
         aliases=["list windows", "what windows"],
         pack="window-management")
def handle_read_windows(app, remainder):
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
