"""Ava readiness: can the CONFIGURED model provider answer right now?

Queue 57 (2026-09-14). Ava answered from DeepSeek for a whole evening and
none of it was heard, and when a provider did fail the user got a success
chip or a message about Ollama, which was not the provider in use. This
module holds one cached answer per process to "is Ava ready?", for the
provider the user actually configured:

  * cloud_llm enabled with a key  -> that cloud provider (DeepSeek, ...)
  * otherwise                     -> the local Ollama host

Two things feed it, and neither blocks an utterance:

  1. A background probe (``ReadinessMonitor``) on a slow cadence: 30 s while
     ready, 10 s while offline or unknown, so recovery shows up without a
     restart and without anyone speaking.
  2. Every real Ava turn (``record_turn``). A real request is the freshest
     evidence there is: a failed turn flips the state to offline at once,
     a successful one flips it back to ready.

Readers (the AVA entry probe, the indicator badge, the outcome chip) only
ever call ``snapshot()``, which is a lock-guarded read with no I/O.

Qt-free and network-injectable so it is testable without a provider.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from samsara.log import get_logger

logger = get_logger(__name__)

READY = "ready"
OFFLINE = "offline"
UNKNOWN = "unknown"

# Failure kinds, shared by the probe and by real turns.
UNREACHABLE = "unreachable"
BAD_KEY = "bad_key"
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
PROVIDER_ERROR = "error"
NOT_CONFIGURED = "not_configured"
OUT_OF_CREDIT = "out_of_credit"     # HTTP 402 (DeepSeek: "You have run out of balance")

READY_POLL_S = 30.0
OFFLINE_POLL_S = 10.0
PROBE_TIMEOUT_S = 3.0

_DISPLAY_NAMES = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
}


def display_name(provider: Optional[str]) -> str:
    if not provider:
        return "the model"
    return _DISPLAY_NAMES.get(provider, str(provider))


@dataclass(frozen=True)
class Readiness:
    state: str                      # READY | OFFLINE | UNKNOWN
    provider: Optional[str] = None  # "deepseek", "ollama", ...
    failure_kind: Optional[str] = None
    source: str = "initial"         # "probe" | "turn" | "initial"
    checked_at: float = 0.0         # time.monotonic() of the evidence

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def offline(self) -> bool:
        return self.state == OFFLINE

    def badge_label(self) -> str:
        """Short label for the indicator: "Ava: ready" / "Ava: offline" /
        "Ava: checking"."""
        if self.state == READY:
            return "Ava: ready"
        if self.state == OFFLINE:
            return "Ava: offline"
        return "Ava: checking"

    def spoken_reason(self) -> str:
        return failure_sentence(self.failure_kind, self.provider)

    def short_reason(self) -> str:
        return failure_short(self.failure_kind)


def failure_sentence(kind: Optional[str], provider: Optional[str]) -> str:
    """What Ava SAYS when it cannot answer. Never contains key material or
    the provider's raw error text."""
    name = display_name(provider)
    if kind == BAD_KEY:
        return f"Ava is offline. {name} rejected the API key. Check it in Settings."
    if kind == RATE_LIMITED:
        return f"Ava is offline. {name} is rate limiting requests. Try again in a minute."
    if kind == TIMEOUT:
        return f"Ava is offline. {name} did not answer in time."
    if kind == OUT_OF_CREDIT:
        return f"Ava is offline. {name} says the account is out of balance."
    if kind == NOT_CONFIGURED:
        return f"Ava is offline. {name} has no API key set."
    if kind == UNREACHABLE:
        if provider == "ollama":
            return "Ava is offline. Ollama is not running on this computer."
        return f"Ava is offline. I can't reach {name}."
    return f"Ava is offline. {name} returned an error."


_SHORT = {
    BAD_KEY: "bad API key",
    RATE_LIMITED: "rate limited",
    TIMEOUT: "timed out",
    NOT_CONFIGURED: "no API key",
    OUT_OF_CREDIT: "out of balance",
    UNREACHABLE: "unreachable",
}


def failure_short(kind: Optional[str]) -> str:
    return _SHORT.get(kind, "provider error")


def classify_http_status(status: int) -> Optional[str]:
    """None when the status means the provider accepted us."""
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return BAD_KEY
    if status == 402:
        return OUT_OF_CREDIT
    if status == 429:
        return RATE_LIMITED
    return PROVIDER_ERROR


def classify_error_text(message: str) -> str:
    """Classify cloud_llm.send()'s "Error: ..." string. cloud_llm keeps its
    string contract (other callers match on it); the text carries the
    requests exception, which names the HTTP status."""
    text = (message or "").lower()
    if "timed out" in text or "timeout" in text:
        return TIMEOUT
    if "no api key" in text:
        return NOT_CONFIGURED
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return BAD_KEY
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return RATE_LIMITED
    if "could not connect" in text or "connection" in text:
        return UNREACHABLE
    return PROVIDER_ERROR


def configured_provider(app) -> str:
    """The provider Ava will actually use for the next turn -- the same
    rule ask_ollama routes by (cloud only when enabled AND keyed)."""
    from samsara import cloud_llm  # noqa: PLC0415
    if cloud_llm.is_enabled(app):
        cfg = getattr(app, "config", {}).get("cloud_llm", {}) or {}
        return str(cfg.get("provider", "deepseek"))
    return "ollama"


