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
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SmartActionsSession:
    """Multi-turn context manager with automatic expiry."""

    def __init__(self, window_minutes: int = 5):
        self.window_minutes: int = window_minutes
        self.session_id: Optional[str] = None
        self.context: List[Dict[str, str]] = []
        self.pending_observations: List[Dict[str, Any]] = []
        self.last_activity: Optional[datetime] = None

    # ---- Session creation / expiry ------------------------------------------

    def get_or_create_session(self) -> str:
        """Return the current session_id, creating a fresh one if expired."""
        if self.session_id is None or self.is_expired():
            if self.session_id is not None:
                logger.info("[SESSION] Inactivity timeout — starting new session")
            self.session_id = uuid.uuid4().hex[:12]
            self.context = []
            self.pending_observations = []
            logger.info("[SESSION] New session: %s", self.session_id)
        self.last_activity = datetime.now()
        return self.session_id

    def is_expired(self) -> bool:
        """True when last_activity is older than window_minutes."""
        if self.last_activity is None:
            return True
        delta = datetime.now() - self.last_activity
        return delta.total_seconds() > self.window_minutes * 60

    # ---- Context accumulation -----------------------------------------------

    def add_user_turn(self, text: str) -> None:
        self.context.append({'role': 'user', 'text': text})
        self.last_activity = datetime.now()

    def add_assistant_turn(self, text: str) -> None:
        self.context.append({'role': 'assistant', 'text': text})
        self.last_activity = datetime.now()

    # ---- Observations (tool call results) -----------------------------------

    def add_observation(self, tool: str, status: str, output: Any = None) -> None:
        """Record a tool call result for inclusion in the next request payload."""
        self.pending_observations.append({
            'tool': tool,
            'status': status,
            'output': output,
        })

    def consume_observations(self) -> List[Dict[str, Any]]:
        """Return and clear pending_observations. Called when building a request."""
        obs = list(self.pending_observations)
        self.pending_observations.clear()
        return obs

    # ---- Explicit reset ------------------------------------------------------

    def reset(self) -> None:
        """End the current session unconditionally ('Jarvis, new conversation')."""
        prev_id = self.session_id
        self.session_id = None
        self.context = []
        self.pending_observations = []
        self.last_activity = None
        if prev_id:
            logger.info("[SESSION] Explicitly reset (was %s)", prev_id)
