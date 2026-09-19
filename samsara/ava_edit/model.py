"""rewrite(text, instruction, app) -> (raw_text | None, reason | None).

The one place a model is asked, and the ONLY thing this module does. Local
Ollama first, BYOK cloud only if it is configured -- the same order and the
same config keys smart_corrections uses, because there is one Ollama server
and one cloud provider, not a second set of settings to keep in sync.

What the model is asked for, and what it is never asked for: it returns the
FULL rewritten text, as prose, and nothing else. It is never asked for an edit
command, a character offset, a JSON envelope or a diff. Small models are
reliable at rewriting a sentence and unreliable at everything structured, so
the structured half of the job -- working out what actually changed -- is done
locally in proposal.diff_changes().

WHY THIS MODULE DOES NOT BUILD A PROPOSAL (queue 105). It used to. That made
two places that ran the sanitizer and constructed a Proposal -- here and in
session.propose() -- and two Proposal dialects is exactly how this package
stopped importing. There is now ONE Proposal constructor in the package
(session.propose), and this module hands it a string or a reason. The failure
taxonomy is unchanged; it is only split along the seam it was always on:

    "there is no text to edit"          here  (defensive; session guards first)
    "no instruction given"              here  (defensive; session guards first)
    "that text is too long to edit"     here
    "no model available"                here
    "the model took too long"           here
    "the model could not be reached"    here
    "the model returned nothing"        here
    "that would not change anything"    proposal.Proposal.build
    everything the sanitizer rejects    sanitize.check, via session.propose

Every path returns a WHOLE refusal: `raw` is either the model's entire reply
or None. There is no half-reply, so there is nothing a caller could partially
apply even if it tried.
"""
from __future__ import annotations

import time

from samsara.log import get_logger

logger = get_logger(__name__)

#: Seconds before a proposal is abandoned. A staged edit is interactive: the
#: user has spoken and is waiting. Longer than this and the honest answer is
#: "that didn't work", not a longer wait.
DEFAULT_TIMEOUT_S = 12.0

#: Fallback model when nothing is configured. Small and fast: this task is a
#: rewrite of one dictated utterance, not a conversation.
DEFAULT_MODEL = "llama3.2:3b"

#: The longest source this will hand to a model. A dictated buffer longer than
#: this is not a v1 single-verb edit, and a rewrite of it could not be reviewed
#: by ear anyway.
MAX_SOURCE_CHARS = 4000

SYSTEM_PROMPT = (
    "You edit text. The user gives you a passage and an instruction.\n"
    "Return the FULL passage with the instruction applied, and NOTHING else.\n"
    "\n"
    "Rules you must follow:\n"
    "- Output only the edited passage. No preamble, no explanation, no quotes,\n"
    "  no markdown, no code fences.\n"
    "- Never answer the passage. If the passage asks a question, edit the\n"
    "  question; do not reply to it.\n"
    "- Change only what the instruction asks for. Keep everything else\n"
    "  word for word.\n"
    "- Keep the passage's meaning and its facts. Never add information.\n"
    "- If the instruction cannot be applied, return the passage unchanged."
)


def _config(app) -> dict:
    return (getattr(app, "config", {}) or {}).get("ava_edit", {}) or {}


def _model_name(app) -> str:
    cfg = _config(app)
    if cfg.get("model"):
        return str(cfg["model"])
    ollama = (getattr(app, "config", {}) or {}).get("ollama", {}) or {}
    return str(ollama.get("model") or DEFAULT_MODEL)


