"""
Conversation history for Ava.

By default history is session-only and clears on app restart. If a
persist_path is supplied, history is loaded on construction and can be
written back to disk via save(), giving optional cross-restart continuity.
Thread-safe: add_user/add_assistant/clear can be called from any thread.
"""

import json
import os
import threading

from samsara.log import get_logger

logger = get_logger(__name__)


class AvaMemory:
    def __init__(self, max_turns=20, persist_path=None):
        self._history = []
        self._max_turns = max_turns
        self._lock = threading.Lock()
        self._persist_path = persist_path
        if persist_path:
            self.load()

    def add_user(self, text):
        with self._lock:
            self._history.append({"role": "user", "content": text})
            self._trim()

    def add_assistant(self, text):
        with self._lock:
            self._history.append({"role": "assistant", "content": text})
            self._trim()

    def get_messages(self, system_prompt=None, token_limit=None):
        """Return the full messages array ready for an API call.

        If token_limit is given (approximate tokens, rough: chars/4),
        oldest turns are dropped until the history fits within the budget.
        The system prompt is never trimmed.
        """
        with self._lock:
            history = list(self._history)

        if token_limit is not None:
            history = _trim_to_token_limit(history, system_prompt, token_limit)

        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.extend(history)
        return msgs

    def clear(self):
        with self._lock:
            self._history.clear()
        if self._persist_path:
            try:
                os.remove(self._persist_path)
            except FileNotFoundError as e:
                logger.debug(f"clear: {e}")
            except OSError as e:
                logger.debug(f"clear: {e}")

    def save(self):
        """Persist history to disk atomically. No-op if no persist_path set.

        Never raises — a save failure must not crash a turn or shutdown.
        """
        if not self._persist_path:
            return
        with self._lock:
            data = json.dumps(self._history, ensure_ascii=False)
        tmp = self._persist_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._persist_path)  # atomic on Windows + POSIX
        except Exception:
            try:
                os.remove(tmp)
            except OSError as e:
                logger.debug(f"save: {e}")

    def load(self):
        """Load history from disk if present. Silent on any failure.

        A corrupt or partial file is ignored and we start fresh rather
        than crash. Loaded history is trimmed to the turn cap.
        """
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            return
        if isinstance(loaded, list):
            with self._lock:
                self._history = loaded[-(self._max_turns * 2):]

    def pop_last_if_user(self) -> bool:
        """Remove the last message if it is an unpaired user turn.

        Called when all LLM backends fail so the orphaned user message
        does not appear as [user, user] at the start of the next turn.
        Returns True if a message was removed.
        """
        with self._lock:
            if self._history and self._history[-1]['role'] == 'user':
                self._history.pop()
                return True
        return False

    def turn_count(self):
        with self._lock:
            return sum(1 for m in self._history if m["role"] == "user")

    def _trim(self):
        max_msgs = self._max_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]


def _trim_to_token_limit(history, system_prompt, token_limit):
    """Return a suffix of history that fits within token_limit."""
    system_tokens = len(system_prompt or "") // 4
    budget = token_limit - system_tokens
    if budget <= 0:
        return []

    kept = []
    tokens_used = 0
    for msg in reversed(history):
        msg_tokens = len(msg["content"]) // 4
        if tokens_used + msg_tokens > budget:
            break
        kept.append(msg)
        tokens_used += msg_tokens

    return list(reversed(kept))
