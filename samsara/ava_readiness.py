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
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Callable, List, Optional

from samsara.log import get_logger

logger = get_logger(__name__)

READY = "ready"
OFFLINE = "offline"
UNKNOWN = "unknown"
WARMING = "warming"
NEEDS_ATTENTION = "needs_attention"
WAITING = "waiting"
CONFIGURED = "configured"
BLOCKED = "blocked"
MODEL_UNAVAILABLE = "model_unavailable"
PROVIDER_DOWN = "provider_down"

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
    state: str                      # READY | OFFLINE | UNKNOWN | WARMING
    provider: Optional[str] = None  # "deepseek", "ollama", ...
    failure_kind: Optional[str] = None
    source: str = "initial"         # "probe" | "turn" | "initial"
    checked_at: float = 0.0         # time.monotonic() of the evidence
    retry_at: Optional[float] = None
    failure: object = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def offline(self) -> bool:
        return self.state == OFFLINE

    @property
    def warming(self) -> bool:
        return self.state == WARMING

    def badge_label(self) -> str:
        """Short label for the indicator."""
        if self.state == READY:
            return "Ava: ready"
        if self.state == CONFIGURED:
            return "Ava: configured"  # STAND-IN COPY (569)
        if self.state == WARMING:
            return "Ava: warming"
        # STAND-IN COPY (566)
        if self.state == NEEDS_ATTENTION:
            return "Ava: needs attention"
        if self.state == WAITING:
            return "Ava: waiting"
        if self.state == OFFLINE:
            return "Ava: offline"
        return "Ava: checking"

    def sentence(self) -> str:
        """One visible, non-secret sentence for this readiness state."""
        if self.state == READY:
            return "Ava is ready to answer."
        if self.state == CONFIGURED:
            return "The key works. Ava is proven ready only after a real turn."  # STAND-IN COPY (569)
        if self.state == WARMING:
            return "Ava is warming up."
        if self.failure_kind is not None:
            return self.spoken_reason()
        return "Ava has not been checked yet."

    def spoken_reason(self) -> str:
        return failure_sentence(self.failure_kind, self.provider,
                                getattr(self.failure, "retry_after_seconds", None))

    def short_reason(self) -> str:
        return failure_short(self.failure_kind)


def failure_sentence(kind: Optional[str], provider: Optional[str], retry_after=None) -> str:
    """What Ava SAYS when it cannot answer. Never contains key material or
    the provider's raw error text."""
    # STAND-IN COPY (566): grouped under report 562's existing wording ask.
    name = display_name(provider)
    if kind == BAD_KEY:
        return f"Ava is offline. {name} rejected the API key. Check it in Settings."
    if kind == RATE_LIMITED:
        from samsara.cloud_llm import bounded_retry_after
        retry_after = bounded_retry_after(retry_after)
        wait = (" Try again shortly." if retry_after is None else
                " Try again later." if retry_after > 60 else
                f" Try again in {max(1, int(retry_after + 0.999))} seconds.")
        return f"{name} is rate limiting requests." + wait
    if kind == TIMEOUT:
        return f"{name} did not answer in time. Try again."
    if kind == OUT_OF_CREDIT:
        return f"{name} says account credit or the API key spending limit is exhausted."
    if kind == BLOCKED:
        return f"{name} blocked the request because of provider policy or budget."
    if kind == MODEL_UNAVAILABLE:
        return f"The selected model or provider route is unavailable on {name}."
    if kind == PROVIDER_DOWN:
        return f"Ava is offline. {name} is temporarily unavailable."
    if kind == NOT_CONFIGURED:
        return f"{name} has no API key set."
    if kind == UNREACHABLE:
        if provider == "ollama":
            return "Ava is offline. Ollama is not running on this computer."
        return f"Ava is offline. I can't reach {name}."
    return f"{name} could not complete the request."