def _timeout_s(app) -> float:
    try:
        return float(_config(app).get("timeout_seconds", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def build_user_message(text: str, instruction: str) -> str:
    """The user turn. The passage is fenced with plain markers so a model
    cannot mistake the instruction for part of the text it must edit."""
    return (
        f"Instruction: {instruction}\n"
        "\n"
        "Passage:\n"
        "<<<PASSAGE\n"
        f"{text}\n"
        "PASSAGE>>>\n"
        "\n"
        "Return the full edited passage only."
    )


def _call_ollama(app, user_message: str, timeout_s: float):
    """(content_or_None, reason_or_None). Mirrors smart_corrections._call_ollama."""
    from samsara import smart_corrections as sc  # noqa: PLC0415  (shared host/probe cache)
    import requests  # noqa: PLC0415
    host = sc._ollama_host(app)
    payload = {
        "model": _model_name(app),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "think": False,
    }
    try:
        response = sc._session.post(f"{host}/api/chat", json=payload, timeout=timeout_s)
        response.raise_for_status()
        return response.json().get("message", {}).get("content", ""), None
    except requests.exceptions.Timeout:
        sc._probe_cache.pop(host, None)
        return None, "the model took too long"
    except Exception as exc:
        sc._probe_cache.pop(host, None)
        logger.debug(f"[AVA-EDIT] Ollama call failed: {exc}")
        return None, "the model could not be reached"


def _call_cloud(app, user_message: str, timeout_s: float):
    """(content_or_None, reason_or_None)."""
    from samsara import cloud_llm  # noqa: PLC0415
    try:
        result, error_kind = cloud_llm.send_ex(
            SYSTEM_PROMPT, user_message, app, timeout=timeout_s)
        if error_kind == "timeout":
            return None, "the model took too long"
        if error_kind is not None:
            return None, "the model could not be reached"
        return result, None
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] cloud call failed: {exc}")
        return None, "the model could not be reached"


def resolve_backend(app) -> "str | None":
    """Return only the provider the owner authorized for Ava editing.

    This is intentionally not a local-then-cloud fallback: policy selection
    must happen before a reachability probe or model request.
    """
    from samsara import ai_preferences  # noqa: PLC0415

    state = ai_preferences.runtime_state(getattr(app, "config", {}) or {}, "editing")
    if not state.allowed:
        return None
    if state.provider.identity == "ollama":
        from samsara import smart_corrections as sc  # noqa: PLC0415
        try:
            return "ollama" if sc._ollama_reachable(app) else None
        except Exception as exc:
            logger.debug(f"[AVA-EDIT] Ollama probe failed: {exc}")
            return None
    return "cloud"


def rewrite(text: str, instruction: str, app=None, *, backend=None,
            call_fn=None) -> tuple:
    """Ask the configured model to rewrite `text` per `instruction`.

    Returns (raw_reply, None) when a model answered, or (None, reason) for
    every failure -- no model configured, a timeout, an unreachable server, a
    call that raised, an empty reply. NEVER raises.

    `raw_reply` is the model's text EXACTLY as it came back. It has not been
    cleaned and has not been through the sanitizer: gating it is
    session.propose's job, and doing it here too is what produced two
    divergent copies of this path in the first place.

    `call_fn(user_message) -> (content_or_None, reason_or_None)` exists so a
    test can drive every failure path without a server; the app never passes
    it.
    """
    source = text if isinstance(text, str) else ""
    if not source.strip():
        return None, "there is no text to edit"
    if not (instruction or "").strip():
        return None, "no instruction given"
    if len(source) > MAX_SOURCE_CHARS:
        return None, "that text is too long to edit"

    if call_fn is None:
        backend = backend or resolve_backend(app)
        if backend is None:
            return None, "no model available"
        call = _call_ollama if backend == "ollama" else _call_cloud
    else:
        backend = backend or "test"

        def call(_app, user_message, timeout_s):
            return call_fn(user_message)

    user_message = build_user_message(source, instruction)
    started = time.monotonic()
    try:
        raw, reason = call(app, user_message, _timeout_s(app))
    except Exception as exc:                      # a call_fn or a backend that raises
        logger.debug(f"[AVA-EDIT] model call raised: {exc}")
        return None, "the model could not be reached"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if reason is not None:
        logger.info("[AVA-EDIT] rewrite failed: %s (backend=%s, %d ms)",
                    reason, backend, elapsed_ms)
        return None, reason
    if not isinstance(raw, str) or not raw.strip():
        logger.info("[AVA-EDIT] rewrite empty (backend=%s, %d ms)", backend, elapsed_ms)
        return None, "the model returned nothing"

    logger.info("[AVA-EDIT] rewrite returned %d chars (backend=%s, %d ms)",
                len(raw), backend, elapsed_ms)
    return raw, None
