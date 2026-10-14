"""
Cloud LLM provider for Ava. Sends requests to external API endpoints
(DeepSeek, OpenAI, Anthropic, OpenRouter) as an alternative to local Ollama.

User provides their own API key. Data leaves the machine when enabled.
Samsara does not store conversation content beyond the local session.
"""

import logging
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import requests
from samsara import ai_transport
from urllib.parse import urlparse


BUILTIN_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "host": "api.deepseek.com",
        "model": "deepseek-flash",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "host": "api.openai.com",
        "model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "host": "api.anthropic.com",
        "model": "claude-sonnet-5-5",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "host": "openrouter.ai",
        "model": "openrouter/auto",
    },
}
SUPPORTED_PROVIDERS = frozenset(BUILTIN_PROVIDERS)
RETIRED_DEFAULTS = {
    "deepseek": {"deepseek-chat": BUILTIN_PROVIDERS["deepseek"]["model"]},
    "anthropic": {"claude-sonnet-4-20250514": BUILTIN_PROVIDERS["anthropic"]["model"]},
}
_reported_retired_defaults = set()



@dataclass(frozen=True)
class CloudFailure:
    """Private provider failures retain only bounded, allowlisted metadata.

    Raw messages/metadata may echo keys or prompts. Never retain them, even
    after regex redaction: project known meanings onto fixed summaries.
    """
    kind: str
    provider: str
    http_status: int | None = None
    retry_after_seconds: float | None = None
    provider_error_code: str | int | None = None
    provider_error_message: str | None = None


class CloudError(str):
    """Keep send()/send_json()'s string API; attach per-request failure data."""
    def __new__(cls, failure, message=None):
        obj = super().__new__(cls, "Error: " + (message or failure.provider_error_message or "Cloud request failed."))
        obj.failure = failure
        return obj


class _CloudRequestFailed(Exception):
    def __init__(self, failure):
        self.failure = failure
        super().__init__(failure.kind)


# STAND-IN COPY (566): all failure copy is grouped in report 566's COPY_DRAFT,
# referencing the existing write.openrouter.failure_copy ask in report 562.
_FAILURE_SUMMARIES = {
    "bad_key": "The API key was rejected.",
    "out_of_credit": "Account credit or the API key spending limit was exhausted.",
    "blocked": "The request was blocked by provider policy or budget.",
    "model_unavailable": "The model or an eligible provider route is unavailable.",
    "rate_limited": "Requests are rate limited.",
    "provider_down": "The provider is unavailable.",
    "timeout": "The request timed out.",
    "unreachable": "The provider could not be reached.",
    "not_configured": "No API key configured for cloud LLM.",
    "error": "The provider returned an error.",
}
_SAFE_ERROR_CODES = frozenset({
    "invalid_api_key", "authentication_error", "permission_error",
    "insufficient_quota", "insufficient_balance", "rate_limit_exceeded",
    "rate_limit_error", "model_not_found", "overloaded_error",
    "content_filter", "moderation_error", "budget_exceeded",
})


def make_failure(kind, provider, status=None, retry_after=None, code=None):
    # Provider identifiers originate in a closed table, never in response data.
    provider = provider if provider in SUPPORTED_PROVIDERS else "cloud"
    return CloudFailure(kind, provider, status, retry_after, code, _FAILURE_SUMMARIES[kind])


MAX_RETRY_AFTER_SECONDS = 3600.0


