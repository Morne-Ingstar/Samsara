"""
In-memory conversation history for Ava.

History is session-only — clears on app restart. No persistence to disk.
Thread-safe: add_user/add_assistant/clear can be called from any thread.
"""

import threading


class AvaMemory:
    def __init__(self, max_turns=20):
        self._history = []
        self._max_turns = max_turns
        self._lock = threading.Lock()

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
