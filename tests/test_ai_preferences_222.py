from copy import deepcopy
import ast
from pathlib import Path

import pytest

from samsara import ai_preferences as ai
from samsara.config_defaults import DEFAULTS


def local_config():
    return {"ollama": {"host": "http://127.0.0.1:11434", "model": "installed-model"}}


def cloud_config():
    # A noncredential sentinel: no requests are made by these pure tests.
    return {"cloud_llm": {"provider": "deepseek", "api_key": "fixture-only", "model": "selected-model"}}


def enabled(config, policy="local", features=("ava", "editing")):
    provider = ai.provider_for(config, policy)
    consent = ai.accept_consent(config, features=features, provider=provider,
                                cloud=policy == "cloud", accepted_at="2026-09-18T12:00:00Z")
    return ai.apply_preferences(config, policy=policy, ava="ava" in features, editing="editing" in features,
                                 consent=consent, local_model="chosen-model" if policy == "local" else None)


def test_fresh_defaults_only_requested_changes():
    for key in ("ava_command_session.enabled", "ava_command_session.keep_warm", "ava_edit.enabled"):
        assert DEFAULTS[key] is False
    assert DEFAULTS["ava.provider_policy"] == "off"
    assert DEFAULTS["tts.engine"] == "edge" and DEFAULTS["tts.enabled"] is False
    assert ai.effective_state({}, "ava").status == "off-by-choice"
    assert ai.migrate_ai_preferences({}) == {"ava": {"provider_policy": "off"}}


@pytest.mark.parametrize("policy", ["local", "cloud"])
@pytest.mark.parametrize("features", [("ava",), ("editing",), ("ava", "editing")])
def test_explicit_opt_in_applies_whole_mapping(policy, features):
    original = local_config() if policy == "local" else cloud_config()
    snapshot = deepcopy(original)
    config = enabled(original, policy, features)
    assert original == snapshot
    assert config["ava"]["provider_policy"] == policy
    assert config["ava_command_session"]["enabled"] == ("ava" in features)
    assert config["ava_edit"]["enabled"] == ("editing" in features)
    assert config["command_packs"]["ai"] == ("ava" in features)
    assert config["ollama"]["enabled"] == (policy == "local")
    assert config["cloud_llm"]["enabled"] == (policy == "cloud")
    assert config["ava_command_session"]["keep_warm"] == (policy == "local" and "ava" in features)
    assert config["ava_command_session"]["backend"] == ("ollama" if policy == "local" else "cloud")
    for section, key in [("smart_corrections", "enabled"), ("smart_corrections", "allow_cloud_fallback"),
                          ("cloud_llm", "web_search"), ("ava", "warm_on_boot"), ("smart_actions", "enabled")]:
        assert config[section][key] is False
    for feature in features:
        assert ai.effective_state(config, feature, ready=True).allowed
        assert ai.effective_state(config, feature, ready=False).status == "unavailable"
        assert ai.effective_state(config, feature).status == "setup-pending"
        assert not ai.effective_state(config, feature, provider_route="cloud" if policy == "local" else "local", ready=True).allowed
    if policy == "local":
        assert config["ollama"]["model"] == "chosen-model"
        for feature, section in (("ava", "ava_command_session"), ("editing", "ava_edit")):
            if feature in features:
                assert config[section]["model"] == "chosen-model"


def test_opt_out_keeps_models_credentials_and_consent_without_accepting_again():
    config = enabled(cloud_config(), "cloud")
    off = ai.apply_preferences(config, policy="off")
    for key in ("api_key", "provider", "model"):
        assert off["cloud_llm"][key] == config["cloud_llm"][key]
    assert off["ava"]["consent"] == config["ava"]["consent"]
    assert ai.effective_state(off, "ava", ready=True).status == "off-by-choice"
    assert not off["ava_edit"]["enabled"] and not off["ollama"]["enabled"]


def test_cancelled_and_deferred_never_manufacture_acceptance():
    for requested in ("off", "local", "cloud"):
        config = ai.apply_preferences({}, policy="off", requested_ai=requested)
        assert "consent" not in config["ava"]
        assert config["onboarding"]["requested_ai"] == requested
        assert not ai.effective_state(config, "ava", ready=True).allowed
        assert ai.migrate_ai_preferences(config) == config


