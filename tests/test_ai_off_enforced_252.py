"""PROMPT 252 -- provider policy is enforced before every model route.

No test imports dictation or contacts a provider.  Each network surface is a
fail-on-call fake so an accidental fallback is immediately visible.
"""
from types import SimpleNamespace
from pathlib import Path

from plugins.commands import ask_ollama
from samsara import ai_preferences
from samsara import ava_readiness
from samsara.ava_edit import model as edit_model
from samsara.vision import VisionBridge


class _Memory:
    def __init__(self):
        self.messages = []

    def add_user(self, text):
        self.messages.append(("user", text))

    def add_assistant(self, text):
        self.messages.append(("assistant", text))

    def pop_last_if_user(self):
        if self.messages and self.messages[-1][0] == "user":
            self.messages.pop()

    def get_messages(self, system, **_kwargs):
        return [{"role": "system", "content": system}, *[
            {"role": role, "content": text} for role, text in self.messages
        ]]

    def save(self):
        pass


def _app(config):
    return SimpleNamespace(config=config, _ava_memory=_Memory())


def _enabled_local(*, ava=True, editing=False):
    config = {"ollama": {"host": "http://127.0.0.1:11434", "model": "fixture"}}
    provider = ai_preferences.provider_for(config, "local")
    consent = ai_preferences.accept_consent(
        config, features=tuple(name for name, on in (("ava", ava), ("editing", editing)) if on),
        provider=provider, accepted_at="2026-09-18T00:00:00Z")
    return ai_preferences.apply_preferences(
        config, policy="local", ava=ava, editing=editing, consent=consent,
        local_model="fixture")


def test_off_makes_ava_route_zero_provider_calls(monkeypatch):
    app = _app({"ava": {"provider_policy": "off"}})
    monkeypatch.setattr(ask_ollama, "_check_ollama_available",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local called")))
    monkeypatch.setattr(ask_ollama.cloud_llm, "send",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cloud called")))

    reply = ask_ollama.ask_model("hello", app)

    assert not reply.ok
    assert reply.provider == "none"


def test_off_blocks_readiness_probe_and_warmup(monkeypatch):
    app = _app({"ava": {"provider_policy": "off"}})
    called = []

    assert ava_readiness.probe_configured_provider(
        app, http_get=lambda *_args, **_kwargs: called.append(True)) == ("none", ava_readiness.NOT_CONFIGURED)
    assert ava_readiness.warm_configured_provider(app) == ava_readiness.tracker.snapshot()
    assert called == []


def test_off_blocks_the_local_vision_model_route(monkeypatch):
    app = _app({"ava": {"provider_policy": "off"}, "vision": {"enabled": True}})
    bridge = VisionBridge(app)
    monkeypatch.setattr("samsara.vision.requests.post",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("vision called")))
    monkeypatch.setattr("samsara.vision.requests.get",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("vision probe")))

    assert bridge.describe("fixture", "describe") is None
    assert bridge.is_available() is False


def test_local_route_never_falls_back_to_cloud(monkeypatch):
    app = _app(_enabled_local())
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cloud fallback")))

    reply = ask_ollama.ask_model("hello", app)

    assert not reply.ok
    assert reply.provider == "ollama"


def test_cloud_without_v2_consent_refuses_before_provider(monkeypatch):
    app = _app({
        "ava": {"provider_policy": "cloud"},
        "ava_command_session": {"enabled": True},
        "cloud_llm": {"enabled": True, "provider": "deepseek", "api_key": "fixture"},
    })
    monkeypatch.setattr(ask_ollama.cloud_llm, "send",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cloud called")))

    reply = ask_ollama.ask_model("hello", app)

    assert not reply.ok
    assert reply.provider == "deepseek"


def test_off_during_delayed_local_reply_drops_it(monkeypatch):
    config = _enabled_local()
    app = _app(config)
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda *_args, **_kwargs: True)

    class _Response:
        def raise_for_status(self):
            config["ava"]["provider_policy"] = "off"

        def json(self):
            return {"message": {"content": "late answer"}}

    monkeypatch.setattr(ask_ollama.requests, "post", lambda *_args, **_kwargs: _Response())

    reply = ask_ollama.ask_model("hello", app)

    assert not reply.ok
    assert app._ava_memory.messages == []
    assert app._ava_cmd_generation == 1


def test_edit_local_never_probes_cloud(monkeypatch):
    app = _app(_enabled_local(ava=False, editing=True))
    monkeypatch.setattr("samsara.smart_corrections._ollama_reachable", lambda _app: False)
    monkeypatch.setattr("samsara.cloud_llm.is_enabled",
                        lambda _app: (_ for _ in ()).throw(AssertionError("cloud probe")))

    assert edit_model.resolve_backend(app) is None


def test_legacy_migration_preserves_prior_choice_without_enabling_fresh_config():
    migrated = ai_preferences.migrate_ai_preferences({
        "first_run_complete": True,
        "ollama": {"enabled": True},
        "ava_command_session": {"enabled": True},
    })

    assert migrated["ava"]["provider_policy"] == "local"
    assert migrated["ava_command_session"]["enabled"] is True
    assert ai_preferences.migrate_ai_preferences({}) == {"ava": {"provider_policy": "off"}}


def test_dictation_boot_warmup_is_guarded_without_importing_dictation():
    """Keep the live app out of this test process while checking its seam."""
    source = Path("dictation.py").read_text(encoding="utf-8-sig")
    warm = source.index("ava_readiness.schedule_warm_on_boot")
    guard = source.rfind('ai_preferences.runtime_state(self.config, "ava").allowed', 0, warm)
    assert guard >= 0