def probe_configured_provider(app, http_get=None, timeout: float = PROBE_TIMEOUT_S):
    """One blocking health check of the configured provider. Returns
    (provider, failure_kind_or_None). Call from the monitor thread only.

    Cloud: GET <base_url>/models with the key -- a 401/403 is a bad key, a
    429 is rate limiting, which a bare "did anything answer" check (the old
    cloud_llm.check_available) reported as available.
    Ollama: GET <host>/api/tags."""
    import requests  # noqa: PLC0415
    from samsara import cloud_llm  # noqa: PLC0415

    get = http_get or requests.get
    provider = configured_provider(app)
    try:
        if provider == "ollama":
            from plugins.commands.ask_ollama import get_host  # noqa: PLC0415
            r = get(f"{get_host(app)}/api/tags", timeout=timeout)
            return provider, (None if r.status_code == 200 else UNREACHABLE)

        cfg = getattr(app, "config", {}).get("cloud_llm", {}) or {}
        api_key = cfg.get("api_key", "")
        if not api_key:
            return provider, NOT_CONFIGURED
        provider, base_url, _model = cloud_llm._get_provider_config(app)
        if provider == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
        r = get(f"{base_url}/models", timeout=timeout, headers=headers)
        return provider, classify_http_status(int(r.status_code))
    except requests.exceptions.Timeout:
        return provider, TIMEOUT
    except requests.exceptions.ConnectionError:
        return provider, UNREACHABLE
    except Exception as exc:
        logger.debug(f"[AVA-READY] probe failed: {type(exc).__name__}")
        return provider, PROVIDER_ERROR


class ReadinessTracker:
    """Thread-safe cached readiness with change listeners."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._current = Readiness(UNKNOWN)
        self._listeners: List[Callable[[Readiness, Readiness], None]] = []

    def snapshot(self) -> Readiness:
        with self._lock:
            return self._current

    def add_listener(self, fn: Callable[[Readiness, Readiness], None]) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _set(self, provider: Optional[str], failure_kind: Optional[str], source: str) -> Readiness:
        new = Readiness(
            state=READY if failure_kind is None else OFFLINE,
            provider=provider,
            failure_kind=failure_kind,
            source=source,
            checked_at=self._clock(),
        )
        with self._lock:
            old = self._current
            self._current = new
            listeners = list(self._listeners)
        changed = (old.state, old.provider, old.failure_kind) != (new.state, new.provider, new.failure_kind)
        if changed:
            logger.info(
                "[AVA-READY] %s -> %s provider=%s reason=%s (from %s)",
                old.state, new.state, new.provider, new.failure_kind, source,
            )
            for fn in listeners:
                try:
                    fn(old, new)
                except Exception as exc:
                    logger.debug(f"[AVA-READY] listener failed: {exc}")
        return new

    def record_probe(self, provider: Optional[str], failure_kind: Optional[str]) -> Readiness:
        return self._set(provider, failure_kind, "probe")

    def record_turn(self, provider: Optional[str], failure_kind: Optional[str]) -> Readiness:
        return self._set(provider, failure_kind, "turn")

    def reset(self) -> None:
        with self._lock:
            self._current = Readiness(UNKNOWN)


class ReadinessMonitor:
    """Background probe loop. ``wake()`` asks for an early re-check (for
    example after an entry was refused, or after the provider settings
    changed) without waiting for the full interval."""

    def __init__(self, tracker: ReadinessTracker, probe_fn: Callable[[], tuple],
                 ready_poll_s: float = READY_POLL_S, offline_poll_s: float = OFFLINE_POLL_S):
        self._tracker = tracker
        self._probe_fn = probe_fn
        self._ready_poll_s = ready_poll_s
        self._offline_poll_s = offline_poll_s
        self._wake = threading.Event()
        self._stop = threading.Event()

    def run_once(self) -> Readiness:
        provider, failure_kind = self._probe_fn()
        return self._tracker.record_probe(provider, failure_kind)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                current = self.run_once()
            except Exception as exc:
                logger.debug(f"[AVA-READY] monitor iteration failed: {exc}")
                current = self._tracker.snapshot()
            interval = self._ready_poll_s if current.ready else self._offline_poll_s
            self._wake.wait(interval)
            self._wake.clear()


# Process-wide instance: one provider configuration per app process.
tracker = ReadinessTracker()
_monitor: Optional[ReadinessMonitor] = None
_monitor_lock = threading.Lock()


def request_recheck() -> None:
    """Ask the running monitor for an early probe (no-op before start)."""
    monitor = _monitor
    if monitor is not None:
        monitor.wake()


def readiness_for(app) -> Readiness:
    """Cached readiness for the provider the app is configured for NOW. A
    snapshot about a different provider (the user said "ava cloud" or
    changed Settings since) is not evidence: it reads as UNKNOWN and an
    early re-check is requested. No I/O."""
    snap = tracker.snapshot()
    try:
        provider = configured_provider(app)
    except Exception:
        return snap
    if snap.provider is not None and snap.provider != provider:
        request_recheck()
        return Readiness(UNKNOWN, provider=provider)
    if snap.provider is None:
        return Readiness(snap.state, provider=provider, failure_kind=snap.failure_kind,
                         source=snap.source, checked_at=snap.checked_at)
    return snap


def start_monitor(app, spawn) -> ReadinessMonitor:
    """Start the process-wide monitor once. ``spawn(name, fn)`` is the app's
    thread registry (samsara.runtime.thread_registry.spawn)."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = ReadinessMonitor(tracker, lambda: probe_configured_provider(app))
            spawn("ava-readiness", _monitor.run)
        return _monitor
