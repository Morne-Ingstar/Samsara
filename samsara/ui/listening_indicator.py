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
  set_session_mode(name: str | None, color: str | None)  -- unified session
      (COMMAND/DICTATE/AVA) badge; takes priority over all other display
      state while non-None. Visibility is NOT this method's concern --
      dictation.py force-shows the pill for the session's duration and
      restores config-controlled visibility on session end.
  set_position(corner: str)
  set_custom_position(screen_name: str, cx: float, cy: float)
  enter_move_mode() / exit_move_mode(cancel: bool)
  flash_success() / flash_error() / flash_wake()

Move mode (drag-to-reposition):
  The tray's "Move listening indicator..." action calls enter_move_mode(),
  which drops WindowTransparentForInput so the pill can be left-dragged,
  without activating the window or stealing focus (WindowDoesNotAcceptFocus
  stays set throughout). Releasing the mouse clamps the drop point to its
  monitor's available geometry, stores it as a normalized center + screen
  identity (not raw pixels -- see set_custom_position), restores click-
  through, and emits placement_committed. A right-click while unlocked
  offers the six presets plus "Cancel move". dictation.py owns persisting
  the emitted placement to config.
"""

import logging
import math

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

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
_VISION_BG        = "#2d0050"
_VISION_BG_BRIGHT = "#4a0080"
_VISION_FG        = "#cc88ff"

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


def _clamp_rect(x: int, y: int, w: int, h: int, geom) -> tuple:
    """Clamp a w x h rect at (x, y) so it lies fully inside geom (a QRect)."""
    max_x = geom.x() + max(geom.width() - w, 0)
    max_y = geom.y() + max(geom.height() - h, 0)
    x = min(max(x, geom.x()), max_x)
    y = min(max(y, geom.y()), max_y)
    return x, y


# ---------------------------------------------------------------------------
# ListeningIndicator
# ---------------------------------------------------------------------------

class ListeningIndicator(QWidget):
    """Always-on-top, click-through pill overlay — PySide6 implementation."""

    # Emitted once a placement change is committed (drag release, or a
    # preset chosen from the move-mode right-click menu). Payload is either
    # {'type': 'custom', 'screen': str, 'cx': float, 'cy': float} or
    # {'type': 'preset', 'position': str}. The widget does not persist this
    # itself -- dictation.py owns config and the existing save path.
    placement_committed = Signal(dict)

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
        self._thinking     = False

        # Custom drag-to-position placement. None means "use self._corner".
        # {'screen': QScreen.name(), 'cx': 0..1, 'cy': 0..1} -- a screen
        # identity plus the pill's normalized center within that screen's
        # available geometry, NOT absolute pixels. See _apply_static_position
        # / _resolve_custom_screen for how this survives DPI, resolution,
        # taskbar, and label-width changes.
        self._custom_position = None

        # Move-mode ("unlocked") state. Entered via the tray's "Move
        # listening indicator..." action -- temporarily drops click-through
        # so the pill can be left-dragged. See enter_move_mode/exit_move_mode.
        self._unlocked              = False
        self._dragging               = False
        self._drag_offset            = None
        self._was_hidden_before_move = False
        self._pre_move_corner        = None
        self._pre_move_custom        = None

        # Unified session mode badge (COMMAND/DICTATE/AVA) -- set by
        # samsara.session_modes via dictation.py._update_mode_overlay().
        # Takes priority over every other display state while non-None;
        # None means no session is active, fall through to normal states.
        self._session_mode_name  = None
        self._session_mode_color = None

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
        self._thinking = False
        self._pulse_timer.stop()
        self._flash_timer.stop()
        self._force_lock()
        super().hide()

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        self._thinking = False
        self._pulse_timer.stop()
        self._flash_timer.stop()
        self._force_lock()
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

    def set_session_mode(self, name, color):
        """Unified session mode badge (COMMAND/DICTATE/AVA). name=None (with
        color=None) clears it, returning to normal listening-state display.
        Does NOT change visibility -- callers force-show/restore visibility
        around the session's lifetime separately (see dictation.py
        enter_command_mode/exit_command_mode)."""
        if name == self._session_mode_name and color == self._session_mode_color:
            return
        self._session_mode_name = name
        self._session_mode_color = color
        if self.isVisible():
            self._reposition()
            self.update()

    def set_position(self, corner):
        if corner not in VALID_POSITIONS:
            corner = "bottom-center"
        self._corner = corner
        self._custom_position = None
        if self.isVisible():
            self._reposition()

    def set_custom_position(self, screen_name, cx, cy):
        """Restore a persisted custom placement (e.g. on app startup).

        screen_name is a QScreen.name() identity; cx/cy are the pill's
        normalized center within that screen's available geometry. If the
        screen is missing when actually positioning, _resolve_custom_screen
        falls back to the primary screen and clamps.

        cx/cy come from on-disk config and may be malformed (missing, null,
        non-numeric, NaN/inf) -- validate rather than let a bad file crash
        indicator init; an invalid call is a no-op, leaving whatever
        placement was already in effect.
        """
        try:
            cx = float(cx)
            cy = float(cy)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring malformed custom listening-indicator position: cx=%r cy=%r",
                cx, cy,
            )
            return
        if not math.isfinite(cx) or not math.isfinite(cy):
            logger.warning(
                "Ignoring non-finite custom listening-indicator position: cx=%r cy=%r",
                cx, cy,
            )
            return
        self._custom_position = {
            'screen': screen_name if isinstance(screen_name, str) else None,
            'cx': min(max(cx, 0.0), 1.0),
            'cy': min(max(cy, 0.0), 1.0),
        }
        if self.isVisible():
            self._reposition()

    # ------------------------------------------------------------------
    # Move mode -- temporary drag-to-reposition
    # ------------------------------------------------------------------

    def enter_move_mode(self):
        """Temporarily disable click-through so the pill can be dragged.

        Does not activate the window or steal keyboard focus -- Qt.
        WindowDoesNotAcceptFocus stays set throughout, and WA_ShowWithout
        Activating (set in __init__) governs the re-show below.
        """
        if self._unlocked:
            return
        self._pre_move_corner = self._corner
        self._pre_move_custom = dict(self._custom_position) if self._custom_position else None
        self._was_hidden_before_move = not self.isVisible()
        if not self.isVisible():
            # Establish a sane on-screen position before it becomes visible
            # and draggable, rather than whatever default the window
            # manager last placed it at.
            self._reposition()
        self._unlocked = True
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, False)
        self._reposition()  # resize for the "Drag to move" label; position untouched
        self.update()
        super().show()

    def exit_move_mode(self, cancel=False):
        """Leave move mode, restoring click-through.

        cancel=True reverts to whatever placement was active before
        enter_move_mode() (used by the right-click "Cancel move" action and
        by hide()/destroy() safety paths). cancel=False keeps whatever
        placement is currently set (used after a drag/preset commit).
        """
        if not self._unlocked:
            return
        self._dragging = False
        if cancel:
            self._corner = self._pre_move_corner
            self._custom_position = self._pre_move_custom
        self._unlocked = False
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        if self._was_hidden_before_move:
            super().hide()
        else:
            self._reposition()
            super().show()
        self.update()

    def _force_lock(self):
        """Immediately restore click-through with no visibility round-trip.

        Used by hide()/destroy() so shutdown or a hidden indicator can never
        be left in an interactive, non-click-through state.
        """
        if not self._unlocked:
            return
        self._dragging = False
        self._unlocked = False
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)

    def _commit_drag_position(self):
        """Clamp the just-dragged position to its monitor, store it, and emit."""
        screen = QApplication.screenAt(self.mapToGlobal(self.rect().center()))
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            self.exit_move_mode(cancel=True)
            return
        geom = screen.availableGeometry()
        w, h = self.width(), self.height()
        x, y = _clamp_rect(self.x(), self.y(), w, h, geom)
        self.move(x, y)
        cx = ((x + w / 2.0) - geom.x()) / max(geom.width(), 1)
        cy = ((y + h / 2.0) - geom.y()) / max(geom.height(), 1)
        cx = min(max(cx, 0.0), 1.0)
        cy = min(max(cy, 0.0), 1.0)
        self._custom_position = {'screen': screen.name(), 'cx': cx, 'cy': cy}
        self._corner = None
        payload = {'type': 'custom', 'screen': screen.name(), 'cx': cx, 'cy': cy}
        self.exit_move_mode()
        self.placement_committed.emit(payload)

    def _choose_preset_from_menu(self, position):
        self._custom_position = None
        self._corner = position if position in VALID_POSITIONS else 'bottom-center'
        payload = {'type': 'preset', 'position': self._corner}
        self.exit_move_mode()
        self.placement_committed.emit(payload)

    def _resolve_custom_screen(self):
        """Screen matching the stored custom-position identity, falling
        back to the primary screen if it's no longer connected."""
        name = self._custom_position.get('screen') if self._custom_position else None
        if name:
            for scr in QApplication.screens():
                if scr.name() == name:
                    return scr
        return QApplication.primaryScreen()

    # ------------------------------------------------------------------
    # Mouse / context-menu handling -- only meaningful while unlocked;
    # normal click-through operation never delivers these events.
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if not self._unlocked or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self._drag_offset = event.globalPosition().toPoint() - self.pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._unlocked or not self._dragging:
            super().mouseMoveEvent(event)
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._unlocked or not self._dragging or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self._commit_drag_position()
        event.accept()

    def contextMenuEvent(self, event):
        if not self._unlocked:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        for position in VALID_POSITIONS:
            act = menu.addAction(position)
            act.triggered.connect(
                lambda checked=False, p=position: self._choose_preset_from_menu(p)
            )
        menu.addSeparator()
        cancel_act = menu.addAction("Cancel move")
        cancel_act.triggered.connect(lambda: self.exit_move_mode(cancel=True))
        menu.exec(event.globalPos())

    def set_thinking(self, active: bool):
        """Activate/deactivate the pulsing purple 'Vision' state."""
        if active == self._thinking:
            return
        self._thinking = active
        if not self.isVisible():
            return
        if active:
            self._pulse_step = 0
            self._pulse_direction = 1
            self._pulse_timer.start()
        else:
            if not self._listening:
                self._pulse_timer.stop()
                self._pulse_step = 0
        self._reposition()
        self.update()

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

        if self._unlocked:
            # Move mode dominates every other display state -- the user is
            # actively dragging the pill and needs an unambiguous cue.
            return _IDLE_BG, _TEAL, "Drag to move", False

        if self._flash_bg is not None:
            if self._snoozed:
                label = "Snoozed"
            elif self._command_mode:
                label = "CMD"
            else:
                label = self._mode_text
            return self._flash_bg, self._flash_fg, label, False

        if self._session_mode_name:
            # Unified session (COMMAND/DICTATE/AVA) dominates the generic
            # CMD/listening states while active -- it's strictly more
            # informative. A transient flash (above) still interrupts it
            # briefly for success/error confirmation.
            return _IDLE_BG, self._session_mode_color, self._session_mode_name, False

        if self._thinking:
            return _lerp_color(_VISION_BG, _VISION_BG_BRIGHT, t), _VISION_FG, "Vision", False

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
        """Resize the widget to fit the current label, then position it.

        While unlocked (move mode) the position is left alone -- the user
        is dragging it, or it's just been shown so it can be dragged.
        """
        self._resize_to_label()
        if self._unlocked:
            return
        self._apply_static_position()

    def _resize_to_label(self):
        _, _, label, show_dot = self._resolve_colors()
        self.resize(self._pill_width(label, show_dot), _PILL_H)

    def _apply_static_position(self):
        """Position the pill per self._custom_position (if set) or the
        preset self._corner. Called only when not unlocked."""
        pill_w, pill_h = self.width(), _PILL_H

        if self._custom_position is not None:
            screen = self._resolve_custom_screen()
            if screen is not None:
                geom = screen.availableGeometry()
                cx = geom.x() + self._custom_position['cx'] * geom.width()
                cy = geom.y() + self._custom_position['cy'] * geom.height()
                x = int(round(cx - pill_w / 2))
                y = int(round(cy - pill_h / 2))
                x, y = _clamp_rect(x, y, pill_w, pill_h, geom)
                self.move(x, y)
                return

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
            x, y = wa_x + m, wa_y + wa_h - pill_h - m
        elif corner == "bottom-right":
            x, y = wa_x + wa_w - pill_w - m, wa_y + wa_h - pill_h - m
        else:  # bottom-center default
            x, y = cx, wa_y + wa_h - pill_h - m

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

        # Unlocked (move-mode) outline -- obvious but tasteful drag affordance
        if self._unlocked:
            pen = QPen(QColor(_TEAL))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            outline_rect = rect.adjusted(1, 1, -1, -1)
            outline_path = QPainterPath()
            outline_path.addRoundedRect(outline_rect, _CORNER_R, _CORNER_R)
            painter.drawPath(outline_path)

        painter.end()

    # ------------------------------------------------------------------
    # Pulse animation — Qt timer on main event loop (no background thread)
    # ------------------------------------------------------------------

    def _pulse_tick(self):
        if (not self._listening and not self._thinking) or not self.isVisible():
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
