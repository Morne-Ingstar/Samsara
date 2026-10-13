"""Short-lived, memory-only take-back for a spoken whole-draft clear."""
from __future__ import annotations

import threading

from samsara.runtime import thread_registry


class ClearDraftUndo:
    """Keep one snapshot; expire it even if no further utterance arrives."""

    def __init__(self, clock, ttl_s: float):
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.RLock()
        self._snapshot = None
        self._timer = None

    def drop(self) -> None:
        with self._lock:
            self._snapshot = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def pending(self) -> bool:
        """A voice-clear snapshot owns shared restore words until it expires."""
        with self._lock:
            return self._snapshot is not None

    def arm(self, manager) -> None:
        with self._lock:
            self.drop()
            document = manager.draft_document
            token = object()
            self._snapshot = {
                "token": token, "expires": self._clock() + self._ttl_s,
                "segments": list(document._segments),
                "transactions": list(document._transactions),
                "manual_commit": document.manual_commit, "edited": document.edited,
                "audio": list(manager._dictate_pending_audio),
                "terminal": manager._last_dictate_ended_terminal,
                "items": [item for item in manager._stack._items
                          if item.kind == "dictation_staged_chunk"],
                "parked": manager._parked_hold,
            }
            self._timer = thread_registry.timer(
                "draft.clear_undo", self._ttl_s, self._expire,
                args=(token,), daemon=True,
            )

    def _expire(self, token) -> None:
        with self._lock:
            if self._snapshot is not None and self._snapshot["token"] is token:
                self.drop()

    def restore(self, manager) -> int:
        with self._lock:
            snapshot = self._snapshot
            if (snapshot is None or self._clock() >= snapshot["expires"]
                    or manager.dictate_pending_buffer):
                self.drop()
                return 0
            self.drop()
        document = manager.draft_document
        document._segments = snapshot["segments"]
        document._transactions = snapshot["transactions"]
        document.manual_commit = snapshot["manual_commit"]
        document.edited = snapshot["edited"]
        document.revision += 1  # Old correction coordinates must remain stale.
        manager._dictate_pending_audio = snapshot["audio"]
        manager._last_dictate_ended_terminal = snapshot["terminal"]
        manager._parked_hold = snapshot["parked"]
        for item in snapshot["items"]:
            manager._stack.push(item)
        return document.chars
