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
  show_outcome(label: str, kind: str, ttl_ms: int | None = 1800)
  clear_outcome()
  set_device_lost(lost: bool)

Outcome chip:
  The indicator is the one place the app says what just happened. Every
  dispatch outcome maps to one short chip in a single table,
  samsara.session_modes.outcome_chip -- which is the source of truth, and
  whose test enumerates every outcome kind from source so none can go
  unmapped. The colour is the meaning: green (SUCCESS) it worked -- a check
  mark plus the command ("switch window"), "typed", "undone"; red (ERROR) it
  failed -- "MISS", a cross plus the reason ("no audio"), and "REC" while a
  hold is live; amber (WARNING) it was deliberately refused -- "refused: focus
  lock", "Ava: nothing to do"; teal (ACCENT) a mode change or work still in
  progress -- an arrow plus the mode ("AVA"), "staged", "Ava" plus an
  ellipsis. A pending chip (staged, the Ava ellipsis, the ellipsis shown
  after releasing a hold) stays until the real outcome replaces it; every
  other chip clears after its TTL. The glyphs are built with chr() in
  session_modes (CHIP_CHECK etc.) so no source file carries them. The chip draws even
  when the indicator is switched off in settings, alone and only for its TTL,
  so a user who cannot hear the earcons still sees every result. A persistent
  "mic lost" chip, driven by the audio engine's DEVICE_LOST state, overrides
  the rest until the microphone reconnects.

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
import random

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from samsara.ui import theme
from samsara.ui.splash_qt import _system_reduced_motion as _reduced_motion
from samsara.ui.tray_qt import SPIN_SECONDS_PER_TURN, paint_mark

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outcome chip -- what just happened (see samsara.session_modes.outcome_chip)
# ---------------------------------------------------------------------------


#: Chip fill by kind. theme.py tokens only. pending/live share a colour with
#: accent/error and differ only in having no TTL.
def _chip_fill():
    return {
        "success": theme.SUCCESS,
        "error":   theme.ERROR,
        "warning": theme.WARNING,
        "accent":  theme.ACCENT,
        "pending": theme.ACCENT,
        "live":    theme.ERROR,
    }
#: Dark text on the saturated fill -- the highest-contrast pairing theme.py
#: offers (TEXT_ON_ACCENT is BG0), and the chip must be readable at a glance.
_CHIP_FONT_PX = theme.TYPE_MIN
_CHIP_H = 26
_CHIP_PAD_X = 12
_CHIP_GAP = 6
_CHIP_DEFAULT_TTL_MS = 1800
_NO_TTL_KINDS = frozenset({"pending", "live"})
_DEVICE_LOST_LABEL = "mic lost"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------


# theme.py tokens only (one accent, one semantic -- theme.py "Visual
# identity"). Pill fills are a token mixed toward BG0, never a new hue and
# never solid accent: the state glyph is the monochrome mark in the state
# colour and must stay visible on its own pill.
def _teal_dim():
    return theme._mix(theme.ACCENT, theme.BG0, 0.80)


def _teal_bright():
    return theme._mix(theme.ACCENT, theme.BG0, 0.62)


# Snoozed is "asleep": the closed-lid glyph and the label carry it.
def _cmd_bg():
    return theme._mix(theme.ACCENT, theme.BG0, 0.80)


def _cmd_active_bg():
    return theme._mix(theme.ACCENT, theme.BG0, 0.62)


def _flash_success_bg():
    return theme._mix(theme.SUCCESS, theme.BG0, 0.70)


def _flash_error_bg():
    return theme._mix(theme.ERROR, theme.BG0, 0.70)


def _vision_bg():
    return theme._mix(theme.AVA, theme.BG0, 0.80)


def _vision_bg_bright():
    return theme._mix(theme.AVA, theme.BG0, 0.62)
# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_PILL_H       = 36
_PILL_MIN_W   = 90
_PILL_PAD_X   = 22
_DOT_SPACE    = 26        # left reserve for the state glyph (the Samsara mark)
_GLYPH_PX     = 20
_GLYPH_X      = 10
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

