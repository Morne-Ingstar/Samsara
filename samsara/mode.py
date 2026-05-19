"""
Mode state machine for Samsara.

Defines the set of operating modes (IDLE, HOLD, TOGGLE, WAKE, COMMAND,
AVA, STREAMING, CONTINUOUS) and the legal transition table between them.

Used by DictationApp as the single source of truth for current mode state.
Thread-safe.  All reads and transitions are lock-protected internally.

Note: CONTINUOUS mode is included in the enum for completeness but is
tracked separately by DictationApp (it can coexist with WAKE mode).
The state machine currently transitions between the other seven modes.
"""

import threading
from enum import Enum, auto
from typing import Callable, FrozenSet, Optional, Set


class Mode(Enum):
    IDLE       = auto()  # not recording, not listening
    HOLD       = auto()  # hold-to-record active (Ctrl+Shift held)
    TOGGLE     = auto()  # toggle recording active
    CONTINUOUS = auto()  # continuous listening (tracked separately; see module note)
    WAKE       = auto()  # wake word listening active (background)
    COMMAND    = auto()  # Mouse 4 / keyboard command-mode recording
    AVA        = auto()  # Right-Alt Ava conversation recording
    STREAMING  = auto()  # CapsLock streaming-dictation active


# Legal transitions: from → {allowed targets}.
# Illegal transitions log a warning; the machine tries a defensive IDLE
# detour when the target is reachable from IDLE, otherwise stays put.
_TRANSITIONS: dict[Mode, Set[Mode]] = {
    Mode.IDLE:       {Mode.HOLD, Mode.TOGGLE, Mode.CONTINUOUS,
                      Mode.WAKE, Mode.COMMAND, Mode.AVA, Mode.STREAMING},
    Mode.HOLD:       {Mode.IDLE, Mode.WAKE},
    Mode.TOGGLE:     {Mode.IDLE, Mode.WAKE},
    Mode.CONTINUOUS: {Mode.IDLE},
    Mode.WAKE:       {Mode.IDLE, Mode.HOLD, Mode.COMMAND, Mode.AVA, Mode.STREAMING},
    Mode.COMMAND:    {Mode.IDLE, Mode.WAKE},
    Mode.AVA:        {Mode.IDLE, Mode.WAKE},
    Mode.STREAMING:  {Mode.IDLE, Mode.WAKE},
}

# Modes that involve active audio capture (DictationConsumer running)
_RECORDING_MODES: FrozenSet[Mode] = frozenset({
    Mode.HOLD, Mode.TOGGLE, Mode.COMMAND, Mode.AVA, Mode.STREAMING,
})

# Modes that are passive listeners (wake consumer or continuous VAD)
_LISTENING_MODES: FrozenSet[Mode] = frozenset({
    Mode.WAKE, Mode.CONTINUOUS,
})


class ModeStateMachine:
    """Single source of truth for DictationApp's current operating mode.

    Thread-safe.  Listeners are called outside the internal lock so they
    can safely call back into the app without deadlocking.
    """

    def __init__(self):
        self._mode = Mode.IDLE
        self._listeners: list[Callable] = []
        self._lock = threading.Lock()

    # ── public API ─────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._mode

    def transition(self, target: Mode) -> bool:
        """Attempt to transition to *target*.

        Returns True on success, False if the transition cannot be made.

        Defensive behaviour for illegal transitions:
          - If the target is reachable from IDLE, the machine logs a warning
            and forces the transition (equivalent to the old flag-set code
            that would flip the new flag without clearing the old one).
          - If the target is not reachable from IDLE either, the transition
            is rejected and the current mode is kept.

        Safe to call from any thread.
        """
        with self._lock:
            old = self._mode
            if old == target:
                return True  # already there; no-op, not an error

            allowed = _TRANSITIONS.get(old, set())
            if target in allowed:
                self._mode = target
                listeners = list(self._listeners)
            else:
                idle_can_reach = target in _TRANSITIONS.get(Mode.IDLE, set())
                if idle_can_reach:
                    print(f"[MODE] Illegal {old.name} -> {target.name}; "
                          f"forcing via IDLE")
                    self._mode = target
                    listeners = list(self._listeners)
                else:
                    print(f"[MODE] Illegal {old.name} -> {target.name}; ignored")
                    return False

            new = self._mode

        print(f"[MODE] {old.name} -> {new.name}")
        for cb in listeners:
            try:
                cb(old, new)
            except Exception as exc:
                print(f"[MODE] Listener error: {exc}")
        return True

    def register_listener(self, callback: Callable[[Mode, Mode], None]) -> None:
        """Register *callback(old, new)* to be called on every successful transition."""
        with self._lock:
            self._listeners.append(callback)

    def is_recording(self) -> bool:
        """True when in any active-capture mode (mic open, consumer active)."""
        return self._mode in _RECORDING_MODES

    def is_listening(self) -> bool:
        """True when passively listening (wake word detector or continuous VAD)."""
        return self._mode in _LISTENING_MODES
