"""Focused coverage for selectable Ava cloud providers."""

from types import SimpleNamespace

from samsara import cloud_llm
from samsara.ai_capability import get_settings_constraints


def _app(provider="openrouter", **overrides):
    cloud = {
        "enabled": True,
        "api_key": "test-key",
        "provider": provider,
        **overrides,
    }
    return SimpleNamespace(config={"cloud_llm": cloud})


def test_openrouter_is_a_supported_config_provider():
    providers = get_settings_constraints()["cloud_llm.provider"]["options"]
    assert "openrouter" in providers


def test_openrouter_default_uses_official_compatible_endpoint_and_auto_router():
    assert cloud_llm._get_provider_config(_app()) == (
        "openrouter",
        "https://openrouter.ai/api/v1",
        "openrouter/auto",
    )


def test_openrouter_model_override_is_preserved():
    provider, base_url, model = cloud_llm._get_provider_config(
        _app(model="anthropic/claude-sonnet-4")
    )
    assert provider == "openrouter"
    assert base_url == "https://openrouter.ai/api/v1"
    assert model == "anthropic/claude-sonnet-4"


def test_hidden_provider_endpoint_override_is_ignored():
    provider, base_url, model = cloud_llm._get_provider_config(_app(
        providers={
            "openrouter": {
                "base_url": "http://attacker.invalid/collect",
                "model": "attacker/model",
            }
        }
    ))

    assert provider == "openrouter"
    assert base_url == "https://openrouter.ai/api/v1"
    assert model == "openrouter/auto"


def test_unsupported_provider_fails_without_network_request(monkeypatch):
    requests = []
    monkeypatch.setattr(
        cloud_llm.requests,
        "post",
        lambda *args, **kwargs: requests.append((args, kwargs)),
    )

    result = cloud_llm.send("system", "sensitive prompt", _app("attacker"))

    assert result.startswith("Error: Cloud LLM request failed:")
    assert "Unsupported cloud LLM provider" in result
    assert requests == []


def test_openrouter_send_reuses_openai_compatible_request(monkeypatch):
    request = {}

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": " routed "}}]}

    def _post(url, **kwargs):
        request.update(url=url, **kwargs)
        return _Response()

    monkeypatch.setattr(cloud_llm.requests, "post", _post)

    assert cloud_llm.send("system", "hello", _app()) == "routed"
    assert request["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["json"]["model"] == "openrouter/auto"


def test_openrouter_is_selectable_in_ava_cloud_ui(qapp):
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
    from tests.test_settings import _StubApp

    win = _SettingsWindow(_StubApp())
    combo = win._widgets["cloud_provider"]

    assert combo.findText("OpenRouter") >= 0
    combo.setCurrentText("OpenRouter")
    assert win._widgets["cloud_model"].placeholderText() == (
        "Default: openrouter/auto"
    )
    assert "many model providers" in win._widgets["cloud_info_label"].text()

    save_ava = win._save_fns[_TAB_NAMES.index("Ava / Cloud")]
    assert save_ava({})["cloud_llm"]["provider"] == "openrouter"
    win.close()
