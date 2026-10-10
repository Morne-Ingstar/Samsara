"""Cloud defaults and exact retired-default migration, without provider calls."""
import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest

from samsara import cloud_llm


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Provider requests are forbidden in these tests")
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", forbidden)
    monkeypatch.setattr(cloud_llm, "_reported_retired_defaults", set())


@pytest.mark.parametrize("provider,expected", [
    ("deepseek", "deepseek-flash"), ("anthropic", "claude-sonnet-5-5"),
    ("openai", "gpt-4o-mini"), ("openrouter", "openrouter/auto"),
])
def test_defaults_and_settings_agree(provider, expected):
    from samsara.ui.settings import ava_cloud_qt
    app = SimpleNamespace(config={"cloud_llm": {"provider": provider}})
    assert cloud_llm.BUILTIN_PROVIDERS[provider]["model"] == expected
    assert cloud_llm._get_provider_config(app)[2] == expected
    assert ava_cloud_qt._DEFAULT_MODELS[provider] == expected
    if provider in cloud_llm.RETIRED_DEFAULTS:
        assert ava_cloud_qt._PROVIDER_INFO[provider].startswith(expected)


@pytest.mark.parametrize("provider", list(cloud_llm.BUILTIN_PROVIDERS))
@pytest.mark.parametrize("old,owner", [
    ("deepseek-chat", "deepseek"), ("claude-sonnet-4-20250514", "anthropic"),
])
def test_retired_model_only_changes_for_its_provider(provider, old, owner):
    config = {"cloud_llm": {"provider": provider, "model": old}}
    original = deepcopy(config)
    actual = cloud_llm._get_provider_config(SimpleNamespace(config=config))[2]
    expected = cloud_llm.BUILTIN_PROVIDERS[owner]["model"] if provider == owner else old
    assert actual == expected
    assert config == original


@pytest.mark.parametrize("provider", list(cloud_llm.BUILTIN_PROVIDERS))
def test_custom_model_is_untouched(provider):
    app = SimpleNamespace(config={"cloud_llm": {
        "provider": provider, "model": "custom/model-name",
    }})
    assert cloud_llm._get_provider_config(app)[2] == "custom/model-name"


def test_legacy_anthropic_model_and_first_use_log(caplog):
    app = SimpleNamespace(config={"cloud_llm": {
        "provider": "anthropic", "anthropic_model": "claude-sonnet-4-20250514",
    }})
    with caplog.at_level(logging.INFO, logger="Samsara.cloud_llm"):
        assert cloud_llm.resolve_default_model(
            "anthropic", "claude-sonnet-4-20250514") == "claude-sonnet-5-5"
        assert not caplog.records
        for _ in range(2):
            assert cloud_llm._get_provider_config(app)[2] == "claude-sonnet-5-5"
    assert len(caplog.records) == 1
    assert "claude-sonnet-5-5" in caplog.text


@pytest.mark.parametrize("provider,old", [
    ("deepseek", "deepseek-chat"), ("anthropic", "claude-sonnet-4-20250514"),
    ("deepseek", "custom/model-name"), ("openai", "deepseek-chat"),
])
def test_settings_displays_resolved_model_without_mutating_config(qapp, provider, old):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp
    app = _StubApp()
    app.config = {"cloud_llm": {"provider": provider, "model": old}}
    original = deepcopy(app.config)
    window = _SettingsWindow(app)
    try:
        assert window._widgets["cloud_model"].text() == cloud_llm.resolve_default_model(provider, old)
        assert app.config == original
    finally:
        window.deleteLater()
