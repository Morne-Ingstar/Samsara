"""Optional LLM post-processing pass for dictation output ("Smart Corrections").

Off by default. Fixes homophones, misrecognitions, and punctuation the way
Wispr Flow's cleanup pass does -- never paraphrases, never blocks output on
failure, never touches command/Ava utterances (callers are responsible for
only invoking smart_correct() on plain dictation output; see the call sites
in dictation.py and samsara/streaming.py).

Backend is either a local Ollama server (reusing the same "ollama.host"
config Ava's Ollama client reads) or the existing samsara.cloud_llm client.
We deliberately do NOT import plugins.commands.ask_ollama here even though
it already talks to Ollama: importing that module starts a background
health-monitor thread as a side effect (module-level `_start_health_monitor()`
call), which we don't want triggered just to read a host string or do a
health check. Instead this module makes its own minimal `requests` calls,
per the same /api/chat contract Ava uses.
"""

import re
import time

import requests

from samsara import cloud_llm
from samsara.log import get_logger

logger = get_logger(__name__)

# Editable at the top of the module by design -- tune wording here.
SYSTEM_PROMPT = (
    "You are a dictation post-processor. The input is raw speech-to-text "
    "output. Fix obvious misrecognitions, homophones (their/there, to/too), "
    "and punctuation. Preserve the speaker's exact wording, tone, and "
    "meaning. Do not paraphrase, summarize, expand, or add content. Do not "
    "answer questions in the text. Output ONLY the corrected text with no "
    "preamble, no quotes, no markdown."
)

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "qwen2.5:3b"
_DEFAULT_TIMEOUT_S = 4.0
_DEFAULT_MIN_WORDS = 3
_VOCAB_CONTEXT_CAP = 600
_WORD_DEVIATION_LIMIT = 0.4

_PROBE_TTL_S = 60.0
_probe_cache: dict = {}  # host -> (monotonic_timestamp, reachable: bool)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _sc_config(app) -> dict:
    return getattr(app, "config", {}).get("smart_corrections", {}) or {}


def is_enabled(app) -> bool:
    return bool(_sc_config(app).get("enabled", False))


def _ollama_host(app) -> str:
    # Same config key Ava's Ollama client reads -- one Ollama server, not a
    # second host setting to keep in sync.
    return getattr(app, "config", {}).get("ollama", {}).get("host") or _DEFAULT_HOST


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

def _ollama_reachable(app) -> bool:
    host = _ollama_host(app)
    now = time.monotonic()
    cached = _probe_cache.get(host)
    if cached is not None and (now - cached[0]) < _PROBE_TTL_S:
        return cached[1]
    try:
        r = requests.get(f"{host}/api/tags", timeout=2)
        reachable = r.status_code == 200
    except Exception:
        reachable = False
    _probe_cache[host] = (now, reachable)
    return reachable