def test_saved_key_card_choice_and_wrong_scope_are_not_consent():
    config = cloud_config()
    config["onboarding"] = {"requested_ai": "cloud"}
    with pytest.raises(ValueError, match="consent"):
        ai.apply_preferences(config, policy="cloud", ava=True)
    local = enabled(local_config(), features=("ava",))
    with pytest.raises(ValueError, match="consent"):
        ai.apply_preferences(local, policy="local", editing=True, local_model="chosen-model")
    assert ai.effective_state(local, "editing", ready=True).status == "off-by-choice"


def test_provider_change_requires_new_consent_and_does_not_carry_cloud_grant():
    config = enabled(cloud_config(), "cloud")
    config["cloud_llm"]["provider"] = "openai"
    assert ai.effective_state(config, "ava", ready=True).status == "setup-pending"
    destination = ai.provider_for(local_config(), "local")
    changed = ai.accept_consent(config, features=("ava",), provider=destination, accepted_at="later")
    assert changed["cloud_version"] is None and changed["features"] == ["ava"]


@pytest.mark.parametrize("endpoint, addresses, location", [
    ("http://127.0.0.1:11434", (), "loopback"), ("http://[::1]:11434", (), "loopback"),
    ("http://192.168.1.5:11434", (), "remote"), ("https://example.test", (), "unverified"),
    ("http://localhost:11434", (), "unverified"),
    ("http://localhost:11434", ("127.0.0.1", "::1"), "loopback"),
    ("http://localhost:11434", ("127.0.0.1", "192.168.1.5"), "remote"),
])
def test_local_means_verified_loopback(endpoint, addresses, location):
    provider = ai.identify_provider("ollama", endpoint, resolved_addresses=addresses)
    assert provider.location == location
    cfg = {"ollama": {"host": endpoint}}
    consent = ai.accept_consent(cfg, provider=provider)
    if location != "loopback":
        with pytest.raises(ValueError, match="loopback"):
            ai.apply_preferences(cfg, policy="local", ava=True, consent=consent, local_model="selected")
    else:
        result = ai.apply_preferences(cfg, policy="local", ava=True, consent=consent,
                                      local_model="selected", resolved_addresses=addresses)
        assert ai.effective_state(result, "ava", ready=True, resolved_addresses=addresses).allowed


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://name:secret@example.test", "https://example.test?token=x",
                                  "https://example.test#secret", "http://127.0.0.1:99999", "http://127.0.0.1:0", "http://127.0.0.1\\evil"])
def test_provider_records_reject_credential_bearing_and_invalid_urls(url):
    with pytest.raises(ValueError):
        ai.identify_provider("ollama", url)


@pytest.mark.parametrize("config", [{}, {"ollama": {"enabled": True}},
    {"ava_command_session": {"enabled": False}, "ava_edit": {"enabled": False}},
    {"cloud_llm": {"enabled": True, "provider": "deepseek", "api_key": "fixture-only"}},
    {"ava": {"provider_policy": "off"}, "ollama": {"enabled": True}}])
def test_migration_is_idempotent_and_never_accepts(config):
    original = deepcopy(config)
    migrated = ai.migrate_ai_preferences(config)
    assert config == original
    assert ai.migrate_ai_preferences(migrated) == migrated
    assert "consent" not in migrated["ava"]


def test_existing_ava_and_v1_consent_keep_original_grant_not_editing():
    config = local_config()
    config["ollama"]["enabled"] = True
    config["ava"] = {"consent": {"version": 1, "cloud_version": None, "accepted_at": "original-time"}}
    migrated = ai.migrate_ai_preferences(config)
    assert migrated["ava"]["provider_policy"] == "local"
    assert migrated["ava_command_session"]["enabled"] is True
    assert migrated["ava_command_session"]["keep_warm"] is True
    assert migrated["ava_edit"]["enabled"] is True
    assert migrated["ava"]["consent"]["accepted_at"] == "original-time"
    assert ai.effective_state(migrated, "ava", ready=True).allowed
    assert ai.effective_state(migrated, "editing", ready=True).status == "setup-pending"
    migrated["ollama"]["host"] = "http://127.0.0.1:11500"
    assert ai.effective_state(migrated, "ava", ready=True).status == "setup-pending"


