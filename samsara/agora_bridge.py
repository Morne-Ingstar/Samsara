"""Ava-to-Agora bridge client v1 -- Samsara's intake-direction sender.

Library + dev CLI only. This module deliberately does NOT wire any voice
command, Ava verb, session-mode phrase, or UI, and does NOT implement the
callback receiver (Samsara-side listener for Agora's outbound messages) --
both are later passes. It sends one thing: a signed `POST /v1/intents`
request to a locally-running Agora bridge listener, per
AVA_AGORA_CONTRACT.md's wire-envelope and Stage 3 token/signature sections.

Reference implementation this module must interoperate with (a different
repo, read-only, never imported at runtime): F:\\Projects F\\Agora\\
bridge_signing.py (signing/verification helpers) and bridge_listener.py
(the loopback HTTP listener). This module reimplements the identical
canonical-signing-input construction natively (pure stdlib -- no
dependency on that repo at runtime); only this module's OWN test suite
imports bridge_signing.py, to prove byte-for-byte interop.

Interpretations made where the contract text does not pin an exact
value (see the module's own docstring sections below and the task
report for the full list):
  - The wire envelope sent over HTTP contains EXACTLY the 8 keys
    store.py's `_validate_external_intent_envelope` allow-list accepts
    (contract_version, request_id, utterance, intent_context,
    supersedes_request_id, sent_at, expires_at,
    require_reconfirmation_if_delayed). `utterance_sha256` is NOT part
    of the wire body -- Agora computes it itself from `utterance`
    server-side (contract: "Agora also computes utterance_sha256...").
    Sending it as an extra top-level key would be rejected as an
    unknown field by the reference envelope validator. This client
    still computes utterance_sha256 locally (matching Agora's own
    formula byte-for-byte) for its own bookkeeping -- retry-window
    same-utterance comparisons and the typed result's own field -- it
    is simply never transmitted.
  - The `X-Ava-Agora-Timestamp` header value and the body's `sent_at`
    field are the SAME string for one request -- both represent "when
    this request was signed and sent." The contract does not say this
    in so many words, but "Samsara signs a new transmission timestamp"
    (retry section) only makes sense read this way, and using two
    independently-generated clock reads for one request would invite
    exactly the kind of skew the replay window exists to catch.
  - `require_reconfirmation_if_delayed` defaults to `True` (the
    contract's own wire example uses `true`; this is the conservative
    default absent a real reconfirmation flow, which is out of scope
    for this pass) -- overridable by the caller.
  - Any HTTP error status this module doesn't have a dedicated typed
    exception for (400/410/413/503/404/405) raises the generic
    `AgoraIntentError` carrying the status and the response's `error`
    code, rather than being silently swallowed.
  - A `urllib.error.URLError` (which also covers a request timeout, not
    only ECONNREFUSED) is treated as reachability failure
    (`AgoraUnreachableError`) for retry purposes -- the task named
    "connection refused" specifically, but a timed-out socket is not
    meaningfully different for "should we retry" purposes.

Wire limits (contract, measured after UTF-8 encoding) this client
enforces before sending, so a caller gets a clear local error instead of
a same-shaped-but-late 400 from Agora: body <=64 KiB, utterance non-empty
and <=8 KiB. `intent_context` size/shape validation is left to Agora --
this pass never constructs a real one (no UI/context capture exists
yet); the parameter exists for a future caller to pass a pre-built,
already-contract-shaped context object through unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

CONTRACT_VERSION = 1
INTAKE_PATH = "/v1/intents"
DEFAULT_BASE_URL = "http://127.0.0.1:8788"
SOURCE = "samsara/ava"

TOKEN_FILE_ENV_VAR = "AGORA_BRIDGE_TOKEN_FILE"

# Contract: "no more than 30 seconds later" than sent_at. Reused as both
# this single request's own sent_at->expires_at lifetime AND the retry
# buffer's total wall-clock budget below -- the contract ties both ideas
# to the same 30-second figure (Stage 3's "bounded retry window").
EXPIRES_LIFETIME_SECONDS = 30.0
RETRY_TOTAL_BUDGET_SECONDS = 30.0
_RETRY_BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 4.0, 8.0)  # capped, see _send_with_retry

_EVENT_ID_HEADER = "X-Ava-Agora-Event-ID"
_TIMESTAMP_HEADER = "X-Ava-Agora-Timestamp"
_SIGNATURE_HEADER = "X-Ava-Agora-Signature"
_DIRECTION_LABEL = b"ava-agora:intake:v1"  # intake direction only -- see module docstring

_MAX_BODY_BYTES = 64 * 1024
_MAX_UTTERANCE_BYTES = 8 * 1024


# ---------------------------------------------------------------------------
# Typed results and errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgoraIntentResult:
    """A successful (possibly duplicate) accepted-intent response."""
    task_id: str
    duplicate: bool
    state: str
    request_id: str
    utterance: str
    utterance_sha256: str


class AgoraBridgeError(Exception):
    """Base for every typed error this module raises."""


class AgoraTokenError(AgoraBridgeError):
    """The bridge token file is missing, unreadable, or empty."""


class AgoraUnreachableError(AgoraBridgeError):
    """Could not reach the Agora bridge listener (connection refused,
    timed out, or another transport-level failure)."""


class AgoraRetryExhaustedError(AgoraUnreachableError):
    """The 30-second retry budget elapsed without a successful send.
    Still an AgoraUnreachableError (via inheritance) for callers that
    only distinguish "reachable or not"."""


class AgoraAuthError(AgoraBridgeError):
    """Rejected with 401 even after one token re-read and retry."""


class AgoraCollisionError(AgoraBridgeError):
    """HTTP 409 request_id_collision: the request_id was already used
    for a DIFFERENT utterance. The caller must mint a fresh UUID for
    this (distinct) intent -- see resend_with_fresh_uuid()."""

    def __init__(self, *, request_id: str, utterance: str, utterance_sha256: str):
        super().__init__(
            f"request_id {request_id} already used for a different utterance "
            "(request_id_collision) -- mint a fresh UUID for this intent"
        )
        self.request_id = request_id
        self.utterance = utterance
        self.utterance_sha256 = utterance_sha256


class AgoraIntentError(AgoraBridgeError):
    """Any other non-2xx response (400/410/413/503/404/405/...), carrying
    the HTTP status and Agora's own `error` code string."""

    def __init__(self, *, status: int, error_code: str, request_id: str):
        super().__init__(f"Agora rejected the intent: HTTP {status} {error_code!r}")
        self.status = status
        self.error_code = error_code
        self.request_id = request_id


