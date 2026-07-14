"""Safe proposal state for rewriting an unpasted HANDS FREE thought.

This module has no UI, model, clipboard, or keyboard dependencies. A model
worker may produce a proposal here, but only SessionModeManager owns delivery.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional


DEFAULT_PROPOSAL_TTL_S = 120.0
MAX_ABSOLUTE_REPLACEMENT_CHARS = 100_000
_CONTROL_TAG_RE = re.compile(r"(?:<\|[^\n]{1,80}\|>|\[/?INST\]|<<\/?SYS>>)", re.IGNORECASE)


class EditProposalError(ValueError):
    """A rewrite response cannot safely become an edit proposal."""


class StaleEditRequest(EditProposalError):
    """A worker attempted to publish a superseded or expired request."""


@dataclass(frozen=True)
class EditRequest:
    source: str
    instruction: str
    request_id: int
    expires_at: float


@dataclass(frozen=True)
class EditProposal:
    source: str
    instruction: str
    replacement: str
    request_id: int
    expires_at: float


def normalize_edit_text(text: str) -> str:
    """Normalize only stable editor-equivalent forms; preserve all spacing."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _find_disallowed_character(text: str) -> Optional[str]:
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            return f"format character U+{ord(char):04X}"
        if category == "Cc" and char not in "\n\t":
            return f"control character U+{ord(char):04X}"
    return None


def parse_rewrite_response(raw_response: str, *, source: str) -> str:
    """Parse a strict ``{"replacement": "..."}`` local-model response."""
    if not isinstance(raw_response, str):
        raise EditProposalError("rewrite response must be text")
    if not isinstance(source, str):
        raise EditProposalError("rewrite source must be text")
    try:
        payload = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EditProposalError("rewrite response must be strict JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"replacement"}:
        raise EditProposalError("rewrite response must contain only 'replacement'")

    replacement = payload["replacement"]
    if not isinstance(replacement, str):
        raise EditProposalError("replacement must be text")
    replacement = normalize_edit_text(replacement)
    source_normalized = normalize_edit_text(source)
    if not replacement.strip():
        raise EditProposalError("replacement is empty")
    if replacement == source_normalized:
        raise EditProposalError("replacement is unchanged")
    if _CONTROL_TAG_RE.search(replacement):
        raise EditProposalError("replacement contains a model control tag")
    disallowed = _find_disallowed_character(replacement)
    if disallowed:
        raise EditProposalError(f"replacement contains {disallowed}")

    source_len = len(source_normalized)
    max_chars = min(
        MAX_ABSOLUTE_REPLACEMENT_CHARS,
        max(source_len * 3, source_len + 1_000),
    )
    if len(replacement) > max_chars:
        raise EditProposalError(
            f"replacement is too long ({len(replacement)} > {max_chars})"
        )
    return replacement


class EditProposalStore:
    """Thread-safe latest-request-wins proposal state."""

    def __init__(
        self, *, ttl_s: float = DEFAULT_PROPOSAL_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._next_request_id = 0
        self._active_request: Optional[EditRequest] = None
        self._proposal: Optional[EditProposal] = None

    def begin(self, *, source: str, instruction: str) -> EditRequest:
        if not isinstance(source, str) or not source:
            raise EditProposalError("there is no pending text to rewrite")
        if not isinstance(instruction, str) or not instruction.strip():
            raise EditProposalError("rewrite instruction is empty")
        with self._lock:
            self._next_request_id += 1
            request = EditRequest(
                source=source,
                instruction=instruction.strip(),
                request_id=self._next_request_id,
                expires_at=self._clock() + self._ttl_s,
            )
            self._active_request = request
            self._proposal = None
            return request

    def complete(self, request: EditRequest, raw_response: str) -> EditProposal:
        with self._lock:
            self._require_current_locked(request)

        try:
            replacement = parse_rewrite_response(raw_response, source=request.source)
        except EditProposalError:
            with self._lock:
                if self._active_request == request:
                    self._active_request = None
            raise

        with self._lock:
            self._require_current_locked(request)
            proposal = EditProposal(
                source=request.source,
                instruction=request.instruction,
                replacement=replacement,
                request_id=request.request_id,
                expires_at=request.expires_at,
            )
            self._active_request = None
            self._proposal = proposal
            return proposal

    def peek(self) -> Optional[EditProposal]:
        with self._lock:
            if self._proposal is not None and self._clock() >= self._proposal.expires_at:
                self._proposal = None
            return self._proposal

    def discard(self) -> None:
        with self._lock:
            self._active_request = None
            self._proposal = None

    def _require_current_locked(self, request: EditRequest) -> None:
        if self._active_request != request:
            raise StaleEditRequest("rewrite request was superseded")
        if self._clock() >= request.expires_at:
            self._active_request = None
            raise StaleEditRequest("rewrite request expired")
