"""
Samsara Listening State Indicator — PySide6 pill overlay.

Replaces the Win32 UpdateLayeredWindow + PIL implementation with a
native Qt widget rendered by QPainter.  No ctypes, no PIL, no
background threads.

Pulse animation uses QTimer on the Qt event loop.
Flash animation uses a single-shot QTimer chain.

All public methods must be called from the Qt main thread.
dictation.py wraps every call in _schedule_ui(), which marshals to
the Qt thread via QTimer.singleShot, so this constraint is satisfied
automatically.

Public API (unchanged from Win32 version):
  show() / hide() / destroy()
  set_mode(mode_str)
  set_listening(bool)
  set_snoozed(bool)
  set_command_mode(active: bool)
  set_position(corner: str)
  flash_success() / flash_error() / flash_wake()
"""

import logging
import math

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QWidget

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
_DOT_R        = 5
_DOT_X        = 14
_CORNER_R     = 18.0
_EDGE_MARGIN  = 24

# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

_PULSE_INTERVAL_MS   = 50
_PULSE_STEPS         = 20
_FLASH_DURATION_MS   = 600
_FLASH_FADE_STEPS    = 6
_FLASH_STEP_INTERVAL = _FLASH_DURATION_MS // _FLASH_FADE_STEPS

VALID_POSITIONS = (
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lerp_color(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# ListeningIndicator
# ---------------------------------------------------------------------------

class ListeningIndicator(QWidget):
    """Always-on-top, click-through pill overlay — PySide6 implementation."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Display state
        self._mode_text    = "Hold"
        self._listening    = False
        self._snoozed      = False
        self._corner       = "bottom-center"
        self._command_mode = False

        # Pulse state
        self._pulse_step      = 0
        self._pulse_direction = 1
        self._pulse_timer     = QTimer(self)
        self._pulse_timer.setInterval(_PULSE_INTERVAL_MS)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        # Flash state
        self._flash_bg    = None
        self._flash_fg    = None
        self._flash_step  = 0
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(_FLASH_STEP_INTERVAL)
        self._flash_timer.timeout.connect(self._flash_tick)

        self.resize(_PILL_MIN_W, _PILL_H)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self):
        self._reposition()
        super().show()
        if self._listening:
            self._pulse_timer.start()

    def hide(self):
        self._pulse_timer.stop()
        self._flash_timer.stop()
        super().hide()

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        self._pulse_timer.stop()
        self._flash_timer.stop()
        super().hide()

    def set_mode(self, mode_str):
        self._mode_text = mode_str if mode_str else "Hold"
        if self.isVisible():
            self._reposition()
            self.update()

    def set_listening(self, active):
        if active == self._listening:
            return
        self._listening = active
        if not self.isVisible():
            return
        if active:
            self._pulse_step = 0
            self._pulse_direction = 1
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self._pulse_step = 0
        self._reposition()
        self.update()

    def set_snoozed(self, snoozed):
        if snoozed == self._snoozed:
            return
        self._snoozed = snoozed
        if self.isVisible():
            self._reposition()
            self.update()

    def set_command_mode(self, active):
        if active == self._command_mode:
            return
        self._command_mode = active
        if self.isVisible():
            self._reposition()
            self.update()

    def set_position(self, corner):
        if corner not in VALID_POSITIONS:
            corner = "bottom-center"
        self._corner = corner
        if self.isVisible():
            self._reposition()

    def flash_success(self):
        if self.isVisible():
            self._start_flash(_FLASH_SUCCESS_BG, _FLASH_SUCCESS_FG)

    def flash_error(self):
        if self.isVisible():
            self._start_flash(_FLASH_ERROR_BG, _FLASH_ERROR_FG)

    def flash_wake(self):
        if self.isVisible():
            self._start_flash(_TEAL, _LISTENING_FG)

    # ------------------------------------------------------------------
    # Mode-machine integration (optional; wired when samsara.mode merges)
    # ------------------------------------------------------------------

    def register_with_mode_machine(self, mode_machine):
        """Auto-update indicator on mode transitions when mode.py is available."""
        try:
            from samsara.mode import Mode
        except ImportError:
            return
        _state_map = {
            Mode.IDLE:       "Hold",
            Mode.HOLD:       "Dictating",
            Mode.TOGGLE:     "Dictating",
            Mode.CONTINUOUS: "Listening",
            Mode.WAKE:       "Listening",
            Mode.COMMAND:    "Command",
            Mode.AVA:        "Ava",
            Mode.STREAMING:  "Streaming",
        }
        def _on_mode_change(old, new):
            label = _state_map.get(new, "Hold")
            listening = new in (Mode.WAKE, Mode.CONTINUOUS,
                                Mode.HOLD, Mode.TOGGLE,
                                Mode.STREAMING)
            self.set_mode(label)
            self.set_listening(listening)
        mode_machine.register_listener(_on_mode_change)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _font(self) -> QFont:
        f = QFont("Segoe UI", 9)
        f.setBold(True)
        return f

    def _resolve_colors(self):
        """Return (bg_hex, fg_hex, label, show_dot) from current state."""
        t = self._pulse_step / _PULSE_STEPS

        if self._flash_bg is not None:
            if self._snoozed:
                label = "Snoozed"
            elif self._command_mode:
                label = "CMD"
            else:
                label = self._mode_text
            return self._flash_bg, self._flash_fg, label, False

        if self._snoozed:
            return _SNOOZE_BG, _SNOOZE_FG, "Snoozed", False

        if self._command_mode and self._listening:
            return _lerp_color(_CMD_BG, _CMD_ACTIVE_BG, t), _CMD_ACTIVE_FG, "CMD", False

        if self._command_mode:
            return _CMD_BG, _CMD_FG, "CMD", False

        if self._listening:
            return _lerp_color(_TEAL_DIM, _TEAL, t), _LISTENING_FG, self._mode_text, True

        return _IDLE_BG, _IDLE_FG, self._mode_text, False

    def _pill_width(self, label: str, show_dot: bool) -> int:
        fm = QFontMetrics(self._font())
        text_w = fm.horizontalAdvance(label)
        dot_reserve = _DOT_SPACE if show_dot else 0
        return max(_PILL_MIN_W, text_w + 2 * _PILL_PAD_X + dot_reserve)

    def _reposition(self):
        """Resize the widget to fit the current label and move it to the corner."""
        _, _, label, show_dot = self._resolve_colors()
        pill_w = self._pill_width(label, show_dot)
        self.resize(pill_w, _PILL_H)

        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        wa_x, wa_y = geom.x(), geom.y()
        wa_w, wa_h = geom.width(), geom.height()
        m = _EDGE_MARGIN
        cx = wa_x + (wa_w - pill_w) // 2

        corner = self._corner
        if corner == "top-left":
            x, y = wa_x + m, wa_y + m
        elif corner == "top-center":
            x, y = cx, wa_y + m
        elif corner == "top-right":
            x, y = wa_x + wa_w - pill_w - m, wa_y + m
        elif corner == "bottom-left":
            x, y = wa_x + m, wa_y + wa_h - _PILL_H - m
        elif corner == "bottom-right":
            x, y = wa_x + wa_w - pill_w - m, wa_y + wa_h - _PILL_H - m
        else:  # bottom-center default
            x, y = cx, wa_y + wa_h - _PILL_H - m

        self.move(x, y)

    def paintEvent(self, event):
        bg_hex, fg_hex, label, show_dot = self._resolve_colors()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Pill background
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, _CORNER_R, _CORNER_R)
        painter.fillPath(path, QColor(bg_hex))

        fg = QColor(fg_hex)

        # Dot indicator (shows when listening)
        if show_dot:
            painter.setBrush(fg)
            painter.setPen(Qt.PenStyle.NoPen)
            cx = float(_DOT_X)
            cy = float(self.height()) / 2.0
            painter.drawEllipse(
                QPointF(cx, cy),
                float(_DOT_R),
                float(_DOT_R),
            )

        # Text
        painter.setPen(fg)
        painter.setFont(self._font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.end()

    # ------------------------------------------------------------------
    # Pulse animation — Qt timer on main event loop (no background thread)
    # ------------------------------------------------------------------

    def _pulse_tick(self):
        if not self._listening or not self.isVisible():
            self._pulse_timer.stop()
            return
        self._pulse_step += self._pulse_direction
        if self._pulse_step >= _PULSE_STEPS:
            self._pulse_step = _PULSE_STEPS
            self._pulse_direction = -1
        elif self._pulse_step <= 0:
            self._pulse_step = 0
            self._pulse_direction = 1
        self.update()

    # ------------------------------------------------------------------
    # Flash animation — single-shot QTimer chain
    # ------------------------------------------------------------------

    def _start_flash(self, bg: str, fg: str):
        self._flash_timer.stop()
        self._flash_bg   = bg
        self._flash_fg   = fg
        self._flash_step = 0
        self._reposition()
        self.update()
        self._flash_timer.start()

    def _flash_tick(self):
        self._flash_step += 1
        if self._flash_step >= _FLASH_FADE_STEPS or self._flash_bg is None:
            self._flash_bg = None
            self._flash_fg = None
            self._reposition()
            self.update()
            return

        t = self._flash_step / _FLASH_FADE_STEPS
        if self._snoozed:
            target_bg, target_fg = _SNOOZE_BG, _SNOOZE_FG
        elif self._command_mode:
            target_bg, target_fg = _CMD_BG, _CMD_FG
        elif self._listening:
            target_bg = _lerp_color(_TEAL_DIM, _TEAL, 0.5)
            target_fg = _LISTENING_FG
        else:
            target_bg, target_fg = _IDLE_BG, _IDLE_FG

        self._flash_bg = _lerp_color(self._flash_bg, target_bg, t)
        self._flash_fg = _lerp_color(self._flash_fg, target_fg, t)
        self.update()
        self._flash_timer.start()
