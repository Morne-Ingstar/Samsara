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

Every call that actually reaches a backend names that backend (and, for
Ollama, the model) in its log line -- see _backend_tag(). With
backend="auto", falling back to the cloud when the local Ollama server is
down is opt-in (smart_corrections.allow_cloud_fallback, default False): a
privacy-first app must never silently ship dictated text off-device.
"""

import re
import time

import requests

from samsara import cloud_llm
from samsara import languages
from samsara.languages import LANGUAGES, script_class
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

# Editable at the top of the module by design -- tune wording here. Keep the
# preserve-wording/never-paraphrase core. _BASE_INSTRUCTIONS is
# language-agnostic; the English few-shot examples only make sense for
# English-configured dictation (see _build_system_prompt), so they're a
# separate constant appended only in that case. SYSTEM_PROMPT (the combined,
# English-default prompt) is kept as the single top-of-module constant for
# the common case and for anything reading it directly.
_BASE_INSTRUCTIONS = (
    "You are a dictation post-processor. The input is raw speech-to-text "
    "output. Fix obvious misrecognitions, homophones (their/there, to/too), "
    "and punctuation. Preserve the speaker's exact wording, tone, and "
    "meaning. Do not paraphrase, summarize, expand, or add content. Do not "
    "answer questions in the text. You may add quotation marks around a "
    "quoted phrase, title, or saying, and you may fix words that are "
    "clearly misrecognitions of the intended word in context. Output ONLY "
    "the corrected text with no preamble, no quotes, no markdown."
)

_ENGLISH_FEWSHOT_EXAMPLES = (
    "Examples:\n"
    "Input: I didn't know Ativan was an angziolotic.\n"
    "Output: I didn't know Ativan was an anxiolytic.\n\n"
    "Input: Send the draft to Sarah.\n"
    "Output: Send the draft to Sarah."
)

SYSTEM_PROMPT = f"{_BASE_INSTRUCTIONS}\n\n{_ENGLISH_FEWSHOT_EXAMPLES}"

# Opt-in (smart_corrections.repair_disfluencies, default False) -- appended
# to the system prompt by _build_system_prompt() when enabled. Widens
# _sanitize_output's shrink allowance and suspends its punctuation floor to
# match (removing fillers/fragments legitimately shrinks word count and
# punctuation more than the default guardrails expect -- see both gates
# below).
_DISFLUENCY_INSTRUCTIONS = (
    "Remove disfluencies: filler words (um, uh), immediate self-corrections "
    "(keep only the corrected version: 'understand, misunderstood' means the "
    "speaker corrected themselves to 'misunderstood'), and abandoned sentence "
    "fragments. Never remove intentional repetition or content."
)

# code -> plain native display name (LANGUAGES entries are "Name (code)"),
# for embedding in the non-English prompt addendum below.
_LANGUAGE_DISPLAY_NAMES = {code: name.rsplit(" (", 1)[0] for name, code in LANGUAGES}

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "qwen2.5:3b"
_DEFAULT_TIMEOUT_S = 6.0
_DEFAULT_MIN_WORDS = 3
_DEFAULT_KEEP_ALIVE = "30m"
_VOCAB_CONTEXT_CAP = 600
# A corrections pass fixes homophones and punctuation -- it has no business
# changing 40% of the words. Tightened from 0.4 (tribunal Fix 1, CRITICAL).
_WORD_DEVIATION_LIMIT = 0.15
# INVARIANT: keep this equal to (never less than) _WORD_DEVIATION_LIMIT.
# _fails_punctuation_floor REJECTS when word_shrink <= this threshold and
# lets a shrink LARGER than this threshold through unrejected -- but any
# shrink that large is, by the same arithmetic, also > _WORD_DEVIATION_LIMIT,
# so the deviation gate below catches it instead. Equal thresholds mean
# there is no shrink band that slips past both gates.
_PUNCT_FLOOR_SHRINK_THRESHOLD = 0.15

# repair_disfluencies gate state (see _sanitize_output): off keeps the
# symmetric _WORD_DEVIATION_LIMIT unchanged in both directions. On widens
# the SHRINK allowance only -- stripping fillers/self-corrections/fragments
# legitimately removes more words than a homophone-style fix would -- while
# the growth cap stays the same as the default (the model still must not
# be adding content).
_DISFLUENCY_SHRINK_LIMIT = 0.5
_DISFLUENCY_GROWTH_LIMIT = _WORD_DEVIATION_LIMIT

_PROBE_TTL_S = 60.0
_probe_cache: dict = {}  # host -> (monotonic_timestamp, reachable: bool)
# Last observed reachability per host (independent of the TTL window --
# updated only on a live probe, never on a cache hit), used solely to
# detect a down->up recovery transition for the per-outage notice reset
# below (tribunal Fix 7). None = never probed yet.
_last_known_reachable: dict = {}

# Reused across every Ollama call this module makes (reachability probe,
# corrections, warm_up) so repeated local requests don't pay a fresh
# TCP handshake each time. samsara.cloud_llm.send() does NOT manage a
# session of its own -- every cloud call is a bare requests.post/get -- so
# this only benefits the Ollama path; see the verification report.
_session = requests.Session()

# Fires once per OUTAGE, not once per process (tribunal Fix 7): reset to
# False whenever _ollama_reachable() observes a down->up recovery, so a
# second, later outage still gets its own notice. Silence must never be
# mistakable for "still local" once a prior outage has already been
# through a recovery.
_fallback_notice_shown = False


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
        r = _session.get(f"{host}/api/tags", timeout=2)
        reachable = r.status_code == 200
    except Exception:
        reachable = False
    _probe_cache[host] = (now, reachable)

    # Per-outage notice reset (tribunal Fix 7): a fresh probe (not a cache
    # hit -- only live probes update _last_known_reachable) that finds the
    # host back up after it was previously observed down means this outage
    # is over. Reset the notice flag so the NEXT outage -- however soon --
    # gets its own tray notice instead of staying silent because the flag
    # already fired once, potentially outage(s) ago.
    was_reachable = _last_known_reachable.get(host)
    if reachable and was_reachable is False:
        global _fallback_notice_shown
        _fallback_notice_shown = False
    _last_known_reachable[host] = reachable

    return reachable


def _resolve_backend_detailed(app):
    """Return (backend, used_cloud_fallback, skip_reason).

    backend: 'ollama' | 'cloud' | None
    used_cloud_fallback: True only when backend=='cloud' was reached via an
        auto fallback (not an explicit backend='cloud' setting) -- exactly
        the case that needs one-time tray visibility.
    skip_reason: a short machine-readable tag explaining a None result;
        None whenever a backend was resolved.
    """
    try:
        cfg = _sc_config(app)
        backend_setting = cfg.get("backend", "auto")
        allow_fallback = bool(cfg.get("allow_cloud_fallback", False))

        if backend_setting == "ollama":
            if _ollama_reachable(app):
                return "ollama", False, None
            return None, False, "ollama_down_explicit_backend"

        if backend_setting == "cloud":
            if cloud_llm.is_enabled(app):
                return "cloud", False, None
            return None, False, "cloud_not_configured_explicit_backend"

        if backend_setting == "auto":
            if _ollama_reachable(app):
                return "ollama", False, None
            if not allow_fallback:
                logger.info(
                    "[SMART] local backend down, cloud fallback disabled -- skipping"
                )
                return None, False, "ollama_down_fallback_disabled"
            if cloud_llm.is_enabled(app):
                return "cloud", True, None
            logger.info(
                "[SMART] local backend down, cloud fallback enabled but no "
                "cloud provider configured -- skipping"
            )
            return None, False, "ollama_down_no_cloud_configured"

        return None, False, "unknown_backend_setting"
    except Exception as exc:
        logger.debug(f"[SMART] backend resolution failed: {exc}")
        return None, False, "exception"


def resolve_backend(app) -> "str | None":
    """Return 'ollama', 'cloud', or None if nothing usable is configured."""
    return _resolve_backend_detailed(app)[0]


def describe_backend_status(app) -> str:
    """Human-readable summary of the resolved backend, for the Settings UI
    status line (Advanced tab, Smart Corrections section)."""
    backend, _used_fallback, skip_reason = _resolve_backend_detailed(app)
    if backend == "ollama":
        model = _sc_config(app).get("ollama_model") or _DEFAULT_MODEL
        return f"Ollama (local, {model})"
    if backend == "cloud":
        return "Cloud"
    if skip_reason == "ollama_down_fallback_disabled":
        return "None — local AI down, cloud fallback off"
    if skip_reason == "ollama_down_no_cloud_configured":
        return "None — local AI down, cloud fallback enabled but no cloud provider configured"
    if skip_reason == "ollama_down_explicit_backend":
        return "None — local AI down"
    if skip_reason == "cloud_not_configured_explicit_backend":
        return "None — cloud AI not configured"
    return "None — no backend configured"


# ---------------------------------------------------------------------------
# One-time-per-session fallback notice
# ---------------------------------------------------------------------------

def _notify_fallback_issue(app, kind: str) -> None:
    """Tray-notify the first time (per process) a call is skipped, or routed
    to cloud from auto, so an "auto" backend outage never silently ships
    text off-device without the user knowing. `kind` is 'cloud' (routed to
    cloud via fallback) or 'skip' (no backend usable)."""
    global _fallback_notice_shown
    if _fallback_notice_shown:
        return
    _fallback_notice_shown = True

    nm = getattr(app, "notification_manager", None)
    if nm is None:
        return
    suffix = "using Cloud AI" if kind == "cloud" else "skipping"
    try:
        nm.show_notification(
            "Smart Corrections",
            f"Smart Corrections: local AI unavailable — {suffix}",
            duration=8,
        )
    except Exception as exc:
        logger.debug(f"[SMART] fallback notice failed: {exc}")


_OLLAMA_DOWN_SKIP_REASONS = {
    "ollama_down_explicit_backend",
    "ollama_down_fallback_disabled",
    "ollama_down_no_cloud_configured",
}


def _maybe_notify(app, backend, used_fallback, skip_reason) -> None:
    if used_fallback:
        _notify_fallback_issue(app, "cloud")
    elif backend is None and skip_reason in _OLLAMA_DOWN_SKIP_REASONS:
        _notify_fallback_issue(app, "skip")


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


def _language_aware_prompt(app) -> str:
    """SYSTEM_PROMPT for the configured dictation language.

    English (the default) keeps the original prompt verbatim, including its
    two few-shot examples. Any other configured language drops those
    English-specific examples (they'd be misleading/irrelevant) and adds an
    explicit instruction not to translate -- "auto" gets a language-neutral
    "preserve whatever language this is" instruction since we don't know
    the language in advance; a specific code names it so the model corrects
    in that language rather than defaulting to English.
    """
    lang = getattr(app, "config", {}).get("language", "en")
    if lang in (None, "en"):
        return SYSTEM_PROMPT
    if lang == "auto":
        addendum = "Preserve the language of the input exactly; never translate."
    else:
        lang_name = _LANGUAGE_DISPLAY_NAMES.get(lang, lang)
        addendum = (
            f"The text is expected to be in {lang_name}. Correct it in "
            "whatever language it is actually in. Never translate."
        )
    return f"{_BASE_INSTRUCTIONS}\n\n{addendum}"


def _build_system_prompt(app) -> str:
    prompt = _language_aware_prompt(app)
    if bool(_sc_config(app).get("repair_disfluencies", False)):
        prompt = f"{prompt}\n\n{_DISFLUENCY_INSTRUCTIONS}"
    context = _vocab_context(app)
    return f"{prompt}\n\n{context}" if context else prompt


# ---------------------------------------------------------------------------
# Output guardrails
# ---------------------------------------------------------------------------

_PREAMBLE_RE = re.compile(
    r"^\s*(?:here(?:'|’)s the corrected text|corrected text|corrected)\s*:\s*",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’'))
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)

# The llama3.2-class over-edit signature: commas/apostrophes/periods/question
# marks silently stripped while claiming to "preserve" wording. Quotation
# marks are deliberately NOT in this set -- adding them is now allowed (see
# SYSTEM_PROMPT) and must never trip the floor.
_FLOOR_PUNCT_CHARS = ",.'?"


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


def _strip_think_blocks(text: str) -> str:
    """Reasoning models (qwen3-family) may emit <think>...</think> blocks
    ahead of the actual answer. Strip closed blocks; if reasoning got cut
    off mid-stream and an unclosed <think> remains, drop everything from
    that tag onward -- there's nothing usable after it."""
    text = _THINK_BLOCK_RE.sub('', text)
    m = _THINK_OPEN_RE.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def _punct_count(s: str) -> int:
    return sum(s.count(ch) for ch in _FLOOR_PUNCT_CHARS)


def _fails_punctuation_floor(original: str, text: str) -> bool:
    """The llama3.2 failure signature is stripping commas/periods/
    apostrophes/question marks while keeping ~the same words (e.g. "OK,
    ... it's" -> "... its" -- word count barely moves, punctuation just
    vanishes). A legitimate edit that trims punctuation should usually also
    shrink the word count meaningfully; if punctuation dropped but word
    count didn't shrink by more than 10%, treat it as an over-edit."""
    orig_count = _punct_count(original)
    new_count = _punct_count(text)
    if new_count >= orig_count:
        return False
    orig_words = original.split()
    if not orig_words:
        return False
    new_words = text.split()
    word_shrink = (len(orig_words) - len(new_words)) / len(orig_words)
    return word_shrink <= _PUNCT_FLOOR_SHRINK_THRESHOLD


def _looks_translated(original: str, text: str) -> bool:
    """True if the output's script majority (Latin vs non-Latin) flipped
    relative to the input -- the model translated instead of correcting.
    A crude ratio check; skipped entirely when either side has no letters
    to judge (script_class returns None for digit/punctuation-only text)."""
    orig_class = script_class(original)
    new_class = script_class(text)
    if orig_class is None or new_class is None:
        return False
    return orig_class != new_class


def _sanitize_output(
    raw: str, original: str, repair_disfluencies: bool = False, lang: str = "en",
) -> str:
    """Guardrails on LLM output. Pure function -- no I/O, no side effects.

    Returns the sanitized correction, or `original` unchanged if the output
    is empty, contains a structural artifact the original didn't have,
    deviates too much in word count, looks like an over-edit that silently
    stripped punctuation, looks translated (script flip OR, for a specific
    Latin-script configured language, a same-script translation -- see
    languages.looks_translated_to_english), or otherwise looks unsafe to
    trust.

    repair_disfluencies (default False, unchanged behavior): when True, the
    punctuation floor is suspended entirely (removing a filler like "um,"
    legitimately drops punctuation without shrinking word count much) and
    the word-count deviation check switches from a symmetric +/-15% to an
    asymmetric shrink-up-to-50%/grow-up-to-15% check (fillers/self-
    corrections/fragments legitimately shrink more than they should ever
    grow).

    lang (default "en"): the configured dictation language code, used only
    by the same-script translation guard below.
    """
    if not raw:
        return original

    # Structural-artifact reject gate (tribunal Fix 2): a <think> tag or
    # code fence in the RAW output means the model went off-script.
    # Stripping and continuing (the old behavior) risks deleting real user
    # speech mixed in with the artifact -- a user who literally dictated
    # backtick-adjacent content would get their words eaten. Reject
    # outright instead, UNLESS the ORIGINAL already contained that same
    # artifact (then its presence isn't a sign of the model going
    # off-script -- it's just what the user said). _strip_think_blocks/
    # _strip_fences stay defined below for other callers/tests; this path
    # no longer calls them destructively.
    if _THINK_OPEN_RE.search(raw) and not _THINK_OPEN_RE.search(original):
        return original
    if '```' in raw and '```' not in original:
        return original

    text = raw.strip()
    text = _PREAMBLE_RE.sub('', text, count=1).strip()
    text = _strip_quotes(text)

    if not text:
        return original

    if '\n' not in original and '\n' in text:
        text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return original

    if _looks_translated(original, text):
        return original

    if languages.looks_translated_to_english(lang, original, text):
        return original

    if not repair_disfluencies and _fails_punctuation_floor(original, text):
        return original

    orig_words = original.split()
    new_words = text.split()
    if not orig_words:
        return text if not new_words else original

    delta = len(new_words) - len(orig_words)
    if repair_disfluencies:
        if delta < 0 and (-delta) / len(orig_words) > _DISFLUENCY_SHRINK_LIMIT:
            return original
        if delta > 0 and delta / len(orig_words) > _DISFLUENCY_GROWTH_LIMIT:
            return original
    else:
        deviation = abs(delta) / len(orig_words)
        if deviation > _WORD_DEVIATION_LIMIT:
            return original

    return text


def _truncate(s: str, limit: int = 120) -> str:
    return s if len(s) <= limit else s[:limit] + "..."


def _backend_tag(backend: str, model: "str | None" = None, elapsed_ms: "int | None" = None) -> str:
    parts = [backend]
    if model:
        parts.append(model)
    if elapsed_ms is not None:
        parts.append(f"{elapsed_ms}ms")
    return "[" + " ".join(parts) + "]"


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------

def _call_ollama(text: str, app, system_prompt: str, timeout_s: float, model: str):
    """Returns (raw_content_or_None, was_timeout)."""
    host = _ollama_host(app)
    keep_alive = _sc_config(app).get("keep_alive") or _DEFAULT_KEEP_ALIVE
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
    }
    try:
        response = _session.post(f"{host}/api/chat", json=payload, timeout=timeout_s)
        response.raise_for_status()
        return response.json().get("message", {}).get("content", ""), False
    except requests.exceptions.Timeout:
        logger.debug("[SMART] Ollama call timed out")
        # A successful reachability probe minutes ago doesn't mean the
        # server is still healthy NOW -- invalidate so the next resolution
        # re-probes instead of trusting a stale "up" for the rest of the
        # TTL (tribunal Fix 6).
        _probe_cache.pop(host, None)
        return None, True
    except Exception as exc:
        logger.debug(f"[SMART] Ollama call failed: {exc}")
        _probe_cache.pop(host, None)
        return None, False


