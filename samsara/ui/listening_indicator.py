"""
Samsara Listening State Indicator

A small, borderless, always-on-top pill overlay that shows the current
dictation mode and pulses teal while audio is being captured.
Flashes green on successful dictation, red on errors.
"""

import sys
import tkinter as tk


# Teal color matching tray icon active state
_TEAL = "#00CED1"
_TEAL_DIM = "#007A7C"
_IDLE_BG = "#2B2B2B"
_IDLE_FG = "#888888"
_LISTENING_FG = "#FFFFFF"
_SNOOZE_BG = "#3D2E00"
_SNOOZE_FG = "#CC9900"

# Status flash colors
_FLASH_SUCCESS_BG = "#1B5E20"
_FLASH_SUCCESS_FG = "#66FF66"
_FLASH_ERROR_BG = "#7F0000"
_FLASH_ERROR_FG = "#FF6666"

# Pill geometry
_PILL_W = 150
_PILL_H = 36
_CORNER_RADIUS = 18
_EDGE_MARGIN = 24

# Pulse animation
_PULSE_INTERVAL_MS = 50
_PULSE_STEPS = 20  # frames per half-cycle

# Status flash timing
_FLASH_DURATION_MS = 600
_FLASH_FADE_STEPS = 6  # fade-out steps within the duration

# How often to re-assert topmost (ms) -- Windows steals it on taskbar click
_TOPMOST_INTERVAL_MS = 3000

# Valid positions
VALID_POSITIONS = (
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
)


def _lerp_color(c1, c2, t):
    """Linearly interpolate between two hex colors."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _get_work_area():
    """Return (x, y, w, h) of the primary monitor's work area (excludes taskbar).

    Falls back to full screen dimensions if the work area cannot be queried.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.wintypes.LONG),
                    ("top", ctypes.wintypes.LONG),
                    ("right", ctypes.wintypes.LONG),
                    ("bottom", ctypes.wintypes.LONG),
                ]

            rect = RECT()
            SPI_GETWORKAREA = 0x0030
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            )
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return None  # caller will fall back to screen dims


