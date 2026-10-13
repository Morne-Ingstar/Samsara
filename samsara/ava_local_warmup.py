"""536: load the local model without generating a conversation turn."""
import threading

import requests
from samsara import ai_preferences, ai_transport, ava_readiness
from samsara.log import get_logger

logger = get_logger(__name__)
_lock = threading.Lock()


def _config(app):
    return (getattr(app, "config", {}) or {}).get("ollama", {}) or {}


def load(app):
    """Explicit warming is independent of the optional startup switch."""
    state = ai_preferences.runtime_state(getattr(app, "config", {}) or {}, "ava")
    if not state.allowed or state.provider.identity != "ollama":
        return False
    cfg = _config(app)
    generation = ava_readiness.tracker.begin_warming("ollama")
    failure = None
    try:
        response = ai_transport.post_local(
            str(cfg.get("host") or "http://localhost:11434").rstrip("/") + "/api/generate",
            json={"model": str(cfg.get("model") or "llama3"), "stream": False},
            timeout=float(cfg.get("timeout_seconds") or 30))
        response.raise_for_status()
    except ai_transport.AITransportRefused as exc:
        failure = ava_readiness.UNREACHABLE
        if "tried to redirect" in str(exc):
            ai_transport.notify_refusal(app, exc)
        logger.debug("[AVA-WARM] local load refused: %s", type(exc).__name__)
    except requests.exceptions.Timeout as exc:
        failure = ava_readiness.TIMEOUT
        logger.debug("[AVA-WARM] local load failed: %s", type(exc).__name__)
    except requests.exceptions.ConnectionError as exc:
        failure = ava_readiness.UNREACHABLE
        logger.debug("[AVA-WARM] local load failed: %s", type(exc).__name__)
    except Exception as exc:
        failure = ava_readiness.PROVIDER_ERROR
        logger.debug("[AVA-WARM] local load failed: %s", type(exc).__name__)
    ava_readiness.tracker.record_warm_result("ollama", failure, generation)
    return failure is None


def schedule(app, spawn):
    """One optional load per route/model transition, respecting the stored switch.

    Missing keys use the schema default (off), as accepted configurations
    already do. Explicit stored choices, including flat keys, are honored.
    """
    config = getattr(app, "config", {}) or {}
    cfg = _config(app)
    signature = ((config.get("ava", {}) or {}).get("provider_policy"),
                 cfg.get("host"), cfg.get("model"),
                 ai_preferences.runtime_state(config, "ava").allowed,
                 ava_readiness.warm_on_boot_enabled(app))
    with _lock:
        if getattr(app, "_ava_local_load_signature", None) == signature:
            return False
        app._ava_local_load_signature = signature
    if signature[0] != "local" or not signature[3] or not signature[4]:
        return False
    spawn("ava-local-load", lambda: load(app))
    return True