def _call_cloud(text: str, app, system_prompt: str, timeout_s: float):
    """Returns (raw_content_or_None, was_timeout). Uses cloud_llm.send_ex()
    (tribunal Fix 8) for a structured (text, error_kind) result instead of
    substring-matching "timed out" in an "Error: ..." string -- a real
    dictated sentence that happened to contain that phrase could otherwise
    misclassify a genuine failure as a timeout."""
    try:
        result, error_kind = cloud_llm.send_ex(system_prompt, text, app, timeout=timeout_s)
        if error_kind is not None:
            logger.debug(f"[SMART] Cloud LLM call failed ({error_kind})")
            return None, error_kind == "timeout"
        return result, False
    except Exception as exc:
        logger.debug(f"[SMART] Cloud LLM call failed: {exc}")
        return None, False


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------

def warm_up(app) -> None:
    """Fire-and-forget 1-token Ollama request on a daemon thread, so the
    first REAL correction call doesn't eat a cold-start model-load penalty
    on top of its own timeout budget. Call at startup and whenever Smart
    Corrections is newly enabled. Gated on the resolved backend actually
    being 'ollama' -- no point warming up a model nobody will use. Never
    raises; failures are DEBUG-only, since this is best-effort latency
    hiding, not a user-facing feature."""
    try:
        backend = resolve_backend(app)
    except Exception as exc:
        logger.debug(f"[SMART] warm_up backend check failed: {exc}")
        return
    if backend != "ollama":
        return

    def _do_warm_up():
        try:
            host = _ollama_host(app)
            model = _sc_config(app).get("ollama_model") or _DEFAULT_MODEL
            keep_alive = _sc_config(app).get("keep_alive") or _DEFAULT_KEEP_ALIVE
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": keep_alive,
                "options": {"num_predict": 1},
            }
            _session.post(f"{host}/api/chat", json=payload, timeout=30)
        except Exception as exc:
            logger.debug(f"[SMART] warm_up request failed: {exc}")

    thread_registry.spawn("smart_corrections.warm_up", _do_warm_up, daemon=True)


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

        backend, used_fallback, skip_reason = _resolve_backend_detailed(app)
        _maybe_notify(app, backend, used_fallback, skip_reason)
        if backend is None:
            logger.debug(f"[SMART] no backend resolved ({skip_reason}) -- skipping")
            return text

        cfg = _sc_config(app)
        timeout_s = float(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
        system_prompt = _build_system_prompt(app)

        model = None
        t0 = time.perf_counter()
        if backend == "ollama":
            model = cfg.get("ollama_model") or _DEFAULT_MODEL
            raw, was_timeout = _call_ollama(text, app, system_prompt, timeout_s, model)
        else:
            raw, was_timeout = _call_cloud(text, app, system_prompt, timeout_s)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if raw is None:
            if was_timeout:
                logger.info(f"[SMART]{_backend_tag(backend)} timed out -- returning original")
            else:
                logger.info(
                    f"[SMART]{_backend_tag(backend, model, elapsed_ms)} "
                    "backend call failed -- returning original"
                )
            return text

        repair_disfluencies = bool(cfg.get("repair_disfluencies", False))
        lang = getattr(app, "config", {}).get("language", "en") or "en"
        corrected = _sanitize_output(
            raw, text, repair_disfluencies=repair_disfluencies, lang=lang,
        )
        tag = _backend_tag(backend, model, elapsed_ms)

        if corrected != text:
            logger.info(f'[SMART]{tag} "{_truncate(text)}" -> "{_truncate(corrected)}"')
        else:
            logger.info(f"[SMART]{tag} no change")

        return corrected
    except Exception as exc:
        logger.debug(f"[SMART] smart_correct failed: {exc}")
        return text
