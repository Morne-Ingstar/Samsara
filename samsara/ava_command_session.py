"""Closed-world Ava resolver for ordinary COMMAND-mode misses.

This module deliberately owns no capture session, latch, queue, or executor.
Normal COMMAND matching runs first. Only an eligible miss may call the
resolver, which returns a proposal; DictationApp sends that proposal through
CommandExecutor.execute_canonical with Route.MODEL exactly once.

Stage (b) remains a deterministic ACTION2 grammar. It extracts a registered
``focus``/``open``/``close`` command and its user-supplied remainder; it is not
normalised matching and it never calls a model. Stage (c) is the bounded model
fallback. Structured model actions are accepted only when their verb maps to a
catalogued command id.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from samsara.log import get_logger

logger = get_logger(__name__)

_DEFAULTS: dict[str, Any] = {
    "backend": "ollama",
    "model": "llama3.2:3b",
    "shortlist_size": 12,
    "keep_warm": False,
    "interpret_misses": True,
}
MAX_REQUEST_WORDS = 16
MAX_PROPOSAL_STEPS = 3
_REQUEST_LOCK = threading.Lock()
_CANCELLED: set[str] = set()


@dataclass(frozen=True)
class Proposal:
    """A closed-world command selected by Ava; this value has no side effects."""
    command_id: str
    args: dict[str, Any] = field(default_factory=dict)
    origin: str = "model"
    provider: str = "unknown"
    steps: tuple[Proposal, ...] = ()


_ACTION2_VERB_SYNONYMS = {
    "focus": "focus", "focus on": "focus", "bring up": "focus", "go to": "focus",
    "open": "open", "launch": "open", "start": "open", "run": "open",
    "close": "close", "quit": "close", "exit": "close",
}
_ACTION2_PATTERN = re.compile(
    rf"^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    rf"({'|'.join(re.escape(v) for v in sorted(_ACTION2_VERB_SYNONYMS, key=len, reverse=True))})\s+(.+)$",
    re.IGNORECASE,
)


def _cfg(app) -> dict:
    return {**_DEFAULTS, **(getattr(app, "config", {}).get("ava_command_session", {}) or {})}


def _match_action2_grammar(utterance: str) -> tuple[str, str] | None:
    """Deterministically extract a catalogued app-verb and user argument.

    This is grammar recognition, not fuzzy or normalised command matching.
    The returned proposal is still model-origin when it comes from optional
    Ava interpretation, so it retains model-route policy requirements.
    """
    match = _ACTION2_PATTERN.match((utterance or "").strip())
    if not match:
        return None
    verb = _ACTION2_VERB_SYNONYMS.get(match.group(1).lower())
    argument = match.group(2).strip().rstrip(".!?")
    return (verb, argument) if verb and argument else None


def _build_shortlist(app, utterance: str, cfg: dict) -> list[str]:
    executor = getattr(app, "command_executor", None)
    if executor is None or not hasattr(executor, "ava_menu"):
        return []
    from samsara.execution_policy import Route
    return executor.ava_menu(utterance, limit=int(cfg["shortlist_size"]), app=app, route=Route.MODEL)


def _normalise_command_id(value: str | None) -> str:
    text = (value or "").strip().strip('"\'').rstrip(".!?,;: ").strip()
    return re.sub(r"\s+", " ", text).lower()


def cancel(request_id: str) -> None:
    """Invalidate one in-flight request. A late response cannot execute."""
    with _REQUEST_LOCK:
        _CANCELLED.add(str(request_id))


def cancel_all() -> None:
    """Invalidate every in-flight resolver call on stop or mode transition."""
    with _REQUEST_LOCK:
        _CANCELLED.add("*")


def reset_cancellations() -> None:
    with _REQUEST_LOCK:
        _CANCELLED.clear()


def is_cancelled(request_id: str) -> bool:
    with _REQUEST_LOCK:
        return "*" in _CANCELLED or str(request_id) in _CANCELLED


class CommandMissResolver:
    """One request resolver. ``resolve`` never performs an action."""

    def __init__(self, app):
        self.app = app

    def _cancelled(self, request_id: str, deadline: float) -> bool:
        if time.monotonic() > deadline:
            return True
        with _REQUEST_LOCK:
            return "*" in _CANCELLED or str(request_id) in _CANCELLED

    def resolve(self, utterance: str, request_id: str, deadline: float) -> Proposal | None:
        """Return a validated model-origin proposal before ``deadline``.

        A provider may not support cancellation. We therefore recheck the
        cancellation token and deadline after its call and discard late work
        before it can reach the sole execution path.
        """
        started = time.monotonic()
        provider = "none"
        result_id = "none"
        try:
            if self._cancelled(request_id, deadline):
                return None
            cfg = _cfg(self.app)
            if len((utterance or "").split()) > MAX_REQUEST_WORDS:
                return None
            from samsara import ai_preferences, ava_readiness
            state = ai_preferences.runtime_state(getattr(self.app, "config", {}) or {}, "ava")
            provider = getattr(getattr(state, "provider", None), "identity", "none")
            if not state.allowed or not ava_readiness.readiness_for(self.app).ready:
                return None
            grammar = _match_action2_grammar(utterance)
            shortlist = _build_shortlist(self.app, utterance, cfg)
            allowed = {_normalise_command_id(item): item for item in shortlist}
            if (grammar is not None and grammar[0] in allowed
                    and not re.search(r"\b(?:and|then)\b", utterance, re.IGNORECASE)):
                result_id = allowed[grammar[0]]
                return Proposal(result_id, {"remainder": grammar[1]}, provider=provider)

            from plugins.commands import ask_ollama
            system = ask_ollama.get_system_prompt(self.app)
            if "{COMMAND_LIST}" in system:
                system = system.replace("{COMMAND_LIST}", ", ".join(shortlist))
            model = cfg["model"] if provider == "ollama" else None
            response = ask_ollama.ask_ollama(utterance, self.app, model=model, system=system)
            if self._cancelled(request_id, deadline) or not isinstance(response, str) or response == "__OLLAMA_DOWN__":
                return None
            # Repeat the existing APP/ACTION wire lines to carry a list. Keep
            # the existing single-step prompt byte-identical until a live
            # format evaluation is available; never truncate an invalid list.
            lines = [line.strip() for line in response.splitlines() if line.strip()]
            action_lines = [line for line in lines if re.match(
                r"^(?:ACTION2?|APP|SCHEDULE|EXECUTE)\b", line, re.IGNORECASE)]
            if len(action_lines) > 1:
                if (len(action_lines) > MAX_PROPOSAL_STEPS or len(lines) != len(action_lines) + 1
                        or not re.match(r"^CONFIRM\s+.+$", lines[0], re.IGNORECASE)):
                    return None
                steps = []
                for line in lines[1:]:
                    parsed_step = ask_ollama._parse_structured_response(lines[0] + "\n" + line)
                    if parsed_step.get("type") == "action":
                        name = _normalise_command_id(parsed_step.get("command"))
                        if name not in allowed:
                            return None
                        steps.append(Proposal(allowed[name], provider=provider))
                    elif parsed_step.get("type") == "action2":
                        name = _normalise_command_id(parsed_step.get("verb"))
                        argument = str(parsed_step.get("argument") or "").strip()
                        if (name not in allowed or not argument
                                or not ask_ollama.grounded_in_utterance(argument, utterance)):
                            return None
                        steps.append(Proposal(allowed[name], {"remainder": argument}, provider=provider))
                    else:
                        return None
                result_id = ", ".join(step.command_id for step in steps)
                return Proposal(steps[0].command_id, steps[0].args, provider=provider, steps=tuple(steps))
            parsed = ask_ollama._parse_structured_response(response)
            if parsed.get("type") == "action":
                candidate = _normalise_command_id(parsed.get("command"))
                if candidate in allowed:
                    result_id = allowed[candidate]
                    return Proposal(result_id, provider=provider)
            if parsed.get("type") == "action2":
                verb = _normalise_command_id(parsed.get("verb"))
                argument = str(parsed.get("argument") or "").strip()
                if verb in allowed and argument and ask_ollama.grounded_in_utterance(argument, utterance):
                    result_id = allowed[verb]
                    return Proposal(result_id, {"remainder": argument}, provider=provider)
            return None
        finally:
            logger.info("[AVA-RESOLVE] request_id=%s result=%s provider=%s latency_ms=%d",
                        request_id, result_id, provider, round((time.monotonic() - started) * 1000))


def resolve(app, utterance: str, request_id: str, deadline: float) -> Proposal | None:
    """Convenience API for the shared command dispatcher."""
    return CommandMissResolver(app).resolve(utterance, request_id, deadline)


def warm_up(app, on_done=None) -> None:
    """Compatibility entry point: warming belongs to Ava readiness, not a session."""
    from samsara import ava_readiness
    from samsara.runtime import thread_registry

    def _warm():
        ava_readiness.warm_configured_provider(app)
        if on_done is not None:
            on_done()
    thread_registry.spawn("ava-warm", _warm, daemon=True)
