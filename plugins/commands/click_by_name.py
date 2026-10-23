"""Name actions over the shared 602 service; no native work at import time."""
from dataclasses import dataclass, field
import threading
import time

from samsara import execution_policy
from samsara.command_scope import register_tag_source
from samsara.plugin_commands import command
from samsara.runtime import thread_registry
from samsara.uia import UIAError, get_service, normalize_name

TIMEOUT_S = 15.0
_lock = threading.RLock()
_pending = None


def _foreground():
    try:
        import win32gui
        return win32gui.GetForegroundWindow()
    except Exception:
        return 0


def _feedback(app, message):
    # Stand-in copy; the shared chip reaches both Compact and Full displays.
    from plugins.commands.show_numbers import _report_chip, _speak
    _speak(app, message)
    _report_chip(app, message, "error")


def _miss(app, name):
    _feedback(app, f'No match: {name[:39]}')


def prepare_text_click(app, query, modifier, keys):
    """Capture one HWND for name discovery and the existing OCR fallback."""
    from plugins.commands import show_numbers
    from samsara import screen_ocr
    hwnd = screen_ocr.foreground_hwnd()
    foreground = show_numbers._foreground_info(hwnd)
    if show_numbers.is_overlay_active():
        show_numbers._destroy_overlay(app)
        time.sleep(show_numbers._OVERLAY_HIDE_WAIT_S)
    handled = (try_named(app, query, expected_hwnd=hwnd)
               if modifier == 'single' and not keys else None)
    return hwnd, foreground, handled


@dataclass(eq=False)
class Selection:
    app: object = field(repr=False)
    service: object = field(repr=False)
    nodes: tuple = field(repr=False)
    verb: str
    hwnd: int
    generation: int
    deadline: float
    timer: object = None
    overlay: object = None


def _hide(selection):
    from samsara.ui import qt_runtime

    def hide():
        if selection.overlay is not None:
            selection.overlay.hide()
            selection.overlay = None
    qt_runtime.post(hide)


def cancel(selection=None):
    global _pending
    with _lock:
        if _pending is None or (selection is not None and _pending is not selection):
            return
        old, _pending = _pending, None
        if old.timer is not None:
            old.timer.cancel()
    _hide(old)


def _live(selection, app=None):
    return (time.monotonic() < selection.deadline
            and (app is None or selection.app is app)
            and execution_policy.current_generation(selection.app) == selection.generation
            and _foreground() == selection.hwnd)


def active(app=None):
    with _lock:
        selection = _pending
    if selection is None:
        return False
    try:
        live = _live(selection, app)
    except Exception:
        live = False
    if not live:
        cancel(selection)
    return live


def _show(selection):
    from samsara.ui import qt_runtime
    qt_runtime.ensure_started()

    def show():
        from samsara.ui.numbers_overlay_qt import MatchNumbersOverlay
        with _lock:
            if _pending is not selection or not active():
                return
            selection.overlay = MatchNumbersOverlay(
                selection.nodes, lambda: active(), lambda: cancel(selection))
            selection.overlay.show()
    qt_runtime.post(show)


def selection_number(text, app):
    """Only a whole bare number, and only in this app's live selection."""
    if not active(app):
        return None
    from samsara.spoken_numbers import parse_spoken_number
    return parse_spoken_number(text.strip().lower().rstrip('.!?'))


def _act(app, service, node, verb, hwnd):
    try:
        if _foreground() != hwnd:
            raise UIAError('foreground changed')
        # The service revalidates HWND, generation, live bounds and visibility
        # on its COM worker immediately before invoking the pattern.
        service.act(node, verb)
    except Exception:
        _feedback(app, "Target changed or unavailable. Try again.")
    return True  # A refused action must never become dictated text.


def try_named(app, name, verb="click", *, report_miss=False, expected_hwnd=None):
    """None means no UIA match: existing OCR/app handlers may continue."""
    global _pending
    name = (name or "").strip()
    if not name or not normalize_name(name):
        return None
    cancel()
    from plugins.commands import show_numbers
    if show_numbers.is_overlay_active():
        show_numbers._destroy_overlay(app)
    hwnd = expected_hwnd
    try:
        hwnd = _foreground() if expected_hwnd is None else expected_hwnd
        if not hwnd or _foreground() != hwnd:
            _feedback(app, "Window changed. Try again.")
            return True
        service = get_service()
        if len(name.split()) == 1:
            nodes = service.search(name)
        else:
            query = normalize_name(name)
            nodes = tuple(n for n in service.snapshot().nodes
                          if n.enabled and not n.offscreen and query in normalize_name(n.name))
        nodes = tuple(n for n in nodes if n.hwnd == hwnd
                      and n.rect[2] > n.rect[0] and n.rect[3] > n.rect[1])
        if hwnd and _foreground() != hwnd:
            _feedback(app, "Window changed. Try again.")
            return True
    except Exception:
        if _foreground() != hwnd:
            _feedback(app, "Window changed. Try again.")
            return True
        if report_miss:
            _feedback(app, "Could not read this window. Try again.")
            return True
        return None
    if not nodes:
        if report_miss:
            _miss(app, name)
            return True
        return None
    if len(nodes) == 1:
        return _act(app, service, nodes[0], verb, hwnd)
    selection = Selection(app, service, nodes, verb, hwnd,
                          execution_policy.current_generation(app), time.monotonic() + TIMEOUT_S)
    with _lock:
        _pending = selection
        selection.timer = thread_registry.timer(
            'click_by_name.expire', TIMEOUT_S, lambda: cancel(selection), daemon=True)
    try:
        _show(selection)
    except Exception:
        cancel(selection)
        _feedback(app, "Could not show matches. Try again.")
    return True


@command("choose control", pack="accessibility", risk_class="write",
         scope={"tags": ["click_by_name.pending"]},
         param_schema={"remainder": {"type": "str", "required": True}})
def choose_control(app, remainder):
    """Acts on a numbered match from the last control-name request."""
    number = selection_number(remainder, app)
    with _lock:
        selection = _pending
        if selection is None or not active(app):
            return True
        if number is None or not 1 <= number <= len(selection.nodes):
            return True
        node = selection.nodes[number - 1]
        cancel(selection)  # Single use, including a failed action.
    return _act(app, selection.service, node, selection.verb, selection.hwnd)


@command("toggle", pack="accessibility", risk_class="write",
         param_schema={"remainder": {"type": "str", "required": True}})
def toggle(app, remainder):
    """Toggles the control whose name contains the words you give."""
    return try_named(app, remainder, "toggle", report_miss=True) or True


@command("expand", pack="accessibility", risk_class="write",
         param_schema={"remainder": {"type": "str", "required": True}})
def expand(app, remainder):
    """Expands the control whose name contains the words you give."""
    return try_named(app, remainder, "expand", report_miss=True) or True


@command("collapse", pack="accessibility", risk_class="write",
         param_schema={"remainder": {"type": "str", "required": True}})
def collapse(app, remainder):
    """Collapses the control whose name contains the words you give."""
    return try_named(app, remainder, "collapse", report_miss=True) or True


@command("focus control", pack="accessibility", risk_class="write",
         param_schema={"remainder": {"type": "str", "required": True}})
def focus_control(app, remainder):
    """Focuses a named control in the current window."""
    return try_named(app, remainder, "focus", report_miss=True) or True


register_tag_source("click_by_name.pending", active)
