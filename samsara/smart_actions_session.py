"""Session manager for Smart Actions Phase 2.

Tracks multi-turn conversation context and tool-call observations within
a rolling inactivity window. In-memory only -- never persisted to disk.

Session lifecycle:
  - Created (or re-created after expiry) by get_or_create_session()
  - Turns accumulated via add_user_turn() / add_assistant_turn()
  - Tool results recorded via add_observation()
  - Observations consumed (and cleared) by consume_observations() when the
    next request is built
  - Explicitly ended by reset() ("Jarvis, new conversation")

Thread safety:
  self._lock (threading.Lock) protects context, pending_observations,
  session_id, and last_activity.  The lock is held only around list/attr
  ops — never around LLM calls, file I/O, logging, or callbacks.
  is_expired() is intentionally lock-free: it is called from within
  get_or_create_session() which already holds _lock, and the single
  datetime attribute read is GIL-atomic for external callers.
"""

import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SmartActionsSession:
    """Multi-turn context manager with automatic expiry."""

    def __init__(self, window_minutes: int = 5):
        self.window_minutes: int = window_minutes
        self._lock = threading.Lock()
        # Protected by self._lock
        self.session_id: Optional[str] = None
        # Protected by self._lock
        self.context: List[Dict[str, str]] = []
        # Protected by self._lock
        self.pending_observations: List[Dict[str, Any]] = []
        # Protected by self._lock
        self.last_activity: Optional[datetime] = None

    # ---- Session creation / expiry ------------------------------------------

    def get_or_create_session(self) -> str:
        """Return the current session_id, creating a fresh one if expired.

        Logger calls are made outside the lock to avoid holding it around
        potentially-lazy string formatting.
        """
        log_timeout = False
        new_sid = None
        with self._lock:
            if self.session_id is None or self.is_expired():
                log_timeout = self.session_id is not None
                self.session_id = uuid.uuid4().hex[:12]
                self.context.clear()
                self.pending_observations.clear()
                new_sid = self.session_id
            self.last_activity = datetime.now()
            sid = self.session_id
        if log_timeout:
            logger.info("[SESSION] Inactivity timeout — starting new session")
        if new_sid is not None:
            logger.info("[SESSION] New session: %s", new_sid)
        return sid

    def is_expired(self) -> bool:
        """True when last_activity is older than window_minutes.

        Lock-free by design: called from within get_or_create_session() which
        already holds self._lock.  External callers get a GIL-atomic read of
        last_activity; the result may be immediately stale, which is acceptable
        for a staleness-tolerant expiry check.
        """
        if self.last_activity is None:
            return True
        delta = datetime.now() - self.last_activity
        return delta.total_seconds() > self.window_minutes * 60

    # ---- Context accumulation -----------------------------------------------

    def add_user_turn(self, text: str) -> None:
        with self._lock:
            self.context.append({'role': 'user', 'text': text})
            self.last_activity = datetime.now()

    def add_assistant_turn(self, text: str) -> None:
        with self._lock:
            self.context.append({'role': 'assistant', 'text': text})
            self.last_activity = datetime.now()

    def snapshot_context(self) -> List[Dict[str, str]]:
        """Return a point-in-time shallow copy of the context list.

        Shallow copy is safe: context dicts contain only string values and
        are never mutated after creation.  The snapshot may become stale
        immediately after return — callers must not assume it stays current.
        """
        with self._lock:
            return list(self.context)

    # ---- Observations (tool call results) -----------------------------------

    def add_observation(self, tool: str, status: str, output: Any = None) -> None:
        """Record a tool call result for inclusion in the next request payload."""
        with self._lock:
            self.pending_observations.append({
                'tool': tool,
                'status': status,
                'output': output,
            })

    def consume_observations(self) -> List[Dict[str, Any]]:
        """Return and clear pending_observations. Called when building a request.

        Snapshot is taken and list cleared atomically under the lock.
        Shallow copy is safe: observation dicts are never mutated after
        add_observation() creates them.
        """
        with self._lock:
            obs = list(self.pending_observations)
            self.pending_observations.clear()
        return obs

    def snapshot_observations(self) -> List[Dict[str, Any]]:
        """Return a point-in-time copy of pending_observations without clearing.

        Intended for test introspection and diagnostics.  The snapshot may
        be stale immediately after return.
        """
        with self._lock:
            return list(self.pending_observations)

    # ---- Session state queries ----------------------------------------------

    def has_active_session(self) -> bool:
        """True if a session_id has been assigned (not None)."""
        with self._lock:
            return self.session_id is not None

    # ---- Explicit reset ------------------------------------------------------

    def reset(self) -> None:
        """End the current session unconditionally ('Jarvis, new conversation')."""
        with self._lock:
            prev_id = self.session_id
            self.session_id = None
            self.context.clear()
            self.pending_observations.clear()
            self.last_activity = None
        if prev_id:
            logger.info("[SESSION] Explicitly reset (was %s)", prev_id)

    # ---- Test helpers -------------------------------------------------------

    def _backdate_last_activity(self, dt: datetime) -> None:
        """Directly set last_activity to dt. For test use only."""
        with self._lock:
            self.last_activity = dt