# STAND-IN COPY (566)
_SHORT = {
    BLOCKED: "policy or budget block",
    MODEL_UNAVAILABLE: "model or route unavailable",
    PROVIDER_DOWN: "provider unavailable",
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
    """Compatibility status-only wrapper; cloud callers pass full responses."""
    from samsara.cloud_llm import failure_from_response
    failure = failure_from_response("cloud", SimpleNamespace(status_code=status))
    return failure.kind if failure else None


def ollama_response_content(response) -> str:
    """Return a usable Ollama chat completion, or an empty string.

    Both the user-facing Ava turn and the boot warm-up need the same proof
    that a successful HTTP response actually contains a reply.
    """
    payload = response.json()
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def configured_provider(app) -> str:
    """The provider explicitly selected by the current Ava policy."""
    config = getattr(app, "config", {}) or {}
    if (config.get("ava", {}) or {}).get("provider_policy", "off") == "cloud":
        cfg = config.get("cloud_llm", {}) or {}
        return str(cfg.get("provider", "deepseek"))
    return "ollama"


def provider_is_configured(app) -> bool:
    """Whether an enabled provider exists to warm, without doing I/O."""
    config = getattr(app, "config", {}) or {}
    cloud = config.get("cloud_llm", {}) or {}
    ollama = config.get("ollama", {}) or {}
    return bool(cloud.get("enabled") and cloud.get("api_key")) or bool(ollama.get("enabled", True))


def warm_on_boot_enabled(app) -> bool:
    """Respect the stored choice; absent keys use the schema default (off).

    Explicit warming remains independent of this optional startup switch.
    """
    from samsara import ai_preferences  # noqa: PLC0415
    config = getattr(app, "config", {}) or {}
    if not ai_preferences.runtime_state(config, "ava").allowed:
        return False
    ava = config.get("ava", {}) or {}
    if "warm_on_boot" in ava:
        value = ava["warm_on_boot"]
    elif "ava.warm_on_boot" in config:
        value = config["ava.warm_on_boot"]
    else:
        value = False
    return provider_is_configured(app) and bool(value)


def probe_configured_provider(app, http_get=None, timeout: float = PROBE_TIMEOUT_S):
    """One blocking health check of the configured provider. Returns
    (provider, failure_kind_or_None). Call from the monitor thread only.

    Cloud: GET <base_url>/models with the key -- a 401/403 is a bad key, a
    429 is rate limiting, which a bare "did anything answer" check (the old
    cloud_llm.check_available) reported as available.
    Ollama: GET <host>/api/tags."""
    from samsara import ai_preferences  # noqa: PLC0415
    # One immutable input: a Settings provider change may replace the live
    # section between validation and header construction.
    configured = SimpleNamespace(config=deepcopy(getattr(app, "config", {}) or {}))
    state = ai_preferences.runtime_state(configured.config, "ava")
    if not state.allowed:
        return "none", NOT_CONFIGURED
    import requests  # noqa: PLC0415
    from samsara import cloud_llm  # noqa: PLC0415

    from samsara import ai_transport
    provider = configured_provider(configured)
    try:
        if provider == "ollama":
            from plugins.commands.ask_ollama import get_host  # noqa: PLC0415
            r = ai_transport.get_local(f"{get_host(configured)}/api/tags", timeout=timeout,
                                       request_fn=http_get)
            return provider, (None if r.status_code == 200 else UNREACHABLE)

        cfg = configured.config.get("cloud_llm", {}) or {}
        api_key = cfg.get("api_key", "")
        if not api_key:
            return provider, NOT_CONFIGURED
        if provider == "openrouter":
            # Stable evidence lasts until recheck; transient failures retry slowly.
            with _openrouter_probe_lock:
                cached = getattr(app, "_openrouter_readiness_check", None)
                now = time.monotonic()
                transient = cached is not None and getattr(cached[1].failure, "kind", None) in (
                    UNREACHABLE, TIMEOUT, PROVIDER_DOWN)
                retry_due = transient and now - cached[2] >= max(OFFLINE_POLL_S,
                    cloud_llm.bounded_retry_after(cached[1].failure.retry_after_seconds) or 0)
                if cached is None or cached[0] != _recheck_generation or retry_due:
                    result = cloud_llm.check_openrouter(configured, http_get=http_get, timeout=timeout)
                    app._openrouter_readiness_check = (_recheck_generation, result, time.monotonic())
                else:
                    result = cached[1]
                return provider, result.failure
        provider, base_url, _model = cloud_llm._get_provider_config(configured)
        if provider == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
        r = ai_transport.get_cloud(f"{base_url}/models", timeout=timeout, headers=headers,
                                   request_fn=http_get)
        return provider, cloud_llm.failure_from_response(provider, r)
    except ai_transport.AITransportRefused as exc:
        ai_transport.notify_refusal(app, exc)
        return provider, UNREACHABLE
    except requests.exceptions.Timeout:
        return provider, TIMEOUT
    except requests.exceptions.ConnectionError:
        return provider, UNREACHABLE
    except Exception as exc:
        logger.debug(f"[AVA-READY] probe failed: {type(exc).__name__}")
        return provider, PROVIDER_ERROR


def warm_configured_provider(app) -> Readiness:
    """Run one harmless completion after startup to make the configured model
    resident. This is called from a background worker, never the boot lane."""
    from samsara import ai_preferences  # noqa: PLC0415
    from samsara import ai_transport
    if not ai_preferences.runtime_state(getattr(app, "config", {}) or {}, "ava").allowed:
        return tracker.snapshot()
    import requests  # noqa: PLC0415

    provider = configured_provider(app)
    if provider == "openrouter":
        # Even opt-in startup warming must not spend credits as a readiness check.
        provider, failure = probe_configured_provider(app)
        return tracker.record_probe(provider, failure)
    if provider == "ollama":
        from samsara.ava_local_warmup import load
        load(app)  # Explicit warming reports its own readiness result.
        return tracker.snapshot()
    generation = tracker.begin_warming(provider)
    started = time.monotonic()
    failure_kind = None
    try:
        from samsara import cloud_llm  # noqa: PLC0415
        response = cloud_llm.send(
            "You are warming up. Reply only: ready.", "ready", app,
            timeout=float((getattr(app, "config", {}) or {}).get("cloud_llm", {}).get("timeout_seconds", 30) or 30),
        )
        failure_kind = getattr(response, "failure", None)
    except ai_transport.AITransportRefused as exc:
        ai_transport.notify_refusal(app, exc)
        failure_kind = UNREACHABLE
    except requests.exceptions.Timeout:
        failure_kind = TIMEOUT
    except requests.exceptions.ConnectionError:
        failure_kind = UNREACHABLE
    except Exception as exc:
        logger.debug("[AVA-WARM] completion failed: %s", type(exc).__name__)
        failure_kind = PROVIDER_ERROR
    result = tracker.record_warm_result(provider, failure_kind, generation)
    logger.info("[AVA-WARM] provider=%s state=%s latency_ms=%d", provider, result.state,
                round((time.monotonic() - started) * 1000))
    return result


class ReadinessTracker:
    """Thread-safe cached readiness with change listeners."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._current = Readiness(UNKNOWN)
        self._warm_generation = 0
        self._listeners: List[Callable[[Readiness, Readiness], None]] = []

    def snapshot(self) -> Readiness:
        with self._lock:
            current, generation = self._current, self._warm_generation
        if current.state == WAITING and current.retry_at is not None and self._clock() >= current.retry_at:
            # An expired delay permits a fresh check; it cannot prove a turn works.
            current = self._set(current.provider, None, "retry", state=UNKNOWN,
                                expected_warm_generation=generation)[0]
            request_recheck()
        return current

    def add_listener(self, fn: Callable[[Readiness, Readiness], None]) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _set(self, provider: Optional[str], failure_kind: Optional[str], source: str,
             state: Optional[str] = None, *, expected_warm_generation: Optional[int] = None,
             invalidate_warm: bool = False, begin_warm: bool = False) -> tuple[Readiness, Optional[int]]:
        failure = failure_kind if hasattr(failure_kind, "kind") else None
        failure_kind = failure.kind if failure is not None else failure_kind
        checked_at = self._clock()
        from samsara.cloud_llm import bounded_retry_after
        delay = bounded_retry_after(getattr(failure, "retry_after_seconds", None))
        if failure_kind == RATE_LIMITED and delay is None:
            delay = OFFLINE_POLL_S
        inferred = (READY if failure_kind is None else
                    OFFLINE if failure_kind in (BAD_KEY, UNREACHABLE, PROVIDER_DOWN) else
                    WAITING if failure_kind == RATE_LIMITED else NEEDS_ATTENTION)
        new = Readiness(
            state=state or inferred,
            provider=provider,
            failure_kind=failure_kind,
            source=source,
            checked_at=checked_at,
            retry_at=checked_at + delay if failure_kind == RATE_LIMITED else None,
            failure=failure,
        )
        with self._lock:
            if expected_warm_generation is not None and \
                    expected_warm_generation != self._warm_generation:
                return self._current, None
            if begin_warm or invalidate_warm:
                self._warm_generation += 1
            generation = self._warm_generation if begin_warm else None
            old = self._current
            if provider == "openrouter" and failure_kind is None and source != "turn" and state is None:
                if old.provider == provider and old.state == READY and old.source == "turn":
                    new = old
                else:
                    new = Readiness(CONFIGURED, provider=provider, source=source, checked_at=checked_at)
            # Public /models success does not prove balance, routing or quota.
            if source == "probe" and failure_kind is None and old.provider == provider and (
                    old.state == NEEDS_ATTENTION or
                    (old.state == WAITING and old.retry_at is not None and checked_at < old.retry_at)):
                return old, generation
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
        return new, generation

    def record_probe(self, provider: Optional[str], failure_kind: Optional[str]) -> Readiness:
        return self._set(provider, failure_kind, "probe")[0]

    def record_turn(self, provider: Optional[str], failure_kind: Optional[str]) -> Readiness:
        return self._set(provider, failure_kind, "turn", invalidate_warm=True)[0]

    def begin_warming(self, provider: Optional[str]) -> int:
        """Publish warming and return the generation allowed to finish it.

        A real turn or a later warm-up advances the generation, so an older
        background completion cannot replace fresher user-facing evidence.
        """
        _state, generation = self._set(provider, None, "warmup", state=WARMING,
                                       begin_warm=True)
        assert generation is not None
        return generation

    def record_warm_result(self, provider: Optional[str], failure_kind: Optional[str],
                           generation: int) -> Readiness:
        """Commit only the still-current warm-up generation's outcome."""
        return self._set(provider, failure_kind, "warmup",
                         expected_warm_generation=generation)[0]

    def reset(self) -> None:
        with self._lock:
            self._warm_generation += 1
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
        current = self._tracker.snapshot()
        if current.state == WAITING and current.retry_at is not None and self._tracker._clock() < current.retry_at:
            return current
        with self._tracker._lock:
            generation = self._tracker._warm_generation
        was_warming = self._tracker.snapshot().warming
        provider, failure_kind = self._probe_fn()
        # A warm-up completion is fresher evidence than this independent
        # health probe. Do not flash an offline badge while it is in flight.
        if was_warming or self._tracker.snapshot().warming:
            return self._tracker.snapshot()
        return self._tracker._set(provider, failure_kind, "probe",
                                  expected_warm_generation=generation)[0]

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                current = self.run_once()
            except Exception as exc:
                logger.debug(f"[AVA-READY] monitor iteration failed: {exc}")
                current = self._tracker.snapshot()
            interval = self._ready_poll_s if current.ready else self._offline_poll_s
            if current.state == WAITING and current.retry_at is not None:
                interval = min(interval, max(0.01, current.retry_at - self._tracker._clock()))
            self._wake.wait(interval)


# Process-wide instance: one provider configuration per app process.
tracker = ReadinessTracker()
_monitor: Optional[ReadinessMonitor] = None
_monitor_lock = threading.Lock()
_openrouter_probe_lock = threading.Lock()
_recheck_generation = 0


def request_recheck() -> None:
    """Ask the running monitor for an early probe (no-op before start)."""
    global _recheck_generation
    _recheck_generation += 1
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


def start_monitor(app, spawn, *, recheck=False) -> ReadinessMonitor:
    """Start the process-wide monitor once. ``spawn(name, fn)`` is the app's
    thread registry (samsara.runtime.thread_registry.spawn)."""
    global _monitor, _recheck_generation
    with _monitor_lock:
        if recheck:
            _recheck_generation += 1
            tracker._set(configured_provider(app), None, "settings", state=UNKNOWN,
                         invalidate_warm=True)
        if _monitor is None:
            _monitor = ReadinessMonitor(tracker, lambda: probe_configured_provider(app))
            spawn("ava-readiness", _monitor.run)
        elif recheck:
            _monitor.wake()
        return _monitor


def settings_signature(app) -> tuple:
    """Compare readiness inputs only; never log or persist this tuple."""
    config = getattr(app, "config", {}) or {}
    return tuple((config.get(section, {}) or {}).get(key) for section, key in (
        ("ava", "provider_policy"), ("ava", "consent"),
        ("ava_command_session", "enabled"), ("ava_command_session", "model"),
        ("cloud_llm", "provider"), ("cloud_llm", "api_key"),
        ("cloud_llm", "model"),
        ("ollama", "host"), ("ollama", "model")))


def recheck_after_settings(app, previous: tuple) -> None:
    """Apply wakes one check, even when the app started with Ava off."""
    from samsara import ai_preferences
    if previous == settings_signature(app):
        return
    from samsara.runtime import thread_registry
    from samsara.ava_local_warmup import schedule
    schedule(app, lambda name, fn: thread_registry.spawn(name, fn, daemon=True))
    if not ai_preferences.runtime_state(app.config, "ava").allowed:
        from samsara import execution_policy
        execution_policy.stop_all(app, "optional AI disabled", chip=False)
        tracker.reset()
        return
    start_monitor(app, lambda name, fn: thread_registry.spawn(name, fn, daemon=True),
                  recheck=True)


def schedule_warm_on_boot(app, spawn) -> bool:
    """Schedule the optional warm-up after the app has announced startup.
    ``spawn`` is the app's thread registry, so the completion never runs on
    the boot thread."""
    if configured_provider(app) == "ollama":
        from samsara.ava_local_warmup import schedule
        return schedule(app, spawn)
    if not warm_on_boot_enabled(app):
        return False
    spawn("ava-warm-on-boot", lambda: warm_configured_provider(app))
    return True