def resolve_backend(app) -> "str | None":
    """Return 'ollama', 'cloud', or None if nothing usable is configured."""
    try:
        backend = _sc_config(app).get("backend", "auto")
        if backend == "ollama":
            return "ollama" if _ollama_reachable(app) else None
        if backend == "cloud":
            return "cloud" if cloud_llm.is_enabled(app) else None
        if backend == "auto":
            if _ollama_reachable(app):
                return "ollama"
            if cloud_llm.is_enabled(app):
                return "cloud"
            return None
        return None
    except Exception as exc:
        logger.debug(f"[SMART] backend resolution failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _vocab_context(app) -> str:
    vt = getattr(app, "voice_training_window", None)
    if vt is None:
        return ""
    parts = []
    vocab = getattr(vt, "custom_vocab", None) or []
    if vocab:
        parts.append("Known terms: " + ", ".join(vocab))
    corrections = getattr(vt, "corrections_dict", None) or {}
    if corrections:
        fixes = ", ".join(f"{wrong}->{right}" for wrong, right in corrections.items())
        parts.append("Known fixes: " + fixes)
    return " ".join(parts)[:_VOCAB_CONTEXT_CAP]


def _build_system_prompt(app) -> str:
    context = _vocab_context(app)
    return f"{SYSTEM_PROMPT}\n\n{context}" if context else SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Output guardrails
# ---------------------------------------------------------------------------

_PREAMBLE_RE = re.compile(
    r"^\s*(?:here(?:'|’)s the corrected text|corrected text|corrected)\s*:\s*",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’'))


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith('```'):
        first_newline = t.find('\n')
        t = t[first_newline + 1:] if first_newline != -1 else t[3:]
        if t.endswith('```'):
            t = t[:-3]
    return t.strip()


def _strip_quotes(text: str) -> str:
    if len(text) >= 2:
        for open_c, close_c in _QUOTE_PAIRS:
            if text[0] == open_c and text[-1] == close_c:
                return text[1:-1].strip()
    return text


def _sanitize_output(raw: str, original: str) -> str:
    """Guardrails on LLM output. Pure function -- no I/O, no side effects.

    Returns the sanitized correction, or `original` unchanged if the output
    is empty, deviates too much in word count, or otherwise looks unsafe to
    trust.
    """
    if not raw:
        return original

    text = raw.strip()
    text = _strip_fences(text)
    text = _PREAMBLE_RE.sub('', text, count=1).strip()
    text = _strip_quotes(text)

    if not text:
        return original

    if '\n' not in original and '\n' in text:
        text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return original

    orig_words = original.split()
    new_words = text.split()
    if not orig_words:
        return text if not new_words else original

    deviation = abs(len(new_words) - len(orig_words)) / len(orig_words)
    if deviation > _WORD_DEVIATION_LIMIT:
        return original

    return text


def _truncate(s: str, limit: int = 120) -> str:
    return s if len(s) <= limit else s[:limit] + "..."


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------

def _call_ollama(text: str, app, system_prompt: str, timeout_s: float) -> "str | None":
    host = _ollama_host(app)
    model = _sc_config(app).get("ollama_model") or _DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }
    try:
        response = requests.post(f"{host}/api/chat", json=payload, timeout=timeout_s)
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "")
    except Exception as exc:
        logger.debug(f"[SMART] Ollama call failed: {exc}")
        return None


def _call_cloud(text: str, app, system_prompt: str, timeout_s: float) -> "str | None":
    try:
        result = cloud_llm.send(system_prompt, text, app, timeout=timeout_s)
        if not result or result.startswith("Error:"):
            logger.debug(f"[SMART] Cloud LLM call failed: {result}")
            return None
        return result
    except Exception as exc:
        logger.debug(f"[SMART] Cloud LLM call failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def smart_correct(text: str, app) -> str:
    """Run the optional LLM cleanup pass over dictation output.

    Returns the corrected text, or the ORIGINAL text unchanged on any
    failure, timeout, guardrail trip, or when disabled/not applicable.
    Never raises.
    """
    try:
        if not is_enabled(app):
            logger.debug("[SMART] disabled -- skipping")
            return text

        words = (text or "").split()
        min_words = int(_sc_config(app).get("min_words", _DEFAULT_MIN_WORDS))
        if len(words) < min_words:
            logger.debug(f"[SMART] under min_words ({len(words)}<{min_words}) -- skipping")
            return text

        backend = resolve_backend(app)
        if backend is None:
            logger.debug("[SMART] no backend resolved -- skipping")
            return text

        cfg = _sc_config(app)
        timeout_s = float(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
        system_prompt = _build_system_prompt(app)

        if backend == "ollama":
            raw = _call_ollama(text, app, system_prompt, timeout_s)
        else:
            raw = _call_cloud(text, app, system_prompt, timeout_s)

        if raw is None:
            logger.debug("[SMART] backend call failed -- returning original")
            return text

        corrected = _sanitize_output(raw, text)

        if corrected != text:
            logger.info(f'[SMART] "{_truncate(text)}" -> "{_truncate(corrected)}"')
        else:
            logger.debug("[SMART] no change after sanitize")

        return corrected
    except Exception as exc:
        logger.debug(f"[SMART] smart_correct failed: {exc}")
        return text