class ListeningIndicator:
    """Always-on-top pill overlay showing dictation mode and listening state."""

    def __init__(self, root):
        """
        Args:
            root: The main tkinter root window (must already exist).
        """
        self._root = root
        self._win = None
        self._canvas = None
        self._mode_text = "Hold"
        self._listening = False
        self._snoozed = False
        self._corner = "bottom-center"
        self._visible = False

        # Pulse state
        self._pulse_step = 0
        self._pulse_direction = 1  # 1 = brightening, -1 = dimming
        self._pulse_after_id = None

        # Status flash state
        self._flash_bg = None   # current flash bg override (or None)
        self._flash_fg = None   # current flash fg override (or None)
        self._flash_after_id = None
        self._flash_step = 0

        # Topmost re-assertion loop id
        self._topmost_after_id = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self):
        """Show the indicator pill."""
        if self._visible and self._win is not None:
            try:
                self._win.deiconify()
                self._win.attributes("-topmost", True)
                return
            except tk.TclError:
                self._win = None

        self._create_window()
        self._visible = True
        self._draw()
        self._position_window()
        self._start_topmost_loop()
        if self._listening:
            self._start_pulse()

    def hide(self):
        """Hide the indicator pill."""
        self._stop_pulse()
        self._stop_topmost_loop()
        self._cancel_flash()
        if self._win is not None:
            try:
                self._win.withdraw()
            except tk.TclError:
                self._win = None
        self._visible = False

    def set_mode(self, mode_str):
        """Update the displayed mode name (e.g. 'Hold', 'Wake Word')."""
        # mode_str is now a pre-formatted display string like "Hold + Wake"
        # from DictationApp._get_mode_display(), so use it directly.
        self._mode_text = mode_str if mode_str else "Hold"
        if self._visible:
            self._draw()

    def set_listening(self, active):
        """Set whether audio is actively being captured."""
        if active == self._listening:
            return
        self._listening = active
        if self._visible:
            if active:
                self._pulse_step = 0
                self._pulse_direction = 1
                self._draw()
                self._start_pulse()
            else:
                self._stop_pulse()
                self._draw()

    def set_snoozed(self, snoozed):
        """Show 'Snoozed' state in the pill when listening is paused."""
        if snoozed == self._snoozed:
            return
        self._snoozed = snoozed
        if self._visible:
            self._draw()

    def set_position(self, corner):
        """Set which screen position the pill sits in."""
        if corner not in VALID_POSITIONS:
            corner = "bottom-center"
        self._corner = corner
        if self._visible:
            self._position_window()

    def flash_success(self):
        """Brief green flash to indicate successful dictation."""
        if self._visible:
            self._start_flash(_FLASH_SUCCESS_BG, _FLASH_SUCCESS_FG)

    def flash_error(self):
        """Brief red flash to indicate an error or disturbance."""
        if self._visible:
            self._start_flash(_FLASH_ERROR_BG, _FLASH_ERROR_FG)

    def flash_wake(self):
        """Bright teal flash to indicate wake word was heard."""
        if self._visible:
            self._start_flash(_TEAL, _LISTENING_FG)

    def destroy(self):
        """Permanently destroy the indicator window."""
        self._stop_pulse()
        self._stop_topmost_loop()
        self._cancel_flash()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None
        self._visible = False

    # ------------------------------------------------------------------
    # Internal - drawing
    # ------------------------------------------------------------------

    def _create_window(self):
        """Create the borderless Toplevel pill."""
        self._win = tk.Toplevel(self._root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.85)

        # Click-through on Windows using transparent color trick
        transparent = "#010101"
        if sys.platform == "win32":
            self._win.attributes("-transparentcolor", transparent)

        self._win.configure(bg=transparent)

        self._canvas = tk.Canvas(
            self._win,
            width=_PILL_W,
            height=_PILL_H,
            bg=transparent,
            highlightthickness=0,
            bd=0,
        )
        self._canvas.pack()

        # Middle-click to dismiss
        self._canvas.bind("<Button-2>", lambda _e: self.hide())

        self._win.geometry(f"{_PILL_W}x{_PILL_H}")

    def _draw(self):
        """Redraw the pill with current state colors."""
        if self._canvas is None:
            return

        self._canvas.delete("all")

        # Flash overrides all other color states
        if self._flash_bg is not None:
            bg = self._flash_bg
            fg = self._flash_fg
        elif self._snoozed:
            bg = _SNOOZE_BG
            fg = _SNOOZE_FG
        elif self._listening:
            t = self._pulse_step / _PULSE_STEPS
            bg = _lerp_color(_TEAL_DIM, _TEAL, t)
            fg = _LISTENING_FG
        else:
            bg = _IDLE_BG
            fg = _IDLE_FG

        # Draw rounded rectangle (pill shape)
        r = _CORNER_RADIUS
        x0, y0, x1, y1 = 0, 0, _PILL_W, _PILL_H
        self._canvas.create_arc(x0, y0, x0 + 2 * r, y0 + 2 * r, start=90, extent=90, fill=bg, outline=bg)
        self._canvas.create_arc(x1 - 2 * r, y0, x1, y0 + 2 * r, start=0, extent=90, fill=bg, outline=bg)
        self._canvas.create_arc(x0, y1 - 2 * r, x0 + 2 * r, y1, start=180, extent=90, fill=bg, outline=bg)
        self._canvas.create_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, start=270, extent=90, fill=bg, outline=bg)
        self._canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=bg, outline=bg)
        self._canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=bg, outline=bg)

        # Mode text (show "Snoozed" when snooze is active)
        label = "Snoozed" if self._snoozed else self._mode_text
        if self._listening and not self._snoozed and self._flash_bg is None:
            label = f"  {label}"  # space for the dot
        self._canvas.create_text(
            _PILL_W // 2, _PILL_H // 2,
            text=label,
            fill=fg,
            font=("Segoe UI", 11, "bold") if sys.platform == "win32" else ("Helvetica", 11, "bold"),
        )

        # Recording dot indicator (only when listening and not flashing)
        if self._listening and not self._snoozed and self._flash_bg is None:
            dot_r = 5
            dot_x = 16
            dot_y = _PILL_H // 2
            self._canvas.create_oval(
                dot_x - dot_r, dot_y - dot_r,
                dot_x + dot_r, dot_y + dot_r,
                fill=fg, outline=fg,
            )

    def _position_window(self):
        """Place the pill in the configured screen position, inside the work area."""
        if self._win is None:
            return

        # Use work area (excludes taskbar) when available
        work = _get_work_area()
        if work:
            wa_x, wa_y, wa_w, wa_h = work
        else:
            try:
                wa_x, wa_y = 0, 0
                wa_w = self._win.winfo_screenwidth()
                wa_h = self._win.winfo_screenheight()
            except tk.TclError:
                return

        m = _EDGE_MARGIN
        cx = wa_x + (wa_w - _PILL_W) // 2  # horizontal center

        if self._corner == "top-left":
            x, y = wa_x + m, wa_y + m
        elif self._corner == "top-center":
            x, y = cx, wa_y + m
        elif self._corner == "top-right":
            x, y = wa_x + wa_w - _PILL_W - m, wa_y + m
        elif self._corner == "bottom-left":
            x, y = wa_x + m, wa_y + wa_h - _PILL_H - m
        elif self._corner == "bottom-center":
            x, y = cx, wa_y + wa_h - _PILL_H - m
        else:  # bottom-right
            x, y = wa_x + wa_w - _PILL_W - m, wa_y + wa_h - _PILL_H - m

        try:
            self._win.geometry(f"{_PILL_W}x{_PILL_H}+{x}+{y}")
        except tk.TclError:
            pass

    # --- status flash ---

    def _start_flash(self, bg, fg):
        """Start a brief colored flash that fades back to normal."""
        self._cancel_flash()
        self._flash_bg = bg
        self._flash_fg = fg
        self._flash_step = 0
        self._draw()
        interval = _FLASH_DURATION_MS // _FLASH_FADE_STEPS
        self._flash_after_id = self._root.after(interval, self._flash_tick)

    def _flash_tick(self):
        """One tick of the flash fade-out."""
        self._flash_step += 1
        if self._flash_step >= _FLASH_FADE_STEPS:
            self._flash_bg = None
            self._flash_fg = None
            self._flash_after_id = None
            self._draw()
            return

        # Fade the flash color toward the underlying state color
        t = self._flash_step / _FLASH_FADE_STEPS
        if self._snoozed:
            target_bg, target_fg = _SNOOZE_BG, _SNOOZE_FG
        elif self._listening:
            target_bg = _lerp_color(_TEAL_DIM, _TEAL, 0.5)
            target_fg = _LISTENING_FG
        else:
            target_bg, target_fg = _IDLE_BG, _IDLE_FG

        self._flash_bg = _lerp_color(self._flash_bg, target_bg, t)
        self._flash_fg = _lerp_color(self._flash_fg, target_fg, t)
        self._draw()

        try:
            interval = _FLASH_DURATION_MS // _FLASH_FADE_STEPS
            self._flash_after_id = self._root.after(interval, self._flash_tick)
        except tk.TclError:
            self._flash_after_id = None

    def _cancel_flash(self):
        """Cancel any in-progress flash."""
        if self._flash_after_id is not None:
            try:
                self._root.after_cancel(self._flash_after_id)
            except tk.TclError:
                pass
            self._flash_after_id = None
        self._flash_bg = None
        self._flash_fg = None

    # --- topmost re-assertion ---

    def _start_topmost_loop(self):
        """Periodically re-assert -topmost so the pill stays above the taskbar."""
        if self._topmost_after_id is not None:
            return
        self._topmost_tick()

    def _stop_topmost_loop(self):
        if self._topmost_after_id is not None:
            try:
                self._root.after_cancel(self._topmost_after_id)
            except tk.TclError:
                pass
            self._topmost_after_id = None

    def _topmost_tick(self):
        if not self._visible or self._win is None:
            self._topmost_after_id = None
            return
        try:
            self._win.attributes("-topmost", True)
            self._win.lift()
        except tk.TclError:
            self._topmost_after_id = None
            return
        try:
            self._topmost_after_id = self._root.after(
                _TOPMOST_INTERVAL_MS, self._topmost_tick
            )
        except tk.TclError:
            self._topmost_after_id = None

    # --- pulse animation ---

    def _start_pulse(self):
        """Begin the pulse animation loop."""
        if self._pulse_after_id is not None:
            return  # already running
        self._pulse_tick()

    def _pulse_tick(self):
        """One tick of the pulse animation."""
        if not self._listening or not self._visible:
            self._pulse_after_id = None
            return

        self._pulse_step += self._pulse_direction
        if self._pulse_step >= _PULSE_STEPS:
            self._pulse_step = _PULSE_STEPS
            self._pulse_direction = -1
        elif self._pulse_step <= 0:
            self._pulse_step = 0
            self._pulse_direction = 1

        self._draw()

        try:
            self._pulse_after_id = self._root.after(_PULSE_INTERVAL_MS, self._pulse_tick)
        except tk.TclError:
            self._pulse_after_id = None

    def _stop_pulse(self):
        """Cancel the pulse animation."""
        if self._pulse_after_id is not None:
            try:
                self._root.after_cancel(self._pulse_after_id)
            except tk.TclError:
                pass
            self._pulse_after_id = None
        self._pulse_step = 0
        self._pulse_direction = 1