# Idle life (09b3): the glyph BLINKS and GLANCES now and then while the wake
# listener is armed. Always under 1 s and always back to the same rest pose
# -- anything that lasts longer MEANS something (spin = thinking, closed lid
# = asleep, red = recording). Randomised intervals, so it never reads as a
# progress rhythm. Never in the tray.
_BLINK_MS            = 140              # lid closed, then reopens
_BLINK_INTERVAL_S    = (18.0, 45.0)
_GLANCE_MS           = 600              # eases out to _GLANCE_DEG and back
_GLANCE_DEG          = 14.0
_GLANCE_INTERVAL_S   = (40.0, 90.0)
_IDLE_FRAME_MS       = 33               # frame timer only runs during a blink/glance

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


def _reachable_rect(x: int, y: int, w: int, h: int, geom) -> tuple:
    """Clamp custom placement inside a taskbar-safe, reachable inset."""
    margin = min(
        _EDGE_MARGIN,
        max((geom.width() - w) // 2, 0),
        max((geom.height() - h) // 2, 0),
    )
    safe_geom = geom.adjusted(margin, margin, -margin, -margin)
    return _clamp_rect(x, y, w, h, safe_geom)


def reset_indicator_placement(app) -> bool:
    """Restore the indicator default through the shared recovery path."""
    config = getattr(app, "config", None)
    if not isinstance(config, dict):
        return False
    changes = {
        "listening_indicator_position": "bottom-center",
        "listening_indicator_custom_position": None,
    }
    update = getattr(app, "update_config_and_save", None)
    if callable(update):
        update(changes)
    else:
        config.update(changes)
    apply = getattr(app, "apply_listening_indicator_settings", None)
    if callable(apply):
        apply()
    return True


# ---------------------------------------------------------------------------
# ListeningIndicator
# ---------------------------------------------------------------------------

#: Indicator display states (ListeningIndicator.display_state). ARMED is the
#: quiet wake-listener state: eye open, slow turn, idle pill, no pulse.
STATE_ARMED = "armed"
#: The states that carry capture styling (lit pill, pulse, recording pace).
CAPTURE_STATES = frozenset({"capture", "command-capture"})


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
        self._flash_wake  = False
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(_FLASH_STEP_INTERVAL)
        self._flash_timer.timeout.connect(self._flash_tick)

        # Outcome chip. _chip_label None means no chip. _chip_only is True
        # when the chip had to show the widget itself because the indicator
        # was hidden (listening_indicator_enabled False): the pill body is
        # then not drawn, and the widget hides again when the chip clears.
        self._chip_label = None
        self._chip_kind = None
        self._chip_only = False
        self._chip_timer = QTimer(self)
        self._chip_timer.setSingleShot(True)
        self._chip_timer.timeout.connect(self._clear_outcome)
        # Persistent "mic lost" chip, keyed on the audio engine's DEVICE_LOST
        # state. Wins over any transient outcome chip while set.
        self._device_lost = False

        # Wake listener armed (open eye) -- set by dictation.py.
        self._wake_armed = False

        # Idle life: blink + glance (see _BLINK_* / _GLANCE_*). Two
        # single-shot schedulers at random intervals, plus a frame timer
        # that runs ONLY while a blink or glance is in flight.
        self._clock = QElapsedTimer()
        self._clock.start()
        self._idle_enabled = True          # config ui.idle_animation
        self._idle_rng = random.Random()
        self._blink_started_ms = None
        self._glance_started_ms = None
        self._blink_timer = QTimer(self)
        self._blink_timer.setSingleShot(True)
        self._blink_timer.timeout.connect(self._start_blink)
        self._glance_timer = QTimer(self)
        self._glance_timer.setSingleShot(True)
        self._glance_timer.timeout.connect(self._start_glance)
        self._idle_frame_timer = QTimer(self)
        self._idle_frame_timer.setInterval(_IDLE_FRAME_MS)
        self._idle_frame_timer.timeout.connect(self._idle_frame)

        self.resize(_PILL_MIN_W, _PILL_H)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self):
        # An explicit show() means the pill itself is wanted -- leave
        # chip-only mode (any active chip now draws beside the pill).
        self._chip_only = False
        self._reposition()
        super().show()
        if self._listening or self._thinking:
            self._pulse_timer.start()
        self._state_changed()

    def hide(self):
        self._thinking = False
        self._pulse_timer.stop()
        self._cancel_idle()
        self._flash_timer.stop()
        self._force_lock()
        if self._device_lost:
            # The persistent "mic lost" chip outlives the pill being hidden:
            # it is the explanation for every capture about to fail.
            self._chip_timer.stop()
            self._chip_label = None
            self._chip_kind = None
            self._chip_only = True
            self._reposition()
            self.update()
            return
        self._chip_timer.stop()
        self._chip_label = None
        self._chip_kind = None
        self._chip_only = False
        super().hide()

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        self._thinking = False
        self._pulse_timer.stop()
        self._cancel_idle()
        self._flash_timer.stop()
        self._chip_timer.stop()
        self._chip_label = None
        self._chip_kind = None
        self._chip_only = False
        self._device_lost = False
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
        self._state_changed()
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
        self._state_changed()
        if self.isVisible():
            self._reposition()
            self.update()

    def set_command_mode(self, active):
        if active == self._command_mode:
            return
        self._command_mode = active
        self._state_changed()
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
        self._state_changed()
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
        normalized_name = screen_name if isinstance(screen_name, str) else None
        if normalized_name and not any(
                screen.name() == normalized_name for screen in QApplication.screens()):
            logger.warning(
                "Listening-indicator screen %r is unavailable; restoring default position.",
                normalized_name,
            )
            self.set_position("bottom-center")
            return
        self._custom_position = {
            'screen': normalized_name,
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
        x, y = _reachable_rect(self.x(), self.y(), w, h, geom)
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
            logger.warning(
                "Listening-indicator screen %r disappeared; restoring default position.",
                name,
            )
            self._custom_position = None
            self._corner = "bottom-center"
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
        self._state_changed()
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
            self._flash_wake = False
            self._start_flash(_flash_success_bg(), theme.SUCCESS)

    def flash_error(self):
        if self.isVisible():
            self._flash_wake = False
            self._start_flash(_flash_error_bg(), theme.ERROR)

    def flash_wake(self):
        if self.isVisible():
            # The glyph plays the heard frame (red eye, bright ring) while
            # this flash runs -- see tray_qt.HEARD_KEYFRAMES.
            self._flash_wake = True
            self._start_flash(_teal_bright(), theme.ACCENT)

    def set_wake_armed(self, armed: bool):
        """Wake listener armed: the glyph's eye is open (and may blink)."""
        armed = bool(armed)
        if armed == self._wake_armed:
            return
        self._wake_armed = armed
        self._state_changed()
        if self.isVisible():
            self.update()

    def set_idle_animation(self, enabled: bool):
        """Config ui.idle_animation: blink and glance on/off. Never affects
        state animation (spin, pulse, heard flash)."""
        self._idle_enabled = bool(enabled)
        self._state_changed()

    # ------------------------------------------------------------------
    # Idle life: blink + glance
    # ------------------------------------------------------------------

    def _now_ms(self) -> int:
        return self._clock.elapsed()

    def _spin(self, pace: str) -> float:
        """Continuous clock-driven rotation (degrees) at a SPIN_SECONDS_PER_TURN pace."""
        turn_ms = SPIN_SECONDS_PER_TURN[pace] * 1000.0
        return 360.0 * (self._now_ms() % turn_ms) / turn_ms

    def _capture_pace(self):
        """The pace the ring turns at, or None at rest.
        Motion means capture (queue 19): a live capture (hold, toggle,
        continuous, a latched session, the REC chip) turns at the recording
        pace; the wake listener merely ARMED turns slowly. A capture beats
        armed (42): capturing while armed is capturing."""
        if self._chip_kind == "live" or self._listening:
            return "recording"
        if self._wake_armed:
            return "armed"
        return None

    def display_state(self) -> str:
        """The one name for what the pill is showing now (42), in the same
        priority order the paint uses (_resolve_colors / _base_glyph):
          move, heard, flash, session, thinking, snoozed,
          command-capture, command, capture, armed, idle.
        CAPTURE_STATES are the ones with capture styling; ARMED is not one."""
        if self._unlocked:
            return "move"
        if self._flash_bg is not None:
            return "heard" if self._flash_wake else "flash"
        if self._session_mode_name:
            return "session"
        if self._thinking:
            return "thinking"
        if self._snoozed:
            return "snoozed"
        if self._command_mode:
            return "command-capture" if self._listening else "command"
        if self._listening or self._chip_kind == "live":
            return "capture"
        if self._wake_armed:
            return STATE_ARMED
        return "idle"

    def _base_glyph(self):
        """The state glyph without idle overlays: (capture, eye, rotation, opacity).
        Opacity is always 1: the listening pulse was retired in favour of
        rotation (queue 19); the ring is still only at rest."""
        if self._unlocked:
            return "idle", "off", 0.0, 1.0
        if self._flash_bg is not None and self._flash_wake:
            return "listening", "heard", 0.0, 1.0
        pace = self._capture_pace()
        if self._session_mode_name:
            capture = "ava" if str(self._session_mode_name).upper() == "AVA" else "listening"
            return capture, "off", self._spin(pace) if pace else 0.0, 1.0
        if self._thinking:
            return "ava", "off", self._spin("thinking"), 1.0
        if self._snoozed:
            return "idle", "asleep", 0.0, 1.0
        rotation = self._spin(pace) if pace else 0.0
        if self._wake_armed and not self._command_mode:
            return "listening", "armed", rotation, 1.0
        if self._command_mode or self._listening or pace:
            return "listening", "off", rotation, 1.0
        return "idle", "off", 0.0, 1.0

    def _spinning(self) -> bool:
        return self._thinking or self._capture_pace() is not None

    def _idle_allowed(self) -> bool:
        """Blink/glance only on a visible, unminimised pill whose eye is open
        (armed), with idle animation enabled and the OS allowing motion.
        Never while asleep/off, recording (the live chip), a heard flash,
        move mode, or chip-only mode."""
        if not self._idle_enabled or _reduced_motion():
            return False
        if not self.isVisible() or self.isMinimized() or self._chip_only:
            return False
        if self._unlocked or self._flash_bg is not None or self._chip_kind == "live":
            return False
        return self._base_glyph()[1] == "armed"

    def _cancel_idle(self):
        """Stop any in-flight blink/glance at once and clear the schedule."""
        self._blink_timer.stop()
        self._glance_timer.stop()
        self._idle_frame_timer.stop()
        was_animating = self._blink_started_ms is not None or self._glance_started_ms is not None
        self._blink_started_ms = None
        self._glance_started_ms = None
        if was_animating and self.isVisible():
            self.update()

    def _schedule_idle(self):
        if not self._idle_allowed():
            self._blink_timer.stop()
            self._glance_timer.stop()
            return
        if not self._blink_timer.isActive() and self._blink_started_ms is None:
            self._blink_timer.start(int(self._idle_rng.uniform(*_BLINK_INTERVAL_S) * 1000))
        if self._spinning():
            self._glance_timer.stop()          # no rest pose to tilt from (queue 19)
        elif not self._glance_timer.isActive() and self._glance_started_ms is None:
            self._glance_timer.start(int(self._idle_rng.uniform(*_GLANCE_INTERVAL_S) * 1000))

    def _state_changed(self):
        """A real state change cancels idle motion immediately, then
        reschedules only if the new state allows it."""
        self._cancel_idle()
        self._schedule_idle()
        # A capture state that turns the ring needs the frame clock running.
        if self.isVisible() and self._spinning() and not self._pulse_timer.isActive():
            self._pulse_timer.start()

    def _start_blink(self):
        if not self._idle_allowed():
            self._schedule_idle()
            return
        self._blink_started_ms = self._now_ms()
        self._idle_frame_timer.start()
        self.update()

    def _start_glance(self):
        # A glance is a tilt that returns to a REST pose; while the ring turns
        # (every capture state, queue 19) there is no rest pose, so it is
        # suppressed. The blink still plays.
        if not self._idle_allowed() or self._spinning():
            self._schedule_idle()
            return
        self._glance_started_ms = self._now_ms()
        self._idle_frame_timer.start()
        self.update()

    def _idle_frame(self):
        now = self._now_ms()
        if not self._idle_allowed():
            self._cancel_idle()
            self._schedule_idle()
            return
        if self._blink_started_ms is not None and now - self._blink_started_ms >= _BLINK_MS:
            self._blink_started_ms = None
        if self._glance_started_ms is not None and now - self._glance_started_ms >= _GLANCE_MS:
            self._glance_started_ms = None
        if self._blink_started_ms is None and self._glance_started_ms is None:
            self._idle_frame_timer.stop()
            self._schedule_idle()
        self.update()

    def _glance_angle(self) -> float:
        """0 -> _GLANCE_DEG -> 0 over _GLANCE_MS (ease out and back)."""
        if self._glance_started_ms is None:
            return 0.0
        t = (self._now_ms() - self._glance_started_ms) / _GLANCE_MS
        if t >= 1.0 or t < 0.0:
            return 0.0
        return _GLANCE_DEG * math.sin(math.pi * t)

    # ------------------------------------------------------------------
    # Outcome chip
    # ------------------------------------------------------------------

    def show_outcome(self, label: str, kind: str, ttl_ms: "int | None" = _CHIP_DEFAULT_TTL_MS):
        """Show a short chip saying what just happened.

        A new call replaces the current chip. `pending` and `live` kinds have
        no TTL and stay until replaced; every other kind clears after
        `ttl_ms`. Colours are theme.py tokens by kind.

        Renders even when the indicator is hidden by config
        (listening_indicator_enabled False): the widget shows in chip-only
        mode -- no pill body -- for the chip's lifetime, then hides again.
        That is the whole point for a user who cannot hear the earcons.
        Never takes focus: this widget's window flags already forbid it.
        """
        if not label:
            self.clear_outcome()
            return
        kind = kind if kind in _chip_fill() else "warning"
        self._chip_label = str(label)
        self._chip_kind = kind
        self._chip_timer.stop()
        self._state_changed()   # e.g. the "live" (recording) chip stops idle motion

        if not self.isVisible():
            self._chip_only = True
            self._reposition()
            super().show()
        else:
            self._reposition()
        self.update()

        if kind not in _NO_TTL_KINDS and ttl_ms:
            self._chip_timer.start(int(ttl_ms))

    def clear_outcome(self):
        """Remove the transient chip (the device-lost chip is unaffected)."""
        self._chip_timer.stop()
        self._clear_outcome()

    def set_device_lost(self, lost: bool):
        """Persistent "mic lost" chip, keyed on the audio engine's DEVICE_LOST
        state (AudioCaptureEngine.device_lost). Stays until cleared by a
        successful reopen; overrides any transient chip while set, since it
        is the reason every other action is about to fail."""
        lost = bool(lost)
        if lost == self._device_lost:
            return
        self._device_lost = lost
        if lost and not self.isVisible():
            self._chip_only = True
            self._reposition()
            super().show()
        elif not lost and self._chip_only and self._chip_label is None:
            self._chip_only = False
            super().hide()
            return
        if self.isVisible():
            self._reposition()
            self.update()

    def _active_chip(self) -> "tuple[str, str] | None":
        """(label, kind) actually drawn, or None. Device-lost wins."""
        if self._unlocked:
            return None  # move mode dominates, as it does for the pill
        if self._device_lost:
            return (_DEVICE_LOST_LABEL, "error")
        if self._chip_label:
            return (self._chip_label, self._chip_kind or "warning")
        return None

    def _clear_outcome(self):
        self._chip_label = None
        self._chip_kind = None
        self._state_changed()
        if self._chip_only and not self._device_lost:
            # The chip was the only reason this widget was on screen.
            self._chip_only = False
            super().hide()
            return
        if self.isVisible():
            self._reposition()
            self.update()

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
        f = theme.qfont(theme.TYPE_MIN)
        f.setBold(True)
        return f

    def _resolve_colors(self):
        """Return (bg_hex, fg_hex, label, show_glyph) from current state.

        Every pill state shows the state glyph (the Samsara mark, see
        _glyph_mark); the 4th element stays for layout callers.
        """
        t = self._pulse_step / _PULSE_STEPS

        if self._unlocked:
            # Move mode dominates every other display state -- the user is
            # actively dragging the pill and needs an unambiguous cue.
            return theme.BG1, theme.ACCENT, "Drag to move", True

        if self._flash_bg is not None:
            if self._snoozed:
                label = "Snoozed"
            elif self._command_mode:
                label = "CMD"
            else:
                label = self._mode_text
            return self._flash_bg, self._flash_fg, label, True

        if self._session_mode_name:
            # Unified session (COMMAND/DICTATE/AVA) dominates the generic
            # CMD/listening states while active -- it's strictly more
            # informative. A transient flash (above) still interrupts it
            # briefly for success/error confirmation.
            return theme.BG1, self._session_mode_color, self._session_mode_name, True

        if self._thinking:
            return _lerp_color(_vision_bg(), _vision_bg_bright(), t), theme.AVA, "Vision", True

        if self._snoozed:
            return theme.BG1, theme.ICON_IDLE, "Snoozed", True

        if self._command_mode and self._listening:
            return _lerp_color(_cmd_bg(), _cmd_active_bg(), t), theme.ACCENT_HOVER, "CMD", True

        if self._command_mode:
            return _cmd_bg(), theme.ACCENT, "CMD", True

        if self._listening:
            return _lerp_color(_teal_dim(), _teal_bright(), t), theme.ACCENT, self._mode_text, True

        return theme.BG1, theme.ICON_IDLE, self._mode_text, True

    def _glyph_mark(self):
        """(capture, eye, rotation_deg, opacity) for the pill's state glyph --
        the same mark the tray draws (tray_qt.paint_mark), at pill size.

        Motion on the same drawing: the ring turns in every capture state at
        its SPIN_SECONDS_PER_TURN pace (thinking, armed, recording) and is
        still at rest. Only in the armed (open-eye) state the idle blink
        closes the lid; the idle glance tilts a ring that is NOT turning.
        """
        capture, eye, rotation, opacity = self._base_glyph()
        if eye == "armed":
            if self._blink_started_ms is not None:
                eye = "asleep"
            if not self._spinning():
                rotation += self._glance_angle()
        return capture, eye, rotation, opacity

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

    def _chip_font(self) -> QFont:
        f = QFont("Segoe UI")
        f.setPixelSize(theme.TYPE_MIN)
        f.setBold(True)
        return f

    def _chip_width(self, label: str) -> int:
        return QFontMetrics(self._chip_font()).horizontalAdvance(label) + 2 * _CHIP_PAD_X

    def _layout(self):
        """(total_w, total_h, pill_rect|None, chip_rect|None) in widget coords.

        The chip sits below a top-anchored pill and above a bottom-anchored
        one, so it always grows AWAY from the screen edge the pill hugs and
        the pill itself never moves when a chip appears.
        """
        _, _, label, show_dot = self._resolve_colors()
        pill_w = self._pill_width(label, show_dot)
        chip = self._active_chip()
        chip_w = self._chip_width(chip[0]) if chip else 0

        if chip is None:
            return pill_w, _PILL_H, QRectF(0, 0, pill_w, _PILL_H), None
        if self._chip_only:
            return chip_w, _CHIP_H, None, QRectF(0, 0, chip_w, _CHIP_H)

        total_w = max(pill_w, chip_w)
        total_h = _PILL_H + _CHIP_GAP + _CHIP_H
        # Align both to the edge the pill is anchored to. Centring a chip
        # wider than the pill would make the widget overhang the screen at a
        # left/right corner, and the clamp would then drag the pill inward.
        align = self._chip_h_align()
        if align == "left":
            pill_x, chip_x = 0.0, 0.0
        elif align == "right":
            pill_x, chip_x = float(total_w - pill_w), float(total_w - chip_w)
        else:
            # Whole pixels: a half-pixel pill offset rounded the widget one
            # way and the pill the other, moving the pill 1 px when a chip
            # appeared (odd width difference).
            pill_x = float((total_w - pill_w) // 2)
            chip_x = float((total_w - chip_w) // 2)
        if self._chip_goes_above():
            chip_rect = QRectF(chip_x, 0, chip_w, _CHIP_H)
            pill_rect = QRectF(pill_x, _CHIP_H + _CHIP_GAP, pill_w, _PILL_H)
        else:
            pill_rect = QRectF(pill_x, 0, pill_w, _PILL_H)
            chip_rect = QRectF(chip_x, _PILL_H + _CHIP_GAP, chip_w, _CHIP_H)
        return total_w, total_h, pill_rect, chip_rect

    def _chip_h_align(self) -> str:
        """'left' / 'right' / 'center' -- which edge the pill hugs."""
        if self._custom_position is not None:
            cx = self._custom_position.get('cx', 0.5)
            if cx < 1 / 3:
                return "left"
            if cx > 2 / 3:
                return "right"
            return "center"
        corner = str(self._corner or "")
        if corner.endswith("left"):
            return "left"
        if corner.endswith("right"):
            return "right"
        return "center"

    def _chip_goes_above(self) -> bool:
        """True when the pill is in the lower half of its screen."""
        if self._custom_position is not None:
            return self._custom_position.get('cy', 1.0) >= 0.5
        return not str(self._corner or "").startswith("top")

    def _resize_to_label(self):
        total_w, total_h, _, _ = self._layout()
        self.resize(int(total_w), int(total_h))

    def _pill_anchor(self, pill_w: int):
        """Where the PILL's top-left goes, exactly as before the chip existed.
        Returns (x, y, geom) or None when there is no screen."""
        pill_h = _PILL_H
        if self._custom_position is not None:
            screen = self._resolve_custom_screen()
            if screen is not None:
                geom = screen.availableGeometry()
                cx = geom.x() + self._custom_position['cx'] * geom.width()
                cy = geom.y() + self._custom_position['cy'] * geom.height()
                x = int(round(cx - pill_w / 2))
                y = int(round(cy - pill_h / 2))
                x, y = _reachable_rect(x, y, pill_w, pill_h, geom)
                return x, y, geom

        screen = QApplication.primaryScreen()
        if screen is None:
            return None
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
        return x, y, geom

    def _apply_static_position(self):
        """Position per self._custom_position (if set) or the preset
        self._corner. Called only when not unlocked.

        The pill's on-screen position is computed exactly as it always was;
        the widget is then offset so the pill stays put and any chip grows
        away from the anchored edge. In chip-only mode the chip takes the
        pill's place, so it follows the same placement and move-mode logic.
        """
        _, _, label, show_dot = self._resolve_colors()
        pill_w = self._pill_width(label, show_dot)
        anchor = self._pill_anchor(pill_w)
        if anchor is None:
            return
        pill_x, pill_y, geom = anchor
        total_w, total_h, pill_rect, chip_rect = self._layout()

        if pill_rect is None:  # chip-only: centre the chip on the pill's spot
            x = int(round(pill_x + (pill_w - total_w) / 2.0))
            y = int(round(pill_y + (_PILL_H - total_h) / 2.0))
        else:
            x = int(round(pill_x - pill_rect.x()))
            y = int(round(pill_y - pill_rect.y()))
        clamp = _reachable_rect if self._custom_position is not None else _clamp_rect
        x, y = clamp(x, y, int(total_w), int(total_h), geom)
        self.move(x, y)

    def paintEvent(self, event):
        bg_hex, fg_hex, label, show_dot = self._resolve_colors()
        _, _, pill_rect, chip_rect = self._layout()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if pill_rect is not None:
            self._paint_pill(painter, pill_rect, bg_hex, fg_hex, label, show_dot)

        chip = self._active_chip()
        if chip_rect is not None and chip is not None:
            self._paint_chip(painter, chip_rect, *chip)

        painter.end()

    def _paint_pill(self, painter, rect, bg_hex, fg_hex, label, show_dot):
        path = QPainterPath()
        path.addRoundedRect(rect, _CORNER_R, _CORNER_R)
        painter.fillPath(path, QColor(bg_hex))

        fg = QColor(fg_hex)

        text_rect = rect
        if show_dot:
            # State glyph: the Samsara mark (shared routine, tray_qt.paint_mark).
            capture, eye, rotation, opacity = self._glyph_mark()
            glyph = QRectF(rect.x() + _GLYPH_X,
                           rect.y() + (rect.height() - _GLYPH_PX) / 2.0,
                           _GLYPH_PX, _GLYPH_PX)
            paint_mark(painter, glyph, capture, eye, rotation, opacity)
            text_rect = rect.adjusted(_DOT_SPACE, 0, 0, 0)

        painter.setPen(fg)
        painter.setFont(self._font())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, label)

        # Unlocked (move-mode) outline -- obvious but tasteful drag affordance
        if self._unlocked:
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            outline_path = QPainterPath()
            outline_path.addRoundedRect(rect.adjusted(1, 1, -1, -1), _CORNER_R, _CORNER_R)
            painter.drawPath(outline_path)

    def _paint_chip(self, painter, rect, label, kind):
        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(_chip_fill().get(kind, theme.WARNING)))
        painter.setPen(QColor(theme.TEXT_ON_ACCENT))
        painter.setFont(self._chip_font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # ------------------------------------------------------------------
    # Pulse animation — Qt timer on main event loop (no background thread)
    # ------------------------------------------------------------------

    def _pulse_tick(self):
        # Also the frame clock for the turning ring (_spinning): it runs while
        # anything is capturing and stops at rest, so an idle pill never wakes.
        if (not self._listening and not self._spinning()) or not self.isVisible():
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
        self._state_changed()   # a flash is a state change: idle motion stops now
        self._reposition()
        self.update()
        self._flash_timer.start()

    def _flash_tick(self):
        self._flash_step += 1
        if self._flash_step >= _FLASH_FADE_STEPS or self._flash_bg is None:
            self._flash_bg = None
            self._flash_fg = None
            self._flash_wake = False
            self._state_changed()
            self._reposition()
            self.update()
            return

        t = self._flash_step / _FLASH_FADE_STEPS
        if self._snoozed:
            target_bg, target_fg = theme.BG1, theme.ICON_IDLE
        elif self._command_mode:
            target_bg, target_fg = _cmd_bg(), theme.ACCENT
        elif self._listening:
            target_bg = _lerp_color(_teal_dim(), _teal_bright(), 0.5)
            target_fg = theme.ACCENT
        else:
            target_bg, target_fg = theme.BG1, theme.ICON_IDLE

        self._flash_bg = _lerp_color(self._flash_bg, target_bg, t)
        self._flash_fg = _lerp_color(self._flash_fg, target_fg, t)
        self.update()
        self._flash_timer.start()
