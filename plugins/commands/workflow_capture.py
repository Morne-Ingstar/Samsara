"""Workflow Capture — EXPERIMENTAL prototype.

Structure-only activity capture for automation pattern discovery.
Captures keyboard chords, UI click targets, app focus changes, and
text-entry facts (never content). The user reviews and selects what
may be analyzed before anything is sent to an AI.

Privacy guarantee: the review gate (show_review) sits between capture
and AI call. Only explicitly selected events are ever summarised or sent.

Commands:
    "start capture"    -- begin recording
    "stop capture"     -- stop and open the review window
    "capture status"   -- how many events so far
    "review capture"   -- re-open review with current log
"""

import ctypes
import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    from pynput.keyboard import Key, Listener as _KeyListener
    from pynput.mouse import Button, Listener as _MouseListener
    _PYNPUT = True
except ImportError:
    _PYNPUT = False
    logger.warning("[CAPTURE] pynput not available -- keyboard/mouse capture disabled")

_auto = None
_UIA = None  # None = not yet attempted; True/False = result

try:
    import win32gui
    import win32process
    import psutil as _psutil
    _WIN32 = True
except ImportError:
    _WIN32 = False

try:
    import pythoncom as _pythoncom
    import win32com.client as _wcom
    _WMI = True
except ImportError:
    _WMI = False

from samsara.plugin_commands import command


# ---------------------------------------------------------------------------
# Event record
# ---------------------------------------------------------------------------

@dataclass
class CaptureEvent:
    ts: float     # time.monotonic()
    kind: str     # 'chord' | 'click' | 'focus' | 'text_entry' | 'proc'
    label: str    # human-readable; never contains keystroke content or window titles


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_events: list = []
_capturing: bool = False
_key_listener = None
_mouse_listener = None
_focus_thread: "threading.Thread | None" = None
_focus_stop = threading.Event()
_text_entry_timer: "threading.Timer | None" = None
_text_entry_target: str = ""
_proc_thread: "threading.Thread | None" = None
_proc_stop = threading.Event()
# PIDs that descended from the foreground app this session; proc thread only.
_fg_subtree_pids: set = set()


# ---------------------------------------------------------------------------
# Modifier key tables (populated only when pynput available)
# ---------------------------------------------------------------------------

if _PYNPUT:
    _MODS = frozenset({
        Key.shift, Key.shift_l, Key.shift_r,
        Key.ctrl,  Key.ctrl_l,  Key.ctrl_r,
        Key.alt,   Key.alt_l,   Key.alt_r,
        Key.cmd,   Key.cmd_l,   Key.cmd_r,
    })
    _MOD_NAME = {
        Key.shift: 'Shift', Key.shift_l: 'Shift', Key.shift_r: 'Shift',
        Key.ctrl:  'Ctrl',  Key.ctrl_l:  'Ctrl',  Key.ctrl_r:  'Ctrl',
        Key.alt:   'Alt',   Key.alt_l:   'Alt',   Key.alt_r:   'Alt',
        Key.cmd:   'Win',   Key.cmd_l:   'Win',   Key.cmd_r:   'Win',
    }
else:
    _MODS = frozenset()
    _MOD_NAME = {}

_held_mods: set = set()   # only touched from the keyboard-listener thread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app_name(hwnd=None) -> str:
    if not _WIN32:
        return 'unknown'
    try:
        if hwnd is None:
            hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return 'unknown'
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return _psutil.Process(pid).name().removesuffix('.exe')
    except Exception:
        return 'unknown'


def _coarse_region(x: int, y: int) -> str:
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    col = 'left' if x < sw // 3 else ('center' if x < 2 * sw // 3 else 'right')
    row = 'top'  if y < sh // 3 else ('middle' if y < 2 * sh // 3 else 'bottom')
    return f"{row}-{col}"


def _resolve_click(x: int, y: int) -> str:
    """Privacy-safe description of what was clicked — role+name+app or coarse region."""
    global _auto, _UIA
    if _UIA is None:
        try:
            import uiautomation as _m
            _auto = _m
            _UIA = True
        except ImportError:
            _UIA = False
    if _UIA:
        try:
            ctrl = _auto.ControlFromPoint(x, y)
            if ctrl:
                role = ctrl.ControlTypeName or 'Element'
                name = (ctrl.Name or '').strip()
                hwnd = ctrl.NativeWindowHandle
                app = _app_name(hwnd or None)
                return f"{role} '{name}' in {app}" if name else f"{role} in {app}"
        except Exception:
            pass
    if _WIN32:
        try:
            hwnd = win32gui.WindowFromPoint((x, y))
            return f"click in {_app_name(hwnd)} at {_coarse_region(x, y)}"
        except Exception:
            pass
    return f"click at {_coarse_region(x, y)}"