def bounded_retry_after(value):
    """Bound provider delays, including metadata supplied outside HTTP parsing."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds)) if math.isfinite(seconds) else None


def _retry_after(response):
    headers = getattr(response, "headers", None) or {}
    value = next((value for key, value in headers.items()
                  if str(key).lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return bounded_retry_after(seconds)


def failure_from_response(provider, response):
    """One HTTP classifier shared by ordinary/JSON/search turns and probes.

    OpenRouter's documented error.code/message envelope is inspected only
    here. Unknown provider codes and all raw messages/metadata are dropped.
    """
    http_status = status = int(getattr(response, "status_code", 200) or 0)
    error = {}
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
    except Exception:
        pass
    if 200 <= status < 300 and not error:
        return None
    code = error.get("code", error.get("type"))
    if type(code) is int and 400 <= code <= 599:
        # Some compatible services return the documented error envelope on 200.
        if 200 <= status < 300:
            status = code
    elif not isinstance(code, str) or code not in _SAFE_ERROR_CODES:
        code = None
    raw_message = error.get("message")
    message = raw_message.lower() if isinstance(raw_message, str) else ""
    if status == 401:
        kind = "bad_key"
    elif status == 402:
        kind = "out_of_credit"
    elif status == 403:
        auth = code in {"invalid_api_key", "authentication_error"} or any(
            text in message for text in ("invalid api key", "invalid api_key", "invalid key", "invalid credentials"))
        policy = code in {"content_filter", "moderation_error", "budget_exceeded"} or any(
            text in message for text in ("policy", "moderation", "budget", "spending limit"))
        kind = "bad_key" if auth or (provider != "openrouter" and not policy) else "blocked"
    elif status == 404:
        kind = "model_unavailable"
    elif status == 429:
        kind = "rate_limited"
    elif status == 408:
        kind = "timeout"
    elif 500 <= status <= 599:
        kind = "provider_down"
    else:
        kind = "error"
    return make_failure(kind, provider, http_status, _retry_after(response), code)


def failure_from_exception(provider, exc):
    if isinstance(exc, _CloudRequestFailed):
        return exc.failure
    response = getattr(exc, "response", None)
    if response is not None:
        failure = failure_from_response(provider, response)
        if failure is not None:
            return failure
    if isinstance(exc, requests.exceptions.Timeout):
        kind = "timeout"
    elif isinstance(exc, (requests.exceptions.ConnectionError, ai_transport.AITransportRefused)):
        kind = "unreachable"
    else:
        kind = "error"
    return make_failure(kind, provider)


def _check_response(base_url, response):
    provider = next((name for name, cfg in BUILTIN_PROVIDERS.items()
                     if urlparse(base_url).hostname == cfg["host"]), "cloud")
    failure = failure_from_response(provider, response)
    if failure is not None:
        raise _CloudRequestFailed(failure)
    response.raise_for_status()


def _error_result(failure, message=None):
    logging.getLogger("Samsara.cloud_llm").warning(
        "[CLOUD-FAILURE] kind=%s status=%s", failure.kind, failure.http_status)
    return CloudError(failure, message)


def resolve_default_model(provider, model, *, log_replacement=False):
    replacement = RETIRED_DEFAULTS.get(provider, {}).get(model)
    if replacement is None:
        return model
    marker = (provider, model)
    if log_replacement and marker not in _reported_retired_defaults:
        _reported_retired_defaults.add(marker)
        logging.getLogger("Samsara.cloud_llm").info(
            "Cloud provider %s: retired default %s replaced by %s for requests",
            provider, model, replacement,
        )
    return replacement


def is_enabled(app):
    cfg = _get_config(app)
    return cfg.get("enabled", False) and bool(cfg.get("api_key", ""))


def _get_config(app):
    return getattr(app, "config", {}).get("cloud_llm", {})


def _get_provider_config(app):
    cfg = _get_config(app)
    provider = cfg.get("provider", "deepseek")
    provider_cfg = BUILTIN_PROVIDERS.get(provider)
    if provider_cfg is None:
        raise ValueError(f"Unsupported cloud LLM provider: {provider!r}")

    # Built-in providers are a closed, UI-selected set. Never honor the old
    # hidden ``cloud_llm.providers`` map: an imported config could otherwise
    # redirect an existing API key and dictated text to an attacker endpoint.
    base_url = provider_cfg["base_url"]
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != provider_cfg["host"]:
        raise ValueError(f"Unsafe endpoint configured for provider {provider!r}")

    legacy_model = cfg.get("anthropic_model") if provider == "anthropic" else None
    model = resolve_default_model(
        provider, cfg.get("model") or legacy_model or provider_cfg["model"],
        log_replacement=True,
    )
    return provider, base_url, model


def _send_internal(system_prompt, user_message, app, timeout=30, messages=None):
    """Return (text, legacy_error_kind, structured CloudError)."""
    cfg = _get_config(app)
    provider = cfg.get("provider", "deepseek")
    api_key = cfg.get("api_key", "")
    if provider not in SUPPORTED_PROVIDERS:
        # STAND-IN COPY (566)
        return None, "error", _error_result(make_failure("error", provider),
                                           "Cloud LLM request failed: Unsupported cloud LLM provider.")
    if not api_key:
        return None, "error", _error_result(make_failure("not_configured", provider))
    timeout = cfg.get("timeout_seconds", timeout)
    max_tokens = cfg.get("max_tokens", 300)
    try:
        provider, base_url, model = _get_provider_config(app)
        if provider == "anthropic":
            text = _send_anthropic(base_url, api_key, model, system_prompt,
                                   user_message, timeout, max_tokens, messages=messages)
        else:
            text = _send_openai_compatible(base_url, api_key, model, system_prompt,
                                          user_message, timeout, max_tokens, messages=messages)
        return text, None, None
    except Exception as exc:
        failure = failure_from_exception(provider, exc)
        if isinstance(exc, ai_transport.AITransportRefused):
            ai_transport.notify_refusal(app, exc)
        # STAND-IN COPY (566): retain legacy safe text for existing callers.
        message = (f"Cloud LLM request timed out after {timeout}s." if failure.kind == "timeout" else
                   str(exc) if isinstance(exc, ai_transport.AITransportRefused) else None)
        return None, "timeout" if failure.kind == "timeout" else "error", _error_result(failure, message)


def send(system_prompt, user_message, app, timeout=30, messages=None):
    """
    Send a request to the configured cloud LLM.
    Returns the response text string, or an error string starting with
    "Error:" on failure.

    If messages is provided it is used as the full messages array (including
    system prompt and conversation history). Otherwise falls back to a single
    system + user turn (backward-compatible with callers that don't use memory).

    Handles two API formats:
    - OpenAI-compatible (DeepSeek, OpenAI, OpenRouter): POST /chat/completions
    - Anthropic: POST /messages (different request/response shape)
    """
    text, error_kind, error_message = _send_internal(
        system_prompt, user_message, app, timeout=timeout, messages=messages,
    )
    if error_kind is not None:
        return error_message
    return text


def send_ex(system_prompt, user_message, app, timeout=30, messages=None):
    """Structured variant of send(): returns (text_or_None, error_kind)
    instead of encoding failure as an "Error: ..." string to substring-
    match. error_kind is None on success, 'timeout' for a request timeout,
    'error' for anything else. Same internals as send() (_send_internal)
    -- callers that need to distinguish a timeout from other failures
    without matching "timed out" in an error string should use this
    instead (see samsara.smart_corrections._call_cloud). send() itself is
    untouched -- other callers (ask_ollama, etc.) keep its exact existing
    string-return contract.
    """
    text, error_kind, _error_message = _send_internal(
        system_prompt, user_message, app, timeout=timeout, messages=messages,
    )
    return text, error_kind


def _send_openai_compatible(base_url, api_key, model, system_prompt,
                             user_message, timeout, max_tokens=300, messages=None):
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if messages is None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    response = ai_transport.post_cloud(url, json=payload, headers=headers, timeout=timeout)
    _check_response(base_url, response)
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _send_anthropic(base_url, api_key, model, system_prompt,
                    user_message, timeout, max_tokens=300, messages=None):
    url = f"{base_url}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    # Anthropic requires system content in a top-level field, not in messages.
    if messages is not None:
        system = next(
            (m["content"] for m in messages if m["role"] == "system"),
            system_prompt,
        )
        api_messages = [m for m in messages if m["role"] != "system"]
    else:
        system = system_prompt
        api_messages = [{"role": "user", "content": user_message}]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": api_messages,
    }
    response = ai_transport.post_cloud(url, json=payload, headers=headers, timeout=timeout)
    _check_response(base_url, response)
    data = response.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return " ".join(text_blocks).strip()


# ---------------------------------------------------------------------------
# Web search through DeepSeek's Anthropic-compatible endpoint (queue 59)
# ---------------------------------------------------------------------------
#
# DeepSeek's chat-completions endpoint has no hosted search. Its
# Anthropic-compatible endpoint (api-docs.deepseek.com/guides/anthropic_api)
# lists the server_tool_use and web_search_tool_result content types as
# supported: the model decides to search, DeepSeek runs the search and the
# page reads server-side, and returns the answer with the result blocks.
# Same API key. Additive -- send() and the chat-completions path are untouched,
# and this runs only when the user turned web search on (cloud_llm.web_search).
#
# Everything this returns that came from the web (answer text, titles, URLs)
# is UNTRUSTED DATA. It is for speaking a summary and showing sources, never
# for parsing into commands -- see plugins/commands/ask_ollama.py
# _deliver_search_answer, the only consumer.

DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_ANTHROPIC_HOST = "api.deepseek.com"
# Anthropic's server-tool name. DeepSeek's docs confirm the result block types
# but do not print the tool type string; this is the name the Claude Code
# integration they document sends.
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_SEARCH_TOOL_NAME = "web_search"
SEARCH_DEFAULT_MODEL = "deepseek-flash"
SEARCH_DEFAULT_MAX_USES = 3
SEARCH_DEFAULT_MAX_TOKENS = 1024
_SEARCH_MAX_CONTINUATIONS = 2       # stop_reason "pause_turn" re-sends, bounded
_MAX_SOURCES = 8
_MAX_URL_LEN = 2048
_MAX_TITLE_LEN = 200


class SearchSource:
    """One cited or returned web source. url is http(s) only (validated)."""

    __slots__ = ("url", "title", "cited")

    def __init__(self, url, title, cited):
        self.url = url
        self.title = title
        self.cited = cited

    @property
    def domain(self):
        return urlparse(self.url).hostname or ""

    def __repr__(self):
        return f"SearchSource({self.url!r}, cited={self.cited})"


class SearchResult:
    """Outcome of one send_web_search() call.

    error_kind: None on success, else 'timeout', 'unreachable', 'http'
    (http_status set), 'search_error' (DeepSeek returned no answer and a
    web_search_tool_result_error; search_error_code set) or 'error'."""

    def __init__(self, text=None, sources=None, searched=False, search_requests=0,
                 error_kind=None, http_status=None, search_error_code=None, queries=None, failure=None):
        self.text = text
        self.sources = sources or []
        self.searched = searched
        self.search_requests = search_requests
        self.error_kind = error_kind
        self.http_status = http_status
        self.search_error_code = search_error_code
        self.queries = queries or []
        self.failure = failure

    @property
    def ok(self):
        return self.error_kind is None and bool(self.text)


def web_search_available(app):
    """Web search applies only to Cloud AI with DeepSeek, enabled and keyed,
    AND the user's explicit cloud_llm.web_search opt-in. Local (Ollama) and
    other providers never use it."""
    cfg = _get_config(app)
    return (is_enabled(app)
            and cfg.get("provider", "deepseek") == "deepseek"
            and bool(cfg.get("web_search", False)))


def _clean_title(title):
    text = "".join(ch for ch in str(title or "") if ch.isprintable())
    text = " ".join(text.split())
    return text[:_MAX_TITLE_LEN]


def _valid_url(url):
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
        return None
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return url


def parse_search_response(data):
    """Parse one Anthropic Messages response body into
    (answer_text, sources, searched, queries, search_error_code, stop_reason).

    Answer text is the text blocks AFTER the last web_search_tool_result (the
    model's pre-search preamble, "Let me look that up", is dropped); if no
    text follows a result, all text blocks are used. Sources: cited URLs
    first (text-block citations), then the remaining result URLs, deduped,
    http(s) only, capped."""
    content = data.get("content") or []
    last_result_idx = -1
    for i, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") == "web_search_tool_result":
            last_result_idx = i
    searched = any(isinstance(b, dict) and b.get("type") in ("server_tool_use", "web_search_tool_result")
                   for b in content)

    texts, cited, results, queries = [], [], [], []
    search_error_code = None
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            if i > last_result_idx or last_result_idx < 0:
                texts.append(str(block.get("text") or ""))
            for cit in block.get("citations") or []:
                if isinstance(cit, dict):
                    cited.append((cit.get("url"), cit.get("title")))
        elif kind == "server_tool_use":
            query = (block.get("input") or {}).get("query")
            if isinstance(query, str):
                queries.append(query[:200])
        elif kind == "web_search_tool_result":
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "web_search_tool_result_error":
                search_error_code = str(inner.get("error_code") or "unknown")
            elif isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict) and item.get("type") == "web_search_result":
                        results.append((item.get("url"), item.get("title")))
    if not "".join(texts).strip() and last_result_idx >= 0:
        texts = [str(b.get("text") or "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]

    sources, seen = [], set()
    for is_cited, pairs in ((True, cited), (False, results)):
        for url, title in pairs:
            url = _valid_url(url)
            if url is None or url in seen:
                continue
            seen.add(url)
            sources.append(SearchSource(url, _clean_title(title) or urlparse(url).hostname, is_cited))
            if len(sources) >= _MAX_SOURCES:
                break
    answer = "".join(texts).strip()
    return answer, sources, searched, queries, search_error_code, data.get("stop_reason")


def send_web_search(system_prompt, messages, app, http_post=None):
    """One Ava turn through DeepSeek's Anthropic-compatible endpoint with the
    web_search server tool offered. `messages` is the AvaMemory list
    (a leading system message is lifted into the top-level field). Never
    raises; see SearchResult."""
    from functools import partial
    post = partial(ai_transport.post_cloud, request_fn=http_post)
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    if not api_key:
        return SearchResult(error_kind="http", http_status=401,
                            failure=make_failure("not_configured", "deepseek", 401))
    parsed = urlparse(DEEPSEEK_ANTHROPIC_BASE_URL)
    if parsed.scheme != "https" or parsed.hostname != DEEPSEEK_ANTHROPIC_HOST:
        return SearchResult(error_kind="error", failure=make_failure("error", "deepseek"))

    timeout = cfg.get("timeout_seconds", 30)
    try:
        max_uses = max(1, min(10, int(cfg.get("search_max_uses", SEARCH_DEFAULT_MAX_USES))))
    except (TypeError, ValueError):
        max_uses = SEARCH_DEFAULT_MAX_USES
    try:
        max_tokens = max(256, min(4096, int(cfg.get("search_max_tokens", SEARCH_DEFAULT_MAX_TOKENS))))
    except (TypeError, ValueError):
        max_tokens = SEARCH_DEFAULT_MAX_TOKENS
    model = cfg.get("search_model") or SEARCH_DEFAULT_MODEL

    system = system_prompt
    api_messages = []
    for m in messages or []:
        if m.get("role") == "system":
            system = m.get("content") or system
        elif m.get("role") in ("user", "assistant"):
            api_messages.append({"role": m["role"], "content": m.get("content", "")})

    url = f"{DEEPSEEK_ANTHROPIC_BASE_URL}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    tool = {"type": WEB_SEARCH_TOOL_TYPE, "name": WEB_SEARCH_TOOL_NAME, "max_uses": max_uses}

    all_texts, all_sources, all_queries = [], [], []
    searched, search_requests, search_error_code = False, 0, None
    try:
        for _attempt in range(_SEARCH_MAX_CONTINUATIONS + 1):
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": api_messages,
                "tools": [tool],
            }
            response = post(url, json=payload, headers=headers, timeout=timeout)
            status = int(getattr(response, "status_code", 0) or 0)
            failure = failure_from_response("deepseek", response)
            if failure is not None:
                _error_result(failure)
                return SearchResult(error_kind="http", http_status=status, failure=failure)
            data = response.json()
            answer, sources, did_search, queries, err_code, stop_reason = parse_search_response(data)
            usage = (data.get("usage") or {}).get("server_tool_use") or {}
            try:
                search_requests += int(usage.get("web_search_requests", 0) or 0)
            except (TypeError, ValueError):
                pass
            searched = searched or did_search
            search_error_code = search_error_code or err_code
            if answer:
                all_texts.append(answer)
            all_queries.extend(queries)
            known = {s.url for s in all_sources}
            all_sources.extend(s for s in sources if s.url not in known)
            if stop_reason != "pause_turn":
                break
            # Long-running server tool turn: continue it by sending the
            # assistant content back, bounded.
            api_messages = api_messages + [{"role": "assistant", "content": data.get("content") or []}]
    except ai_transport.AITransportRefused as exc:
        ai_transport.notify_refusal(app, exc)
        return SearchResult(error_kind="unreachable", failure=make_failure("unreachable", "deepseek"))
    except requests.exceptions.Timeout:
        return SearchResult(error_kind="timeout", failure=make_failure("timeout", "deepseek"))
    except requests.exceptions.ConnectionError:
        return SearchResult(error_kind="unreachable", failure=make_failure("unreachable", "deepseek"))
    except Exception:
        return SearchResult(error_kind="error", failure=make_failure("error", "deepseek"))

    text = "\n\n".join(t for t in all_texts if t).strip()
    if not text:
        return SearchResult(searched=searched, search_requests=search_requests,
                             error_kind="search_error" if search_error_code else "error",
                             search_error_code=search_error_code, queries=all_queries,
                             failure=make_failure("rate_limited" if search_error_code == "too_many_requests" else "error", "deepseek"))
    return SearchResult(text=text, sources=all_sources[:_MAX_SOURCES], searched=searched,
                        search_requests=search_requests, search_error_code=search_error_code,
                        queries=all_queries)


def send_json(system_prompt, user_message, app):
    """Like send() but optimised for constrained JSON output.

    Uses temperature=0 and response_format=json_object for OpenAI-compatible
    providers. Anthropic has no JSON mode; the prompt carries the constraint.
    Returns the response text string, or an "Error:..." string on failure.
    """
    cfg = _get_config(app)
    provider = cfg.get("provider", "deepseek")
    api_key = cfg.get("api_key", "")
    if not api_key:
        return _error_result(make_failure("not_configured", provider))

    timeout = cfg.get("timeout_seconds", 30)
    max_tokens = cfg.get("max_tokens", 300)

    try:
        provider, base_url, model = _get_provider_config(app)
        if provider == "anthropic":
            return _send_anthropic(base_url, api_key, model, system_prompt,
                                   user_message, timeout, max_tokens)
        else:
            return _send_openai_json(base_url, api_key, model, system_prompt,
                                     user_message, timeout, max_tokens)
    except Exception as exc:
        failure = failure_from_exception(provider, exc)
        if isinstance(exc, ai_transport.AITransportRefused):
            ai_transport.notify_refusal(app, exc)
        # STAND-IN COPY (566)
        message = (f"Cloud LLM request timed out after {timeout}s." if failure.kind == "timeout" else
                   str(exc) if isinstance(exc, ai_transport.AITransportRefused) else None)
        return _error_result(failure, message)


def _send_openai_json(base_url, api_key, model, system_prompt,
                      user_message, timeout, max_tokens=300):
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = ai_transport.post_cloud(url, json=payload, headers=headers, timeout=timeout)
    _check_response(base_url, response)
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


@dataclass(frozen=True)
class OpenRouterCheck:
    failure: CloudFailure | None = None
    key_valid: bool = False
    limit: float | None = None
    limit_remaining: float | None = None
    usage_reset: str | None = None
    is_free_tier: bool | None = None
    model_note: str = "Model not checked."  # STAND-IN COPY (569)

    def status_text(self):
        # STAND-IN COPY (569): never interpolate account or catalogue text.
        from samsara.ava_readiness import failure_sentence
        if self.failure:
            if self.failure.kind == "not_configured":
                return "Select the cloud route and accept consent before checking OpenRouter."
            text = failure_sentence(self.failure.kind, "openrouter", self.failure.retry_after_seconds)
            return text + " " + self.model_note if self.failure.kind == "model_unavailable" else text
        return "Key works. " + self.model_note + " Ava is proven ready only after a real turn."


def _key_number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def check_openrouter(app, *, http_get=None, timeout=3, catalogue=None, needs_tools=False):
    """A free, consent-gated key check; catalogue inspection never fetches."""
    from copy import deepcopy
    from types import SimpleNamespace
    from datetime import date
    from samsara import ai_preferences, openrouter_catalog
    from samsara.paths import samsara_home_dir
    configured = SimpleNamespace(config=deepcopy(getattr(app, "config", {}) or {}))
    cfg = _get_config(configured)
    if (cfg.get("provider") != "openrouter" or not is_enabled(configured)
            or configured.config.get("ava", {}).get("provider_policy") != "cloud"
            or not ai_preferences.runtime_state(configured.config, "ava").allowed):
        return OpenRouterCheck(make_failure("not_configured", "openrouter"))
    response = None
    try:
        response = ai_transport.get_cloud("https://openrouter.ai/api/v1/key", timeout=timeout,
            headers={"Authorization": "Bearer " + cfg["api_key"]}, request_fn=http_get)
        failure = failure_from_response("openrouter", response)
        if failure:
            return OpenRouterCheck(failure)
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return OpenRouterCheck(make_failure("error", "openrouter"))
        limit, remaining = (_key_number(data.get(k)) for k in ("limit", "limit_remaining"))
        reset = data.get("limit_reset", data.get("usage_reset"))
        reset = reset if reset in ("daily", "weekly", "monthly") else None
        free = data.get("is_free_tier")
        free = free if type(free) is bool else None
        if remaining is not None and remaining <= 0:
            return OpenRouterCheck(make_failure("out_of_credit", "openrouter"), True,
                                   limit, remaining, reset, free)
        if catalogue is None:
            catalogue = openrouter_catalog.read_cache(samsara_home_dir() / "openrouter_catalog.json", time.time())
        model_id = _get_provider_config(configured)[2]
        if (not isinstance(model_id, str) or not 0 < len(model_id) <= 256
                or "/" not in model_id or any(c.isspace() or ord(c) < 32 for c in model_id)):
            return OpenRouterCheck(make_failure("model_unavailable", "openrouter"), True,
                limit, remaining, reset, free, "Selected model ID is unknown or invalid.")  # STAND-IN COPY (569)
        model = next((row for row in catalogue.models if row["id"] == model_id), None)
        note, failure = "Custom or uncached model not checked.", None  # STAND-IN COPY (569)
        if model is not None:
            expiration = model.get("expiration_date")
            arch = model.get("architecture", {})
            if expiration and date.fromisoformat(expiration) <= date.today():
                note, failure = "Selected model has expired.", make_failure("model_unavailable", "openrouter")  # STAND-IN COPY (569)
            elif any(not arch.get(k) for k in ("input_modalities", "output_modalities")):
                note = "Selected model text capabilities not checked."  # STAND-IN COPY (569)
            elif any("text" not in arch[k] for k in ("input_modalities", "output_modalities")):
                note = "Selected model does not support text input and output."  # STAND-IN COPY (569)
                failure = make_failure("model_unavailable", "openrouter")
            elif needs_tools and "tools" not in model.get("supported_parameters", []):
                note, failure = "Selected model does not support tools.", make_failure("model_unavailable", "openrouter")  # STAND-IN COPY (569)
            else:
                note = "Selected model supports this path."  # STAND-IN COPY (569)
        return OpenRouterCheck(failure, True, limit, remaining, reset, free, note)
    except Exception as exc:
        return OpenRouterCheck(failure_from_exception("openrouter", exc))
    finally:
        if response is not None and callable(getattr(response, "close", None)):
            response.close()


def check_available(app):
    """Authenticated check: (True, provider_name) or (False, safe reason)."""
    from samsara import ava_readiness as readiness
    # STAND-IN COPY (566); only classified reasons, never raw provider errors.
    reasons = {readiness.BAD_KEY: "The key was rejected.",
               readiness.OUT_OF_CREDIT: "The account is out of balance.",
               readiness.TIMEOUT: "Did not answer in time.",
               readiness.UNREACHABLE: "Could not be reached.",
               readiness.PROVIDER_ERROR: "The provider returned an error.",
               "provider_down": "The provider returned an error.",
               "blocked": "Blocked by provider policy or budget.",
               "model_unavailable": "The model or provider route is unavailable."}
    if not is_enabled(app):
        return False, "Cloud LLM not enabled"
    cfg = _get_config(app)
    if cfg.get("provider") == "openrouter":
        result = check_openrouter(app)
        return result.failure is None, result.status_text()
    api_key = cfg.get("api_key", "")
    try:
        provider, base_url, _model = _get_provider_config(app)
        if provider == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
        r = ai_transport.get_cloud(f"{base_url}/models", timeout=3, headers=headers)
        failure = failure_from_response(provider, r)
        kind = failure.kind if failure else None
        if kind == readiness.RATE_LIMITED:
            return False, readiness.failure_sentence(kind, provider, failure.retry_after_seconds)
        return (False, reasons[kind]) if kind else (True, provider)
    except ai_transport.AITransportRefused as exc:
        ai_transport.notify_refusal(app, exc)
        return False, reasons[readiness.UNREACHABLE]
    except requests.exceptions.Timeout:
        return False, reasons[readiness.TIMEOUT]
    except requests.exceptions.ConnectionError:
        return False, reasons[readiness.UNREACHABLE]
    except Exception:
        return False, reasons[readiness.PROVIDER_ERROR]
