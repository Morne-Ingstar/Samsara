"""
Samsara Listening State Indicator — Win32 per-pixel-alpha pill overlay.

Replaces the Tkinter Toplevel/-transparentcolor implementation with a proper
Win32 layered window (UpdateLayeredWindow + PIL).  The pill is rendered as a
small RGBA image positioned directly by UpdateLayeredWindow — no full-screen
canvas, no chroma-key, no -alpha/-transparentcolor fight.

Key improvements over the Tkinter version:
  - WS_EX_TOPMOST stays topmost permanently — no 3-second polling hack
  - Per-pixel alpha — pill is fully opaque, background is truly invisible
  - Pulse animation runs on a background thread — no root.after() dependency
  - Efficient: only a ~90x36 px image is allocated and pushed per frame

Public API is identical to the old Tkinter version.  root parameter is
accepted for backwards compatibility but is not used.
"""

import ctypes
import ctypes.wintypes
import logging
import sys
import threading

from PIL import Image, ImageDraw, ImageFont

from samsara.ui.layered_overlay import (
    _CLASS_NAME,
    _ensure_class_registered,
    _push_image_to_window,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_POPUP,
    SW_HIDE,
    SW_SHOWNOACTIVATE,
    user32,
    kernel32,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

_TEAL         = "#00CED1"
_TEAL_DIM     = "#007A7C"
_IDLE_BG      = "#2B2B2B"
_IDLE_FG      = "#888888"
_LISTENING_FG = "#FFFFFF"
_SNOOZE_BG    = "#3D2E00"
_SNOOZE_FG    = "#CC9900"
_CMD_BG           = "#4d2600"
_CMD_FG           = "#ff8c00"
_CMD_ACTIVE_BG    = "#7a3d00"
_CMD_ACTIVE_FG    = "#ffa500"
_FLASH_SUCCESS_BG = "#1B5E20"
_FLASH_SUCCESS_FG = "#66FF66"
_FLASH_ERROR_BG   = "#7F0000"
_FLASH_ERROR_FG   = "#FF6666"

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_PILL_H       = 36
_PILL_MIN_W   = 90
_PILL_PAD_X   = 22
_DOT_SPACE    = 22
_CORNER_R     = 18
_EDGE_MARGIN  = 24

# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

_PULSE_INTERVAL_MS = 50
_PULSE_STEPS       = 20
_FLASH_DURATION_MS = 600
_FLASH_FADE_STEPS  = 6

VALID_POSITIONS = (
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
)

# ---------------------------------------------------------------------------
# Win32 extras (not in layered_overlay)
# ---------------------------------------------------------------------------

SWP_NOACTIVATE = 0x0010

user32.SetWindowPos.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.UINT,
]
user32.SetWindowPos.restype = ctypes.wintypes.BOOL

# ---------------------------------------------------------------------------
# Font
# ---------------------------------------------------------------------------

_FONT_PATHS = [
    r'C:\Windows\Fonts\segoeuib.ttf',
    r'C:\Windows\Fonts\segoeui.ttf',
    r'C:\Windows\Fonts\arial.ttf',
]
_font_cache = None


def _get_font():
    global _font_cache
    if _font_cache is None:
        for path in _FONT_PATHS:
            try:
                _font_cache = ImageFont.truetype(path, 13)
                return _font_cache
            except Exception:
                pass
        _font_cache = ImageFont.load_default()
    return _font_cache


def _measure_text(text: str) -> int:
    try:
        bbox = _get_font().getbbox(text)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_to_rgba(color: str, alpha: int = 255) -> tuple:
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return (r, g, b, alpha)


def _lerp_color(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _get_work_area() -> tuple:
    """Return (x, y, w, h) of the primary monitor work area (excludes taskbar)."""
    if sys.platform == "win32":
        try:
            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left",   ctypes.wintypes.LONG),
                    ("top",    ctypes.wintypes.LONG),
                    ("right",  ctypes.wintypes.LONG),
                    ("bottom", ctypes.wintypes.LONG),
                ]
            rect = _RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 0, 0, 1920, 1080