def _resolve_focused_field() -> str:
    """Privacy-safe description of the focused input field — never its content."""
    global _auto, _UIA
    if _UIA is None:
        try:
            import uiautomation as _m
            _auto = _m
            _UIA = True
        except ImportError:
            _UIA = False
    if _UIA:
        try:
            focused = _auto.GetFocusedControl()
            if focused:
                role = focused.ControlTypeName or 'field'
                name = (focused.Name or '').strip()
                hwnd = focused.NativeWindowHandle
                app = _app_name(hwnd or None)
                return f"{role} '{name}' in {app}" if name else f"{role} in {app}"
        except Exception:
            pass
    return f"field in {_app_name()}"


def _format_chord(mods: set, key) -> str:
    parts = sorted(set(_MOD_NAME[m] for m in mods if m in _MOD_NAME))
    if hasattr(key, 'char') and key.char and key.char.isprintable():
        key_str = key.char.upper()
    elif hasattr(key, 'name') and key.name:
        key_str = key.name.replace('_', ' ').title()
    else:
        key_str = str(key).strip("'").upper()
    return '+'.join(parts + [key_str])


def _log(kind: str, label: str) -> None:
    with _lock:
        if _capturing:
            _events.append(CaptureEvent(ts=time.monotonic(), kind=kind, label=label))


# ---------------------------------------------------------------------------
# Keyboard callbacks  (run on pynput's background thread)
# ---------------------------------------------------------------------------

def _on_key_press(key) -> None:
    global _text_entry_timer, _text_entry_target
    if not _capturing:
        return
    if key in _MODS:
        _held_mods.add(key)
        return
    if _held_mods:
        chord = _format_chord(_held_mods, key)
        _log('chord', chord)
        if _text_entry_timer:
            _text_entry_timer.cancel()
            _text_entry_timer = None
        return
    # Plain key — track text-entry fact (no content)
    if _text_entry_timer:
        _text_entry_timer.cancel()
    _text_entry_target = _resolve_focused_field()
    _text_entry_timer = threading.Timer(2.0, _flush_text_entry)
    _text_entry_timer.daemon = True
    _text_entry_timer.start()


def _flush_text_entry() -> None:
    global _text_entry_timer, _text_entry_target
    target = _text_entry_target
    _text_entry_timer = None
    _log('text_entry', f"text entered in {target}")


def _on_key_release(key) -> None:
    _held_mods.discard(key)


# ---------------------------------------------------------------------------
# Mouse callback  (run on pynput's background thread)
# ---------------------------------------------------------------------------

def _on_click(x: int, y: int, button, pressed: bool) -> None:
    if not _capturing or not pressed:
        return
    if button not in (Button.left, Button.right):
        return
    label = _resolve_click(x, y)
    if button == Button.right:
        label = f"right-click: {label}"
    _log('click', label)


# ---------------------------------------------------------------------------
# Process-spawn tracking  (daemon thread)
# ---------------------------------------------------------------------------

def _get_fg_pid() -> int:
    """Return the PID of the current foreground window's process, or 0."""
    if not _WIN32:
        return 0
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid
    except Exception:
        pass
    return 0


def _on_new_proc(name: str, pid: int, ppid: int) -> None:
    """Called for each new process during capture; decides whether to log it.

    Scope rule: log only processes in the foreground app's depth-2 subtree.
    - Direct children (ppid == fg_pid): catches terminal spawning ConPTY.
    - Grandchildren (ppid was itself a direct child): catches ConPTY spawning
      pwsh.exe / bash.exe — the actual shell pane creation event.
    - Everything else is background noise and is silently skipped.

    Command-line arguments are intentionally NOT read here.
    """
    if not _capturing:
        return

    fg_pid = _get_fg_pid()

    is_fg_child  = (fg_pid != 0 and ppid == fg_pid)
    is_fg_subtree = (ppid in _fg_subtree_pids)

    if not is_fg_child and not is_fg_subtree:
        return

    _fg_subtree_pids.add(pid)

    parent_name = 'unknown'
    if _WIN32 and ppid:
        try:
            parent_name = _psutil.Process(ppid).name()
        except Exception:
            pass

    label = f"spawned {name} (parent: {parent_name})"
    _log('proc', label)
    logger.debug("[CAPTURE] proc: %s PID=%d ppid=%d parent=%s", name, pid, ppid, parent_name)


