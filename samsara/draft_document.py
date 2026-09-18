"""Non-Qt authoritative pending draft model.

Draft text is deliberately kept out of this module's logs. Callers receive
ids, revisions, and lengths so the UI/controller can reason about staleness
without turning a private dictated thought into diagnostic output.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Optional
from uuid import uuid4


RECOVERABLE_DRAFT_TTL_S = 300


@dataclass(frozen=True)
class DraftSegment:
    id: str
    text: str


@dataclass(frozen=True)
class EditResult:
    accepted: bool
    reason: str
    revision: int
    chars: int


@dataclass
class _Transaction:
    kind: str
    segment_id: str
    before: str = ""


class DraftDocument:
    """A revisioned, undoable pending document with a recoverable clear slot.

    Public edit coordinates are always `(document_id, revision, segment_id,
    start, end)`. A stale coordinate is refused; it is never applied to a
    similarly-shaped newer draft.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self.document_id = uuid4().hex
        self.revision = 0
        self._segments: list[DraftSegment] = []
        self._transactions: list[_Transaction] = []
        self.manual_commit = False
        self.edited = False
        self._recoverable: Optional[dict] = None

    @property
    def segments(self) -> tuple[DraftSegment, ...]:
        return tuple(self._segments)

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self._segments)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def recoverable_text(self) -> str:
        slot = self._recoverable
        if slot is None or self._clock() - slot["stashed_at"] > RECOVERABLE_DRAFT_TTL_S:
            return ""
        return "".join(segment.text for segment in slot["segments"])

    def append(self, text: str) -> Optional[str]:
        if not text:
            return None
        segment = DraftSegment(uuid4().hex, str(text))
        self._segments.append(segment)
        self._transactions.append(_Transaction("append", segment.id))
        self.revision += 1
        return segment.id

    def replace_text(self, text: str, *, edited: bool = False) -> None:
        """Compatibility bridge for legacy callers; creates one stable segment."""
        self._segments = [DraftSegment(uuid4().hex, str(text))] if text else []
        self._transactions.clear()
        self.edited = self.edited or edited
        self.revision += 1

    def edit(self, document_id: str, revision: int, segment_id: str,
             start: int, end: int, replacement: str) -> EditResult:
        if document_id != self.document_id:
            return EditResult(False, "wrong_document", self.revision, self.chars)
        if revision != self.revision:
            return EditResult(False, "stale_revision", self.revision, self.chars)
        for index, segment in enumerate(self._segments):
            if segment.id != segment_id:
                continue
            if start < 0 or end < start or end > len(segment.text):
                return EditResult(False, "invalid_span", self.revision, self.chars)
            self._transactions.append(_Transaction("edit", segment.id, segment.text))
            self._segments[index] = DraftSegment(
                segment.id, segment.text[:start] + replacement + segment.text[end:])
            self.edited = True
            self.revision += 1
            return EditResult(True, "edited", self.revision, self.chars)
        return EditResult(False, "unknown_segment", self.revision, self.chars)

    def undo(self) -> bool:
        if not self._transactions:
            return False
        transaction = self._transactions.pop()
        if transaction.kind == "append":
            self._segments = [s for s in self._segments if s.id != transaction.segment_id]
        else:
            for index, segment in enumerate(self._segments):
                if segment.id == transaction.segment_id:
                    self._segments[index] = DraftSegment(segment.id, transaction.before)
                    break
        self.revision += 1
        self.edited = any(t.kind == "edit" for t in self._transactions)
        return True

    def mark_manual_commit(self) -> None:
        if not self.manual_commit:
            self.manual_commit = True
            self.revision += 1

    def clear(self) -> int:
        chars = self.chars
        self._segments.clear()
        self._transactions.clear()
        self.manual_commit = False
        self.edited = False
        self.revision += 1
        return chars

    def stash_recoverable(self, source: str) -> int:
        if not self.text.strip():
            return 0
        self._recoverable = {
            "segments": list(self._segments), "source": str(source),
            "stashed_at": self._clock(),
        }
        return self.chars

    def recover(self, *, prepend: bool = False) -> Optional[dict]:
        slot = self._recoverable
        if slot is None or not self.recoverable_text.strip():
            self._recoverable = None
            return None
        self._recoverable = None
        incoming = list(slot["segments"])
        if prepend and incoming and self._segments:
            tail, head = incoming[-1].text, self._segments[0].text
            if tail and head and not tail[-1].isspace() and not head[0].isspace():
                incoming[-1] = DraftSegment(incoming[-1].id, tail + " ")
        self._segments = incoming + self._segments if prepend else incoming
        self._transactions.clear()
        self.revision += 1
        return {"chars": sum(len(s.text) for s in incoming), "source": slot["source"],
                "prepended": bool(prepend)}