def test_partial_legacy_profile_keeps_old_fallbacks_but_v2_deferred_is_off():
    old = ai.migrate_ai_preferences({"first_run_complete": True})
    assert old["ava"]["provider_policy"] == "local"
    assert old["ava_command_session"]["enabled"] and old["ava_edit"]["enabled"]
    assert "consent" not in old["ava"]
    deferred = ai.migrate_ai_preferences({"onboarding": {"version": 2, "requested_ai": "cloud"}})
    assert deferred["ava"]["provider_policy"] == "off"


def test_v1_cloud_scope_keeps_timestamp_but_changed_provider_needs_acceptance():
    old = cloud_config()
    old["cloud_llm"]["enabled"] = True
    old["ava"] = {"consent": {"version": 1, "cloud_version": 1, "accepted_at": "original-time"}}
    migrated = ai.migrate_ai_preferences(old)
    assert migrated["ava"]["provider_policy"] == "cloud"
    assert ai.effective_state(migrated, "ava", ready=True).allowed
    assert not ai.effective_state(migrated, "editing", ready=True).allowed
    migrated["cloud_llm"]["provider"] = "anthropic"
    assert not ai.effective_state(migrated, "ava", ready=True).allowed


@pytest.mark.parametrize("kwargs", [{"policy": "local"}, {"policy": "off", "ava": True},
    {"policy": "invalid"}, {"policy": "off", "requested_ai": "remote"},
    {"policy": "local", "requested_ai": "cloud"}])
def test_invalid_transaction_leaves_original_unchanged(kwargs):
    config = local_config()
    snapshot = deepcopy(config)
    with pytest.raises(ValueError):
        ai.apply_preferences(config, **kwargs)
    assert config == snapshot


def test_new_modules_have_no_qt_or_dictation_imports():
    root = Path(__file__).resolve().parents[1] / "samsara"
    for path in [root / "ai_preferences.py", *(root / "onboarding").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("PySide6", "dictation", "samsara.ui"))
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith(("PySide6", "dictation", "samsara.ui")) for alias in node.names)


def test_consent_wrapper_tracks_explicit_feature_scope_and_provider():
    from samsara.ui.ava_consent_qt import accepted_consent, consent_required
    cfg = local_config()
    record = accepted_consent(cfg, features=("editing",), accepted_at="explicit-time")
    cfg["ava"] = {"consent": record}
    assert not consent_required(cfg, features=("editing",))
    assert consent_required(cfg)  # Old callers ask only for Ava.
    cfg["ollama"]["host"] = "http://127.0.0.1:11500"
    assert consent_required(cfg, features=("editing",))


@pytest.mark.parametrize("record", [None, {}, {"version": 2, "accepted_at": None},
    {"version": 2, "accepted_at": "", "features": ["ava"]}])
def test_invalid_consent_never_authorizes(record):
    assert not ai.consent_covers(record, features=("ava",), provider=ai.provider_for(local_config(), "local"), cloud=False)


def test_acceptance_cannot_promote_unaccepted_feature_grants():
    config = local_config()
    provider = ai.provider_for(config, "local")
    config["ava"] = {"consent": {"version": 2, "accepted_at": None,
        "features": ["editing"], "provider": provider.record(), "cloud_version": 2}}
    record = ai.accept_consent(config, features=("ava",), provider=provider)
    assert record["features"] == ["ava"]
    assert record["cloud_version"] is None
    assert not ai.consent_covers(record, features=("editing",), provider=provider, cloud=False)


def test_explicit_acceptance_retains_previously_accepted_scope_same_origin():
    config = enabled(local_config(), features=("ava",))
    provider = ai.provider_for(config, "local")
    record = ai.accept_consent(config, features=("editing",), provider=provider)
    assert record["features"] == ["ava", "editing"]