class AgoraValidationError(AgoraBridgeError):
    """A local, pre-send limit was violated (empty/oversized utterance,
    oversized body) -- never sent to Agora at all."""


# ---------------------------------------------------------------------------
# Token: read at send time, cached, re-read on 401 or file-mtime change.
# Never logged or persisted anywhere by this module.
# ---------------------------------------------------------------------------

def _token_file_path() -> Path:
    override = os.environ.get(TOKEN_FILE_ENV_VAR)
    if override:
        return Path(override)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        raise AgoraTokenError(
            "LOCALAPPDATA is not set and AGORA_BRIDGE_TOKEN_FILE is not overridden -- "
            "cannot locate the bridge token file."
        )
    return Path(local_appdata) / "Agora" / "bridge" / "api_token.txt"


class _TokenCache:
    """In-memory cache of the bridge token string. Re-reads the file
    whenever its path or mtime changes (checked on every get()), or
    unconditionally when force=True (used after a 401)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._mtime: float | None = None
        self._token: str | None = None

    def get(self, *, force: bool = False) -> str:
        path = _token_file_path()
        with self._lock:
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                raise AgoraTokenError(f"Cannot read bridge token file: {path}") from exc
            if not force and self._token is not None and self._path == path and self._mtime == mtime:
                return self._token
            text = path.read_text(encoding="utf-8").lstrip("﻿").strip()
            if not text:
                raise AgoraTokenError(f"Bridge token file is empty: {path}")
            self._path = path
            self._mtime = mtime
            self._token = text
            return text


_token_cache = _TokenCache()


# ---------------------------------------------------------------------------
# Signing -- intake direction only. Byte-for-byte identical canonical
# construction to F:\Projects F\Agora\bridge_signing.py's
# derive_direction_key()/signature_input()/sign_request(), reimplemented
# here so this module has zero runtime dependency on that repo. See
# tests/test_agora_bridge.py's interop test for the cross-check.
# ---------------------------------------------------------------------------

def _canonical_event_id(request_id: str) -> str:
    try:
        return str(uuid.UUID(str(request_id)))
    except (ValueError, AttributeError) as exc:
        raise AgoraValidationError(f"request_id must be a UUID: {request_id!r}") from exc


def _intake_key(token: str) -> bytes:
    return hmac.new(token.encode("ascii"), _DIRECTION_LABEL, hashlib.sha256).digest()


def _signature_input(*, method: str, path: str, event_id: str, timestamp: str, body: bytes) -> bytes:
    body_sha256 = hashlib.sha256(body).hexdigest()
    return "\n".join((
        "AVA-AGORA-HMAC-V1",
        "intake",
        method.upper(),
        path,
        event_id,
        timestamp,
        body_sha256,
    )).encode("utf-8")


def _sign(token: str, *, method: str, path: str, event_id: str, timestamp: str, body: bytes) -> str:
    key = _intake_key(token)
    digest = hmac.new(
        key, _signature_input(method=method, path=path, event_id=event_id, timestamp=timestamp, body=body),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def _rfc3339_now() -> tuple[datetime, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now, now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def _utterance_sha256(utterance: str) -> str:
    return hashlib.sha256(utterance.encode("utf-8")).hexdigest()


def _validate_utterance(utterance: str) -> bytes:
    if not isinstance(utterance, str) or not utterance.strip():
        raise AgoraValidationError("utterance must be non-empty text.")
    encoded = utterance.encode("utf-8")
    if len(encoded) > _MAX_UTTERANCE_BYTES:
        raise AgoraValidationError(
            f"utterance is {len(encoded)} bytes, exceeds the {_MAX_UTTERANCE_BYTES}-byte contract limit."
        )
    return encoded


def _build_envelope(
    *,
    request_id: str,
    utterance: str,
    intent_context: dict[str, Any] | None,
    supersedes_request_id: str | None,
    require_reconfirmation_if_delayed: bool,
    sent_at: str,
    expires_at: str,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "utterance": utterance,
        "intent_context": intent_context,
        "supersedes_request_id": supersedes_request_id,
        "sent_at": sent_at,
        "expires_at": expires_at,
        "require_reconfirmation_if_delayed": require_reconfirmation_if_delayed,
    }


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: dict[str, str], timeout: float):
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _send_once(
    utterance: str,
    *,
    request_id: str,
    base_url: str,
    timeout: float,
    intent_context: dict[str, Any] | None,
    supersedes_request_id: str | None,
    require_reconfirmation_if_delayed: bool,
    _force_token_reread: bool = False,
) -> AgoraIntentResult:
    """One HTTP attempt, including the single 401-triggered token-reread
    retry (which is NOT part of the 30-second unreachable-retry budget --
    a 401 means Agora IS reachable, just with a stale token)."""
    encoded_utterance = _validate_utterance(utterance)
    canonical_event_id = _canonical_event_id(request_id)
    sent_at_dt, sent_at = _rfc3339_now()
    expires_at = _format_rfc3339(sent_at_dt + timedelta(seconds=EXPIRES_LIFETIME_SECONDS))

    envelope = _build_envelope(
        request_id=request_id, utterance=utterance, intent_context=intent_context,
        supersedes_request_id=supersedes_request_id,
        require_reconfirmation_if_delayed=require_reconfirmation_if_delayed,
        sent_at=sent_at, expires_at=expires_at,
    )
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_BODY_BYTES:
        raise AgoraValidationError(f"request body is {len(body)} bytes, exceeds the {_MAX_BODY_BYTES}-byte contract limit.")

    token = _token_cache.get(force=_force_token_reread)
    signature = _sign(token, method="POST", path=INTAKE_PATH, event_id=canonical_event_id, timestamp=sent_at, body=body)
    headers = {
        _EVENT_ID_HEADER: canonical_event_id,
        _TIMESTAMP_HEADER: sent_at,
        _SIGNATURE_HEADER: signature,
    }

    url = base_url.rstrip("/") + INTAKE_PATH
    try:
        with _post(url, body, headers, timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return AgoraIntentResult(
                task_id=payload["task_id"], duplicate=bool(payload["duplicate"]), state=payload["state"],
                request_id=canonical_event_id, utterance=utterance, utterance_sha256=_utterance_sha256(utterance),
            )
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        error_code = payload.get("error", "unknown")
        if exc.code == 401:
            if _force_token_reread:
                raise AgoraAuthError("Agora rejected the bridge token even after a re-read.") from exc
            return _send_once(
                utterance, request_id=request_id, base_url=base_url, timeout=timeout,
                intent_context=intent_context, supersedes_request_id=supersedes_request_id,
                require_reconfirmation_if_delayed=require_reconfirmation_if_delayed,
                _force_token_reread=True,
            )
        if exc.code == 409:
            raise AgoraCollisionError(
                request_id=canonical_event_id, utterance=utterance, utterance_sha256=_utterance_sha256(utterance),
            ) from exc
        raise AgoraIntentError(status=exc.code, error_code=error_code, request_id=canonical_event_id) from exc
    except urllib.error.URLError as exc:
        # Covers connection-refused (OSError inside URLError.reason) and a
        # request timeout alike -- see module docstring's interpretation note.
        raise AgoraUnreachableError(f"Could not reach Agora bridge at {url}: {exc}") from exc


def _send_with_retry(
    utterance: str,
    *,
    request_id: str,
    base_url: str,
    timeout: float,
    intent_context: dict[str, Any] | None,
    supersedes_request_id: str | None,
    require_reconfirmation_if_delayed: bool,
    _now_fn: Callable[[], float] = time.monotonic,
    _sleep_fn: Callable[[float], None] = time.sleep,
) -> AgoraIntentResult:
    """Contract's Samsara-side 30-second intake retry buffer: on
    AgoraUnreachableError, hold the intent (same request_id, same
    utterance) and retry with a fresh transmission timestamp each
    attempt, bounded to RETRY_TOTAL_BUDGET_SECONDS total, then raise
    AgoraRetryExhaustedError. `_now_fn`/`_sleep_fn` are test seams --
    production callers never need them."""
    deadline = _now_fn() + RETRY_TOTAL_BUDGET_SECONDS
    attempt = 0
    last_exc: AgoraUnreachableError | None = None
    while True:
        try:
            return _send_once(
                utterance, request_id=request_id, base_url=base_url, timeout=timeout,
                intent_context=intent_context, supersedes_request_id=supersedes_request_id,
                require_reconfirmation_if_delayed=require_reconfirmation_if_delayed,
            )
        except AgoraUnreachableError as exc:
            last_exc = exc
            remaining = deadline - _now_fn()
            if remaining <= 0:
                raise AgoraRetryExhaustedError(
                    f"Gave up after {RETRY_TOTAL_BUDGET_SECONDS:.0f}s retrying request_id={request_id}: {exc}"
                ) from exc
            delay = min(_RETRY_BACKOFF_SCHEDULE[min(attempt, len(_RETRY_BACKOFF_SCHEDULE) - 1)], remaining)
            attempt += 1
            _sleep_fn(delay)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_intent(
    utterance: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 5.0,
    request_id: str | None = None,
    intent_context: dict[str, Any] | None = None,
    supersedes_request_id: str | None = None,
    require_reconfirmation_if_delayed: bool = True,
) -> AgoraIntentResult:
    """Send one voice-originated intent to Agora's intake endpoint.

    request_id: a fresh UUID4 is minted when omitted. Pass one explicitly
    only to resend/retry the SAME intent (contract retry semantics --
    same request_id + same utterance = idempotent; same request_id + a
    DIFFERENT utterance = HTTP 409 request_id_collision, see
    AgoraCollisionError / resend_with_fresh_uuid()).

    Raises AgoraCollisionError, AgoraAuthError, AgoraIntentError,
    AgoraValidationError, or (after the 30-second retry budget is spent
    on repeated connection failure) AgoraRetryExhaustedError.
    """
    request_id = request_id or str(uuid.uuid4())
    return _send_with_retry(
        utterance, request_id=request_id, base_url=base_url, timeout=timeout,
        intent_context=intent_context, supersedes_request_id=supersedes_request_id,
        require_reconfirmation_if_delayed=require_reconfirmation_if_delayed,
    )


def resend_with_fresh_uuid(
    collision: AgoraCollisionError,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 5.0,
    intent_context: dict[str, Any] | None = None,
    require_reconfirmation_if_delayed: bool = True,
) -> AgoraIntentResult:
    """Convenience for the collision path: re-send `collision`'s own
    utterance under a brand-new request_id (never the colliding one).
    Does NOT set supersedes_request_id -- the contract's collision case is
    an accidental UUID reuse, not a deliberate correction/supersession."""
    return send_intent(
        collision.utterance, base_url=base_url, timeout=timeout, request_id=None,
        intent_context=intent_context, supersedes_request_id=None,
        require_reconfirmation_if_delayed=require_reconfirmation_if_delayed,
    )


# ---------------------------------------------------------------------------
# Dev CLI -- the dogfood entry point until voice wiring exists.
# ---------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m samsara.agora_bridge",
        description="Dev CLI for the Ava-Agora bridge intake client (dogfood only -- no voice wiring).",
    )
    parser.add_argument("utterance", help="The utterance text to send.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--resend-collision", action="store_true",
        help="If the send collides (409 request_id_collision), automatically resend once with a fresh UUID.",
    )
    args = parser.parse_args(argv)

    try:
        try:
            result = send_intent(args.utterance, base_url=args.base_url, timeout=args.timeout)
        except AgoraCollisionError as exc:
            if not args.resend_collision:
                raise
            print(f"Collision on request_id={exc.request_id} -- resending with a fresh UUID", file=sys.stderr)
            result = resend_with_fresh_uuid(exc, base_url=args.base_url, timeout=args.timeout)
        print(
            f"task_id={result.task_id} duplicate={result.duplicate} state={result.state} "
            f"request_id={result.request_id} utterance_sha256={result.utterance_sha256}"
        )
        return 0
    except AgoraBridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
