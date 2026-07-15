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