def _proc_loop_wmi(stop: threading.Event) -> bool:
    """Subscribe to Win32_Process creation via WMI; call _on_new_proc for each.

    Returns True if the WMI subscription was established and ran to completion.
    Returns False if WMI is unavailable or setup fails (caller should use psutil).
    Runs on its own daemon thread with STA COM initialized on that thread.
    """
    if not _WMI:
        return False
    try:
        _pythoncom.CoInitialize()
    except Exception as exc:
        logger.debug("[CAPTURE] WMI CoInitialize failed: %s", exc)
        return False

    try:
        svc = _wcom.GetObject("winmgmts://./root/cimv2")
        wql = (
            "SELECT * FROM __InstanceCreationEvent WITHIN 0.5 "
            "WHERE TargetInstance ISA 'Win32_Process'"
        )
        watcher = svc.ExecNotificationQuery(wql)
    except Exception as exc:
        logger.debug("[CAPTURE] WMI subscription failed: %s", exc)
        _pythoncom.CoUninitialize()
        return False

    logger.debug("[CAPTURE] WMI process watcher active")
    try:
        while not stop.is_set():
            try:
                event = watcher.NextEvent(500)   # blocks up to 500 ms
            except Exception:
                # NextEvent raises pywintypes.com_error on timeout (0x80043001);
                # just loop back to check stop flag.
                continue
            if event is None:
                continue
            try:
                tgt  = event.TargetInstance
                name = str(tgt.Name or '')
                pid  = int(tgt.ProcessId or 0)
                ppid = int(tgt.ParentProcessId or 0)
                _on_new_proc(name, pid, ppid)
            except Exception as exc:
                logger.debug("[CAPTURE] WMI event parse error: %s", exc)
    finally:
        _pythoncom.CoUninitialize()

    return True


def _proc_loop_psutil(stop: threading.Event) -> None:
    """Psutil-based process-list diff at 500 ms intervals.

    Used when WMI eventing is unavailable.  May miss very short-lived
    processes but is sufficient for long-lived shells / terminal panes.
    """
    prev_pids = set(_psutil.pids())
    while not stop.wait(0.5):
        if not _capturing:
            continue
        try:
            curr_pids = set(_psutil.pids())
            for pid in curr_pids - prev_pids:
                try:
                    proc = _psutil.Process(pid)
                    name = proc.name()
                    par  = proc.parent()
                    ppid = par.pid if par else 0
                    _on_new_proc(name, pid, ppid)
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
                except Exception as exc:
                    logger.debug("[CAPTURE] proc poll pid %d: %s", pid, exc)
            prev_pids = curr_pids
        except Exception as exc:
            logger.debug("[CAPTURE] proc poll error: %s", exc)


def _proc_loop(stop: threading.Event) -> None:
    """Entry point for the process-tracking thread.

    Tries WMI eventing first (real-time, low overhead); falls back to
    psutil polling (~500 ms latency) if WMI is unavailable.
    """
    if not _proc_loop_wmi(stop):
        logger.debug("[CAPTURE] WMI unavailable — using psutil process polling")
        _proc_loop_psutil(stop)


# ---------------------------------------------------------------------------
# Focus poller  (daemon thread)
# ---------------------------------------------------------------------------