# ---------------------------------------------------------------------------
# ListeningIndicator
# ---------------------------------------------------------------------------

class ListeningIndicator:
    """Always-on-top pill overlay — Win32 per-pixel-alpha, no Tkinter."""

    def __init__(self, root=None):
        # root accepted for API backwards compatibility, not used
        self._lock        = threading.Lock()
        self._render_lock = threading.Lock()
        self._hwnd        = 0
        self._visible     = False

        self._mode_text    = "Hold"
        self._listening    = False
        self._snoozed      = False
        self._corner       = "bottom-center"
        self._command_mode = False

        # Pulse
        self._pulse_step      = 0
        self._pulse_direction = 1
        self._pulse_stop      = None   # threading.Event
        self._pulse_thread    = None

        # Flash
        self._flash_bg    = None
        self._flash_fg    = None
        self._flash_step  = 0
        self._flash_timer = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self):
        with self._lock:
            self._ensure_hwnd()
            self._visible = True
        self._redraw()
        with self._lock:
            listening = self._listening
        if listening:
            self._start_pulse()

    def hide(self):
        self._stop_pulse()
        self._cancel_flash()
        with self._lock:
            self._visible = False
            if self._hwnd:
                user32.ShowWindow(self._hwnd, SW_HIDE)

    def set_mode(self, mode_str):
        with self._lock:
            self._mode_text = mode_str if mode_str else "Hold"
        if self._visible:
            self._redraw()

    def set_listening(self, active):
        with self._lock:
            if active == self._listening:
                return
            self._listening = active
            visible = self._visible
        if not visible:
            return
        if active:
            self._pulse_step = 0
            self._pulse_direction = 1
            self._redraw()
            self._start_pulse()
        else:
            self._stop_pulse()
            self._redraw()

    def set_snoozed(self, snoozed):
        with self._lock:
            if snoozed == self._snoozed:
                return
            self._snoozed = snoozed
        if self._visible:
            self._redraw()

    def set_command_mode(self, active):
        with self._lock:
            if active == self._command_mode:
                return
            self._command_mode = active
        if self._visible:
            self._redraw()

    def set_position(self, corner):
        if corner not in VALID_POSITIONS:
            corner = "bottom-center"
        with self._lock:
            self._corner = corner
        if self._visible:
            self._redraw()

    def flash_success(self):
        if self._visible:
            self._start_flash(_FLASH_SUCCESS_BG, _FLASH_SUCCESS_FG)

    def flash_error(self):
        if self._visible:
            self._start_flash(_FLASH_ERROR_BG, _FLASH_ERROR_FG)

    def flash_wake(self):
        if self._visible:
            self._start_flash(_TEAL, _LISTENING_FG)

    def destroy(self):
        self._stop_pulse()
        self._cancel_flash()
        with self._lock:
            self._visible = False
            if self._hwnd:
                try:
                    user32.DestroyWindow(self._hwnd)
                except Exception:
                    pass
                self._hwnd = 0

    # ------------------------------------------------------------------
    # Win32 window management
    # ------------------------------------------------------------------

    def _ensure_hwnd(self):
        """Create the Win32 window if it doesn't exist yet. Call under _lock."""
        if self._hwnd:
            return
        _ensure_class_registered()
        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT
                    | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        hwnd = user32.CreateWindowExW(
            ex_style, _CLASS_NAME, '', WS_POPUP,
            0, 0, _PILL_MIN_W, _PILL_H,
            0, 0, kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            raise OSError(
                f'ListeningIndicator: CreateWindowExW failed '
                f'(error {ctypes.GetLastError()})'
            )
        self._hwnd = hwnd

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _state_snapshot(self):
        """Return a consistent snapshot of all display state under lock."""
        with self._lock:
            return {
                'flash_bg':    self._flash_bg,
                'flash_fg':    self._flash_fg,
                'snoozed':     self._snoozed,
                'command_mode': self._command_mode,
                'listening':   self._listening,
                'pulse_step':  self._pulse_step,
                'mode_text':   self._mode_text,
                'corner':      self._corner,
                'hwnd':        self._hwnd,
                'visible':     self._visible,
            }

    def _resolve_colors(self, s: dict) -> tuple:
        """Return (bg, fg, label, show_dot) from state snapshot."""
        if s['flash_bg'] is not None:
            label = ("Snoozed" if s['snoozed']
                     else "CMD" if s['command_mode']
                     else s['mode_text'])
            return s['flash_bg'], s['flash_fg'], label, False

        if s['snoozed']:
            return _SNOOZE_BG, _SNOOZE_FG, "Snoozed", False

        t = s['pulse_step'] / _PULSE_STEPS

        if s['command_mode'] and s['listening']:
            return _lerp_color(_CMD_BG, _CMD_ACTIVE_BG, t), _CMD_ACTIVE_FG, "CMD", False

        if s['command_mode']:
            return _CMD_BG, _CMD_FG, "CMD", False

        if s['listening']:
            return _lerp_color(_TEAL_DIM, _TEAL, t), _LISTENING_FG, s['mode_text'], True

        return _IDLE_BG, _IDLE_FG, s['mode_text'], False

    def _render_pill(self, bg: str, fg: str, label: str, show_dot: bool) -> tuple:
        """Render the pill as a PIL RGBA image. Returns (Image, pill_w, pill_h)."""
        dot_reserve = _DOT_SPACE if show_dot else 0
        pill_w = max(_PILL_MIN_W, _measure_text(label) + 2 * _PILL_PAD_X + dot_reserve)
        pill_h = _PILL_H

        img  = Image.new('RGBA', (pill_w, pill_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = _get_font()

        bg_rgba = _hex_to_rgba(bg)
        fg_rgba = _hex_to_rgba(fg)

        draw.rounded_rectangle(
            [0, 0, pill_w - 1, pill_h - 1],
            radius=_CORNER_R,
            fill=bg_rgba,
        )

        if show_dot:
            dot_r = 5
            dot_x = 14
            dot_y = pill_h // 2
            draw.ellipse(
                [dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r],
                fill=fg_rgba,
            )

        try:
            bbox = font.getbbox(label)
            tw   = bbox[2] - bbox[0]
            th   = bbox[3] - bbox[1]
            tx   = (pill_w + dot_reserve) // 2 - tw // 2
            ty   = pill_h // 2 - th // 2
            draw.text((tx, ty), label, fill=fg_rgba, font=font)
        except Exception:
            draw.text((pill_w // 2, pill_h // 2), label, fill=fg_rgba,
                      font=font, anchor="mm")

        return img, pill_w, pill_h

    def _compute_position(self, pill_w: int, corner: str) -> tuple:
        """Return (screen_x, screen_y) for the pill's top-left corner."""
        wa_x, wa_y, wa_w, wa_h = _get_work_area()
        m  = _EDGE_MARGIN
        cx = wa_x + (wa_w - pill_w) // 2

        if corner == "top-left":
            return wa_x + m, wa_y + m
        if corner == "top-center":
            return cx, wa_y + m
        if corner == "top-right":
            return wa_x + wa_w - pill_w - m, wa_y + m
        if corner == "bottom-left":
            return wa_x + m, wa_y + wa_h - _PILL_H - m
        if corner == "bottom-right":
            return wa_x + wa_w - pill_w - m, wa_y + wa_h - _PILL_H - m
        # bottom-center (default)
        return cx, wa_y + wa_h - _PILL_H - m

    def _redraw(self):
        with self._render_lock:
            s = self._state_snapshot()
            if not s['visible'] or not s['hwnd']:
                return

            bg, fg, label, show_dot = self._resolve_colors(s)
            pill_img, pill_w, pill_h = self._render_pill(bg, fg, label, show_dot)
            px, py = self._compute_position(pill_w, s['corner'])

            # UpdateLayeredWindow repositions + resizes the window atomically
            ok = _push_image_to_window(s['hwnd'], pill_img, px, py)
            if not ok:
                return

            # Show window on first render
            with self._lock:
                hwnd    = self._hwnd
                visible = self._visible
            if hwnd and visible:
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    # ------------------------------------------------------------------
    # Pulse animation — runs on a background thread
    # ------------------------------------------------------------------

    def _start_pulse(self):
        with self._lock:
            if self._pulse_thread is not None and self._pulse_thread.is_alive():
                return
            stop = threading.Event()
            self._pulse_stop   = stop
            t = threading.Thread(target=self._pulse_loop, args=(stop,), daemon=True)
            self._pulse_thread = t
        t.start()

    def _pulse_loop(self, stop: threading.Event):
        interval = _PULSE_INTERVAL_MS / 1000.0
        while not stop.wait(timeout=interval):
            with self._lock:
                if not self._listening or not self._visible:
                    break
                self._pulse_step += self._pulse_direction
                if self._pulse_step >= _PULSE_STEPS:
                    self._pulse_step     = _PULSE_STEPS
                    self._pulse_direction = -1
                elif self._pulse_step <= 0:
                    self._pulse_step     = 0
                    self._pulse_direction = 1
            self._redraw()

    def _stop_pulse(self):
        with self._lock:
            stop = self._pulse_stop
            self._pulse_stop   = None
            self._pulse_thread = None
        if stop is not None:
            stop.set()
        self._pulse_step      = 0
        self._pulse_direction = 1

    # ------------------------------------------------------------------
    # Flash animation — threading.Timer chain
    # ------------------------------------------------------------------

    def _start_flash(self, bg: str, fg: str):
        self._cancel_flash()
        with self._lock:
            self._flash_bg   = bg
            self._flash_fg   = fg
            self._flash_step = 0
        self._redraw()
        self._schedule_flash_tick()

    def _schedule_flash_tick(self):
        interval = (_FLASH_DURATION_MS // _FLASH_FADE_STEPS) / 1000.0
        t = threading.Timer(interval, self._flash_tick)
        t.daemon = True
        with self._lock:
            self._flash_timer = t
        t.start()

    def _flash_tick(self):
        with self._lock:
            self._flash_step += 1
            step     = self._flash_step
            flash_bg = self._flash_bg
            flash_fg = self._flash_fg
            snoozed  = self._snoozed
            cmd_mode = self._command_mode
            listening = self._listening

        if step >= _FLASH_FADE_STEPS or flash_bg is None:
            with self._lock:
                self._flash_bg    = None
                self._flash_fg    = None
                self._flash_timer = None
            self._redraw()
            return

        t = step / _FLASH_FADE_STEPS
        if snoozed:
            target_bg, target_fg = _SNOOZE_BG, _SNOOZE_FG
        elif cmd_mode:
            target_bg, target_fg = _CMD_BG, _CMD_FG
        elif listening:
            target_bg = _lerp_color(_TEAL_DIM, _TEAL, 0.5)
            target_fg = _LISTENING_FG
        else:
            target_bg, target_fg = _IDLE_BG, _IDLE_FG

        with self._lock:
            self._flash_bg = _lerp_color(flash_bg, target_bg, t)
            self._flash_fg = _lerp_color(flash_fg, target_fg, t)

        self._redraw()
        self._schedule_flash_tick()

    def _cancel_flash(self):
        with self._lock:
            t = self._flash_timer
            self._flash_timer = None
            self._flash_bg    = None
            self._flash_fg    = None
        if t is not None:
            t.cancel()
