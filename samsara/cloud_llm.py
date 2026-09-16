"""
Cloud LLM provider for Ava. Sends requests to external API endpoints
(DeepSeek, OpenAI, Anthropic, OpenRouter) as an alternative to local Ollama.

User provides their own API key. Data leaves the machine when enabled.
Samsara does not store conversation content beyond the local session.
"""

import requests
from urllib.parse import urlparse


BUILTIN_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "host": "api.deepseek.com",
        "model": "deepseek-chat",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "host": "api.openai.com",
        "model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "host": "api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "host": "openrouter.ai",
        "model": "openrouter/auto",
    },
}
SUPPORTED_PROVIDERS = frozenset(BUILTIN_PROVIDERS)


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
    model = cfg.get("model") or legacy_model or provider_cfg["model"]
    return provider, base_url, model


def _send_internal(system_prompt, user_message, app, timeout=30, messages=None):
    """Shared implementation for send()/send_ex(). Returns
    (text_or_None, error_kind, error_message): error_kind is None on
    success (error_message is also None then), 'timeout' for a request
    timeout, or 'error' for anything else (missing API key, connection
    failure, HTTP error, ...). error_message is the human-readable detail
    used to build send()'s "Error: ..." string -- never includes the
    "Error: " prefix itself, so both send() and send_ex() derive their
    return value from the exact same classification.
    """
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    if not api_key:
        return None, "error", "No API key configured for cloud LLM."

    timeout = cfg.get("timeout_seconds", timeout)
    max_tokens = cfg.get("max_tokens", 300)

    try:
        provider, base_url, model = _get_provider_config(app)
        if provider == "anthropic":
            text = _send_anthropic(base_url, api_key, model, system_prompt,
                                   user_message, timeout, max_tokens,
                                   messages=messages)
        else:
            text = _send_openai_compatible(base_url, api_key, model,
                                           system_prompt, user_message,
                                           timeout, max_tokens,
                                           messages=messages)
        return text, None, None
    except requests.exceptions.ConnectionError:
        return None, "error", "Could not connect to the cloud LLM provider."
    except requests.exceptions.Timeout:
        return None, "timeout", f"Cloud LLM request timed out after {timeout}s."
    except Exception as e:
        return None, "error", f"Cloud LLM request failed: {e}"


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
        return f"Error: {error_message}"
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
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
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
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
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
                 error_kind=None, http_status=None, search_error_code=None, queries=None):
        self.text = text
        self.sources = sources or []
        self.searched = searched
        self.search_requests = search_requests
        self.error_kind = error_kind
        self.http_status = http_status
        self.search_error_code = search_error_code
        self.queries = queries or []

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
    post = http_post or requests.post
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    if not api_key:
        return SearchResult(error_kind="http", http_status=401)
    parsed = urlparse(DEEPSEEK_ANTHROPIC_BASE_URL)
    if parsed.scheme != "https" or parsed.hostname != DEEPSEEK_ANTHROPIC_HOST:
        return SearchResult(error_kind="error")

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
            if status < 200 or status >= 300:
                return SearchResult(error_kind="http", http_status=status)
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
    except requests.exceptions.Timeout:
        return SearchResult(error_kind="timeout")
    except requests.exceptions.ConnectionError:
        return SearchResult(error_kind="unreachable")
    except Exception:
        return SearchResult(error_kind="error")

    text = "\n\n".join(t for t in all_texts if t).strip()
    if not text:
        return SearchResult(searched=searched, search_requests=search_requests,
                             error_kind="search_error" if search_error_code else "error",
                             search_error_code=search_error_code, queries=all_queries)
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
    api_key = cfg.get("api_key", "")
    if not api_key:
        return "Error: No API key configured for cloud LLM."

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
    except requests.exceptions.ConnectionError:
        return "Error: Could not connect to the cloud LLM provider."
    except requests.exceptions.Timeout:
        return f"Error: Cloud LLM request timed out after {timeout}s."
    except Exception as e:
        return f"Error: Cloud LLM request failed: {e}"


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
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def check_available(app):
    """Quick health check. Returns (True, provider_name) or (False, error_string)."""
    if not is_enabled(app):
        return False, "Cloud LLM not enabled"
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    try:
        provider, base_url, model = _get_provider_config(app)
        if provider == "anthropic":
            r = requests.get(base_url.rstrip('/'), timeout=3)
        else:
            r = requests.get(f"{base_url}/models", timeout=3,
                             headers={"Authorization": f"Bearer {api_key}"})
        return True, provider
    except Exception as e:
        return False, str(e)