def _focus_loop(stop: threading.Event) -> None:
    last_hwnd = None
    while not stop.wait(timeout=0.5):
        if not _capturing:
            continue
        try:
            hwnd = win32gui.GetForegroundWindow() if _WIN32 else 0
            if hwnd and hwnd != last_hwnd:
                last_hwnd = hwnd
                _log('focus', f"switched to {_app_name(hwnd)}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_capture() -> str:
    global _capturing, _key_listener, _mouse_listener, _focus_thread, _proc_thread

    with _lock:
        if _capturing:
            return "Capture already active."
        _events.clear()
        _capturing = True

    _held_mods.clear()
    _fg_subtree_pids.clear()

    if _PYNPUT:
        _key_listener = _KeyListener(on_press=_on_key_press, on_release=_on_key_release)
        _key_listener.start()
        _mouse_listener = _MouseListener(on_click=_on_click)
        _mouse_listener.start()

    if _WIN32:
        _focus_stop.clear()
        _focus_thread = threading.Thread(
            target=_focus_loop, args=(_focus_stop,),
            daemon=True, name='wf-capture-focus'
        )
        _focus_thread.start()

    _proc_stop.clear()
    _proc_thread = threading.Thread(
        target=_proc_loop, args=(_proc_stop,),
        daemon=True, name='wf-capture-proc'
    )
    _proc_thread.start()

    return "Capture started."


def stop_capture() -> list:
    global _capturing, _key_listener, _mouse_listener, _text_entry_timer, _proc_thread

    with _lock:
        if not _capturing:
            return []
        _capturing = False
        snapshot = list(_events)

    if _text_entry_timer:
        _text_entry_timer.cancel()
        _text_entry_timer = None

    if _key_listener:
        _key_listener.stop()
        _key_listener = None
    if _mouse_listener:
        _mouse_listener.stop()
        _mouse_listener = None

    _focus_stop.set()
    if _focus_thread and _focus_thread.is_alive():
        _focus_thread.join(timeout=1.0)

    _proc_stop.set()
    if _proc_thread and _proc_thread.is_alive():
        _proc_thread.join(timeout=2.0)
    _proc_thread = None

    return snapshot


def is_capturing() -> bool:
    with _lock:
        return _capturing


def event_count() -> int:
    with _lock:
        return len(_events)


# ---------------------------------------------------------------------------
# Summarise  (collapses consecutive duplicates; groups by kind)
# ---------------------------------------------------------------------------

def summarize(events: list) -> str:
    if not events:
        return "(no events selected)"

    lines = []
    i = 0
    while i < len(events):
        ev = events[i]
        j = i + 1
        while j < len(events) and events[j].label == ev.label:
            j += 1
        count = j - i
        lines.append(f"[x{count}] {ev.label}" if count > 1 else ev.label)
        i = j

    span = events[-1].ts - events[0].ts if len(events) > 1 else 0
    header = f"Workflow capture: {len(events)} events over {span:.0f}s\n---\n"
    return header + '\n'.join(lines)


# ---------------------------------------------------------------------------
# AI analysis  (called AFTER user review gate)
# ---------------------------------------------------------------------------

_AI_SYSTEM = """\
You are analyzing a structural workflow capture from a Windows computer.
The data contains: keyboard shortcuts/chords, UI clicks (element role, name, \
and owning app), app focus switches, text-entry facts (field name only — \
no typed content was recorded), and process-spawn events (process name and \
parent process name only — no command-line arguments were recorded).

Your task:
1. Identify repetitive patterns — sequences that recur 3+ times, or actions
   done repeatedly in the same app.
2. Propose specific Samsara voice commands or macros that could automate them.
3. Be concrete: name the command, describe what it does, show the pattern it
   replaces.

Format each proposal as:
  Command: "<voice command phrase>"
  Does: <what it automates>
  Pattern: <sequence it replaces>

If no clear automation opportunities exist, say so honestly.
Do NOT suggest commands that would capture or reproduce personal content.
"""


def analyze_local(summary: str, app) -> str:
    try:
        from plugins.commands.ask_ollama import ask_ollama
        return ask_ollama(summary, app, system=_AI_SYSTEM)
    except Exception as e:
        return f"Error (local): {e}"


def analyze_cloud(summary: str, app) -> str:
    try:
        from samsara import cloud_llm
        if not cloud_llm.is_enabled(app):
            return ("Error: cloud LLM not enabled. "
                    "Set cloud_llm.enabled=true and api_key in config.")
        return cloud_llm.send(_AI_SYSTEM, summary, app, timeout=60)
    except Exception as e:
        return f"Error (cloud): {e}"


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("start capture",
         aliases=["begin capture", "capture workflow", "start workflow capture"],
         pack="experimental")
def handle_start_capture(app, remainder):
    """Start structure-only workflow capture. Records shortcuts and clicks, never content."""
    msg = start_capture()
    print(f"[CAPTURE] {msg}")
    try:
        from samsara.ui import workflow_capture_qt
        workflow_capture_qt.set_active_indicator(True)
    except Exception:
        pass
    return True


@command("stop capture",
         aliases=["stop recording workflow", "end capture", "finish capture"],
         pack="experimental")
def handle_stop_capture(app, remainder):
    """Stop capture and open the review window."""
    events = stop_capture()
    print(f"[CAPTURE] Stopped. {len(events)} events captured.")
    try:
        from samsara.ui import workflow_capture_qt
        workflow_capture_qt.set_active_indicator(False)
        workflow_capture_qt.show_review(events, app)
    except Exception as e:
        print(f"[CAPTURE] Could not open review window: {e}")
    return True


@command("capture status",
         aliases=["workflow capture status", "how many events"],
         pack="experimental")
def handle_capture_status(app, remainder):
    state = "ACTIVE" if is_capturing() else "inactive"
    print(f"[CAPTURE] {state} — {event_count()} events")
    return True


@command("review capture",
         aliases=["show capture review", "open capture review"],
         pack="experimental")
def handle_review_capture(app, remainder):
    """Re-open the review window with the most recent capture."""
    with _lock:
        snapshot = list(_events)
    if not snapshot:
        print("[CAPTURE] No events captured yet. Use 'start capture' first.")
        return True
    try:
        from samsara.ui import workflow_capture_qt
        workflow_capture_qt.show_review(snapshot, app)
    except Exception as e:
        print(f"[CAPTURE] Could not open review window: {e}")
    return True
