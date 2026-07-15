import json

import pytest

from samsara.config_transfer import (
    ConfigTransferError,
    EXPORT_FORMAT,
    EXPORT_VERSION,
    build_export,
    export_config,
    load_config_export,
    merge_import,
)


def test_export_round_trip_keeps_nested_settings_and_private_values(tmp_path):
    config = {
        "microphone": 7,
        "cloud_llm": {"provider": "openrouter", "api_key": "secret"},
    }
    path = export_config(tmp_path / "samsara-backup.json", config)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == EXPORT_FORMAT
    assert payload["version"] == EXPORT_VERSION
    assert payload["contains_private_values"] is True
    assert load_config_export(path) == config


def test_import_accepts_legacy_raw_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model_size": "small"}), encoding="utf-8")
    assert load_config_export(path) == {"model_size": "small"}


def test_import_rejects_wrong_wrapper_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION + 1,
        "config": {},
    }), encoding="utf-8")
    with pytest.raises(ConfigTransferError, match="Unsupported backup version"):
        load_config_export(path)


def test_import_rejects_non_object_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigTransferError, match="must be a JSON object"):
        load_config_export(path)


def test_merge_import_overrides_nested_values_and_preserves_newer_keys():
    current = {
        "model_size": "base",
        "future_setting": True,
        "cloud_llm": {"provider": "deepseek", "timeout_seconds": 30},
    }
    imported = {
        "model_size": "small",
        "cloud_llm": {"provider": "openrouter"},
    }
    assert merge_import(current, imported) == {
        "model_size": "small",
        "future_setting": True,
        "cloud_llm": {"provider": "openrouter", "timeout_seconds": 30},
    }


def test_build_export_does_not_alias_live_config():
    config = {"nested": {"value": 1}}
    payload = build_export(config)
    config["nested"]["value"] = 2
    assert payload["config"]["nested"]["value"] == 1


def test_import_strips_hidden_cloud_endpoint_overrides():
    current = {
        "cloud_llm": {
            "enabled": True,
            "provider": "openrouter",
            "api_key": "existing-secret",
        }
    }
    imported = {
        "cloud_llm": {
            "providers": {
                "openrouter": {
                    "base_url": "http://attacker.invalid/collect",
                }
            }
        }
    }

    merged = merge_import(current, imported)

    assert "providers" not in merged["cloud_llm"]
    assert merged["cloud_llm"]["api_key"] == "existing-secret"


def test_provider_change_without_imported_key_drops_existing_secret():
    current = {
        "cloud_llm": {
            "enabled": True,
            "provider": "openai",
            "api_key": "openai-secret",
        }
    }
    imported = {
        "cloud_llm": {
            "enabled": True,
            "provider": "openrouter",
        }
    }

    merged = merge_import(current, imported)

    assert merged["cloud_llm"]["provider"] == "openrouter"
    assert "api_key" not in merged["cloud_llm"]


def test_provider_change_with_explicit_imported_key_uses_imported_secret():
    current = {
        "cloud_llm": {
            "provider": "openai",
            "api_key": "old-secret",
        }
    }
    imported = {
        "cloud_llm": {
            "provider": "anthropic",
            "api_key": "imported-anthropic-secret",
        }
    }

    merged = merge_import(current, imported)

    assert merged["cloud_llm"]["api_key"] == "imported-anthropic-secret"


def test_import_rejects_unsupported_cloud_provider():
    with pytest.raises(ConfigTransferError, match="Unsupported cloud LLM provider"):
        merge_import(
            {"cloud_llm": {"provider": "deepseek"}},
            {"cloud_llm": {"provider": "custom-attacker"}},
        )
