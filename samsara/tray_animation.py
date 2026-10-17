"""Tray frame descriptions and animation using the app's existing state.

Qt rendering stays in ui.tray_qt; these callbacks only describe MarkFrames.
DictationApp keeps the public facade and owns reasons, rotation and timers.
"""

import logging
import math

from samsara.constants import ICON_TICK_FAST, ICON_TICK_MEDIUM, ICON_TICK_SLOW
from samsara.runtime import thread_registry

logger = logging.getLogger("Samsara")


def create_icon_image(self, rotation=0.0, opacity=1.0):
    """The tray frame for the live state: a tray_qt.MarkFrame.

    Only describes the frame -- tray_qt renders it with render_mark on
    the Qt thread, so this is safe from the icon timer's worker thread.
    rotation is in radians (the chase timer's unit).
    """
    from samsara.ui.tray_qt import MarkFrame
    capture, eye = self._tray_mark()
    return MarkFrame(capture, eye, math.degrees(rotation), opacity)

def _push_tray_icon(self):
    """Show the live tray state now (outside an animation tick)."""
    if not hasattr(self, 'tray_icon'):
        return
    try:
        self.tray_icon.icon = self.create_icon_image(
            rotation=getattr(self, '_icon_rotation', 0.0))
    except OSError as e:
        logger.debug(f"Tray icon state swap failed: {e}")

def _flash_tray_heard(self):
    """Wake phrase heard: play tray_qt.HEARD_KEYFRAMES on the tray eye."""
    from samsara.ui.tray_qt import HEARD_KEYFRAMES
    self._tray_heard_generation = getattr(self, '_tray_heard_generation', 0) + 1
    generation = self._tray_heard_generation

    def _step(index):
        if generation != self._tray_heard_generation:
            return   # a newer flash superseded this one
        at_ms, eye = HEARD_KEYFRAMES[index]
        self._tray_heard_eye = eye
        self._push_tray_icon()
        if index + 1 < len(HEARD_KEYFRAMES):
            delay_s = (HEARD_KEYFRAMES[index + 1][0] - at_ms) / 1000.0
            thread_registry.timer(
                "dictation.tray_heard", delay_s,
                lambda: _step(index + 1), daemon=True)

    _step(0)

def _request_icon_chase(self, reason):
    """Register a reason for the icon to animate. Starts animation if not running."""
    self._icon_anim_reasons.add(reason)
    if not self._icon_animating:
        self._start_icon_chase()

def _release_icon_chase(self, reason):
    """Remove a reason for animation. Stops only when ALL reasons are gone."""
    self._icon_anim_reasons.discard(reason)
    if not self._icon_anim_reasons and self._icon_animating:
        self._stop_icon_chase()

def _start_icon_chase(self):
    """Start the spinning color-chase animation on the tray icon."""
    self._icon_animating = True
    self._icon_chase_offset = 0
    self._icon_chase_counter = 0
    # _icon_rotation is NOT reset: the head keeps chasing the tail from
    # wherever it stopped, never snapping to an aligned rest pose.
    self._icon_chase_tick()

def _stop_icon_chase(self):
    """Stop the chase animation and show idle icon."""
    self._icon_animating = False
    if self._icon_chase_timer is not None:
        self._icon_chase_timer.cancel()
        self._icon_chase_timer = None
    self._icon_chase_offset = 0
    self._push_tray_icon()   # keeps the current rotation (no aligned rest)

def _icon_chase_tick(self):
    """Advance the tray animation and schedule the next tick.

    Motion means capture (queue 19): the ring TURNS in every active
    capture state -- the ouroboros head chasing its tail -- and speed is
    the state channel (tray_qt.SPIN_SECONDS_PER_TURN):
    - recording:     filled red wheel AND turning, 'recording' pace
                     (fill/colour = state, rotation = liveness)
    - transcribing:  the 'recording' reason outlives capture -> fastest
    - continuous:    'listening' pace (ambient)
    - wake_word:     'armed' pace (ambient)
    No opacity pulse any more. At rest the chase is released and the mark
    stands still (_stop_icon_chase). Thinking (2.4 s/turn) is signalled
    only to the listening indicator (set_thinking from ask_ollama / the Ava
    command session), so the tray has no thinking reason. Tick intervals
    stay the existing ICON_TICK_* constants.
    """
    if not self._icon_animating:
        return

    from samsara.ui.tray_qt import SPIN_SECONDS_PER_TURN

    # Highest-priority active reason picks the tick and the pace.
    if getattr(self, 'recording', False):
        tick_interval, pace = ICON_TICK_FAST, 'recording'
    elif 'recording' in self._icon_anim_reasons:
        tick_interval, pace = ICON_TICK_FAST, 'transcribing'
    elif 'continuous' in self._icon_anim_reasons:
        tick_interval, pace = ICON_TICK_MEDIUM, 'listening'
    else:  # wake_word or anything else
        tick_interval, pace = ICON_TICK_SLOW, 'armed'

    self._icon_rotation += 2 * math.pi * tick_interval / SPIN_SECONDS_PER_TURN[pace]

    if hasattr(self, 'tray_icon'):
        try:
            self.tray_icon.icon = self.create_icon_image(rotation=self._icon_rotation)
        except OSError as e:
            # transient WinError during icon handle swap -- skip this frame
            logger.debug(f"Tray icon animation frame swap failed: {e}")

    self._icon_chase_timer = thread_registry.timer(
        "dictation.icon_chase", tick_interval,
        self._icon_chase_tick, daemon=True)

