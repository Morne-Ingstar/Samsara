"""Pure-geometry gesture recognition from MediaPipe 21 hand landmarks.

No trained classifier -- extension/curl are derived from joint angles/positions;
pinch from thumb-tip to index-tip distance normalized by palm size.

State machine (load-bearing):

    IDLE -> CANDIDATE (pose held stable >= hold_ms)
         -> FIRED     (dispatched exactly once per deliberate gesture)
         -> REFRACTORY (hand must return to OTHER/neutral before next fire)
         -> IDLE

This prevents a held pose from spamming 30 events/sec and is critical for the
dictation toggle (same pose = start OR stop depending on app state).
"""

import logging
import time

logger = logging.getLogger(__name__)

# MediaPipe hand landmark indices used for classification
_WRIST = 0
_THUMB_TIP = 4
_THUMB_IP  = 3
_THUMB_MCP = 2
_INDEX_MCP = 5
_INDEX_PIP = 6
_INDEX_TIP = 8
_MIDDLE_MCP = 9
_MIDDLE_TIP = 12
_RING_PIP  = 14
_RING_TIP  = 16
_PINKY_MCP = 17
_PINKY_PIP = 18
_PINKY_TIP = 20

# (tip_idx, pip_idx) pairs for the four fingers
_FINGER_PAIRS = [
    (_INDEX_TIP,  6),   # index  tip / pip
    (_MIDDLE_TIP, 10),  # middle tip / pip
    (_RING_TIP,   14),  # ring   tip / pip
    (_PINKY_TIP,  _PINKY_PIP),
]

_STATE_IDLE       = "idle"
_STATE_CANDIDATE  = "candidate"
_STATE_FIRED      = "fired"
_STATE_REFRACTORY = "refractory"


def _finger_extended(lm, tip_idx: int, pip_idx: int) -> bool:
    return lm[tip_idx].y < lm[pip_idx].y


def _thumb_extended(lm) -> bool:
    return lm[_THUMB_TIP].y < lm[_THUMB_IP].y


def _palm_size(lm) -> float:
    dx = lm[_MIDDLE_MCP].x - lm[_WRIST].x
    dy = lm[_MIDDLE_MCP].y - lm[_WRIST].y
    return (dx * dx + dy * dy) ** 0.5 + 1e-6


def classify_pose(lm) -> str:
    """Classify a set of 21 MediaPipe landmarks into one of the 4 V1 pose names
    or ``"other"`` when no pose matches.

    Poses::

        open_palm  -- all 5 fingers extended (dictation toggle)
        peace      -- index + middle extended, ring + pinky + thumb curled (Ava)
        fist       -- all 5 fingers curled (stop/cancel)
        shaka      -- thumb + pinky extended, index + middle + ring curled
                      (window chooser)
    """
    f = [_finger_extended(lm, t, p) for t, p in _FINGER_PAIRS]
    thumb = _thumb_extended(lm)

    # open_palm: all extended
    if thumb and f[0] and f[1] and f[2] and f[3]:
        return "open_palm"

    # fist: all curled
    if not thumb and not f[0] and not f[1] and not f[2] and not f[3]:
        return "fist"

    # peace: index + middle only
    if f[0] and f[1] and not f[2] and not f[3] and not thumb:
        return "peace"

    # shaka: thumb + pinky only
    if thumb and not f[0] and not f[1] and not f[2] and f[3]:
        return "shaka"

    return "other"


class GestureRecognizer:
    """Stateful recognizer with IDLE/CANDIDATE/FIRED/REFRACTORY state machine.

    Call :meth:`update` once per frame.  Returns ``(pose_name, True)`` exactly
    once when a deliberate gesture fires.  All other returns are ``(pose, False)``.

    Args:
        hold_ms:             Milliseconds a pose must be held to fire (default 350).
        refractory_frames:   Consecutive OTHER-pose frames required before the next
                             fire is accepted (default 8, ~0.25s at 30fps).
    """

    def __init__(self, hold_ms: int = 350, refractory_frames: int = 8) -> None:
        self._hold_ms = hold_ms
        self._refractory_frames = refractory_frames

        self._state = _STATE_IDLE
        self._candidate_pose: str | None = None
        self._candidate_start: float = 0.0
        self._neutral_count: int = 0

    def update(self, pose: str) -> tuple[str, bool]:
        """Feed the current-frame pose name and return ``(pose, fired)``."""
        now = time.monotonic()

        if self._state == _STATE_IDLE:
            if pose != "other":
                self._state = _STATE_CANDIDATE
                self._candidate_pose = pose
                self._candidate_start = now
            return pose, False

        if self._state == _STATE_CANDIDATE:
            if pose != self._candidate_pose:
                # Pose changed before hold threshold -- restart
                self._state = _STATE_IDLE
                if pose != "other":
                    self._state = _STATE_CANDIDATE
                    self._candidate_pose = pose
                    self._candidate_start = now
                return pose, False

            held_ms = (now - self._candidate_start) * 1000
            if held_ms >= self._hold_ms:
                self._state = _STATE_FIRED
                logger.debug("[GESTURE] FIRED %s (held %.0fms)", self._candidate_pose, held_ms)
                return self._candidate_pose, True

            return pose, False

        if self._state == _STATE_FIRED:
            self._state = _STATE_REFRACTORY
            self._neutral_count = 0
            return pose, False

        if self._state == _STATE_REFRACTORY:
            if pose == "other":
                self._neutral_count += 1
                if self._neutral_count >= self._refractory_frames:
                    self._state = _STATE_IDLE
                    logger.debug("[GESTURE] Refractory cleared -> IDLE")
            else:
                # Hand still showing a gesture -- reset neutral count
                self._neutral_count = 0
            return pose, False

        return pose, False

    def reset(self) -> None:
        """Force state machine back to IDLE (e.g. on camera loss)."""
        self._state = _STATE_IDLE
        self._candidate_pose = None
        self._neutral_count = 0
