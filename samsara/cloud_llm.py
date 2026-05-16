"""
Cloud LLM provider for Ava. Sends requests to external API endpoints
(DeepSeek, OpenAI, Anthropic) as an alternative to local Ollama.

User provides their own API key. Data leaves the machine when enabled.
Samsara does not store conversation content beyond the local session.
"""

import requests
import threading

_config_lock = threading.Lock()


def is_enabled(app):
    cfg = _get_config(app)
    return cfg.get("enabled", False) and bool(cfg.get("api_key", ""))


def _get_config(app):
    return getattr(app, "config", {}).get("cloud_llm", {})


def _get_provider_config(app):
    cfg = _get_config(app)
    provider = cfg.get("provider", "deepseek")
    providers = cfg.get("providers", {})
    default_providers = {
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-sonnet-4-20250514",
        },
    }
    provider_cfg = providers.get(provider, default_providers.get(provider, {}))
    model = cfg.get("model") or provider_cfg.get("model", "deepseek-chat")
    base_url = provider_cfg.get("base_url", "https://api.deepseek.com/v1")
    return provider, base_url, model


def send(system_prompt, user_message, app, timeout=30):
    """
    Send a request to the configured cloud LLM.
    Returns the response text string, or an error string starting with
    "Error:" on failure.

    Handles two API formats:
    - OpenAI-compatible (DeepSeek, OpenAI): POST /chat/completions
    - Anthropic: POST /messages (different request/response shape)
    """
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    if not api_key:
        return "Error: No API key configured for cloud LLM."

    provider, base_url, model = _get_provider_config(app)
    timeout = cfg.get("timeout_seconds", timeout)

    try:
        if provider == "anthropic":
            return _send_anthropic(base_url, api_key, model, system_prompt,
                                   user_message, timeout)
        else:
            return _send_openai_compatible(base_url, api_key, model,
                                           system_prompt, user_message,
                                           timeout)
    except requests.exceptions.ConnectionError:
        return "Error: Could not connect to the cloud LLM provider."
    except requests.exceptions.Timeout:
        return f"Error: Cloud LLM request timed out after {timeout}s."
    except Exception as e:
        return f"Error: Cloud LLM request failed: {e}"


def _send_openai_compatible(base_url, api_key, model, system_prompt,
                             user_message, timeout):
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _send_anthropic(base_url, api_key, model, system_prompt,
                    user_message, timeout):
    url = f"{base_url}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": 300,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message},
        ],
    }
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return " ".join(text_blocks).strip()


def check_available(app):
    """Quick health check. Returns (True, provider_name) or (False, error_string)."""
    if not is_enabled(app):
        return False, "Cloud LLM not enabled"
    cfg = _get_config(app)
    api_key = cfg.get("api_key", "")
    provider, base_url, model = _get_provider_config(app)
    try:
        if provider == "anthropic":
            r = requests.get(base_url.rstrip('/'), timeout=3)
        else:
            r = requests.get(f"{base_url}/models", timeout=3,
                             headers={"Authorization": f"Bearer {api_key}"})
        return True, provider
    except Exception as e:
        return False, str(e)
