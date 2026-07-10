"""
Tests locking in the BYOK monetization model: Cloud AI (bring your own key)
must work with just cloud_llm.enabled + api_key -- no premium/supporter
license required anywhere. See samsara/premium.py for why that module still
exists (optional supporter key, unlocks nothing).
"""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import cloud_llm, premium


class _StubApp:
    def __init__(self, cloud_llm_cfg=None, premium_license=""):
        self.config = {
            "cloud_llm": cloud_llm_cfg or {},
            "premium_license": premium_license,
        }


class TestCloudLlmIsEnabledNoLicenseRequired:
    def test_enabled_with_key_and_no_license(self):
        app = _StubApp(cloud_llm_cfg={"enabled": True, "api_key": "sk-real-key"})
        assert cloud_llm.is_enabled(app) is True

    def test_disabled_without_key_even_with_license(self):
        app = _StubApp(
            cloud_llm_cfg={"enabled": True, "api_key": ""},
            premium_license="SAMSARA-AAAA-BBBB-CCCC",
        )
        assert cloud_llm.is_enabled(app) is False

    def test_enabled_flag_false_even_with_key_and_license(self):
        app = _StubApp(
            cloud_llm_cfg={"enabled": False, "api_key": "sk-real-key"},
            premium_license="SAMSARA-AAAA-BBBB-CCCC",
        )
        assert cloud_llm.is_enabled(app) is False


class TestAskOllamaToggleCloudNoLicenseRequired:
    def test_toggle_cloud_works_with_key_and_no_license(self):
        from plugins.commands.ask_ollama import toggle_cloud

        app = _StubApp(cloud_llm_cfg={"enabled": False, "api_key": "sk-real-key"},
                        premium_license="")
        toggle_cloud(app)
        assert app.config["cloud_llm"]["enabled"] is True

    def test_toggle_cloud_blocked_only_by_missing_api_key(self):
        from plugins.commands.ask_ollama import toggle_cloud

        app = _StubApp(cloud_llm_cfg={"enabled": False, "api_key": ""},
                        premium_license="")
        toggle_cloud(app)
        assert app.config["cloud_llm"].get("enabled", False) is False

    def test_ask_ollama_module_does_not_import_premium(self):
        """Regression lock: cloud-path gating must never re-couple to
        samsara.premium. is_premium is no longer imported by this module."""
        import plugins.commands.ask_ollama as ask_ollama
        assert not hasattr(ask_ollama, "is_premium")

    def test_ask_ollama_source_has_no_premium_capability_branch(self):
        import plugins.commands.ask_ollama as ask_ollama
        source = inspect.getsource(ask_ollama)
        assert "premium" not in source.lower()


class TestPremiumModuleNoLongerGatesCapability:
    def test_is_premium_does_not_appear_in_cloud_llm_module(self):
        source = inspect.getsource(cloud_llm)
        assert "premium" not in source.lower()

    def test_validate_key_still_works_for_supporter_key_display(self):
        assert premium.validate_key("SAMSARA-AAAA-BBBB-CCCC") is True
        assert premium.validate_key("not-a-key") is False

    def test_has_supporter_key_alias_matches_is_premium(self):
        assert premium.has_supporter_key is premium.is_premium


class TestSendExStructuredResult:
    """Tribunal Fix 8: send_ex() returns a structured (text, error_kind)
    result instead of encoding failure as an "Error: ..." string a caller
    has to substring-match. Implemented from the same internals as send()
    (_send_internal) -- send() itself must keep its exact existing
    string-return contract for other callers (ask_ollama, etc.)."""

    def _app(self):
        return SimpleNamespace(config={
            "cloud_llm": {"enabled": True, "api_key": "sk-test", "provider": "deepseek"},
        })

    def test_success_returns_text_and_none_error_kind(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "corrected text"}}]}

        monkeypatch.setattr(cloud_llm.requests, 'post', lambda *a, **k: _Resp())

        text, error_kind = cloud_llm.send_ex("sys", "user", self._app())

        assert text == "corrected text"
        assert error_kind is None

    def test_timeout_returns_none_text_and_timeout_error_kind(self, monkeypatch):
        def _raise(*a, **k):
            raise cloud_llm.requests.exceptions.Timeout("simulated")

        monkeypatch.setattr(cloud_llm.requests, 'post', _raise)

        text, error_kind = cloud_llm.send_ex("sys", "user", self._app(), timeout=5)

        assert text is None
        assert error_kind == "timeout"

    def test_connection_error_returns_none_text_and_error_error_kind(self, monkeypatch):
        def _raise(*a, **k):
            raise cloud_llm.requests.exceptions.ConnectionError("simulated")

        monkeypatch.setattr(cloud_llm.requests, 'post', _raise)

        text, error_kind = cloud_llm.send_ex("sys", "user", self._app())

        assert text is None
        assert error_kind == "error"

    def test_missing_api_key_returns_error_kind_not_timeout(self):
        app = SimpleNamespace(config={"cloud_llm": {"enabled": True, "api_key": ""}})

        text, error_kind = cloud_llm.send_ex("sys", "user", app)

        assert text is None
        assert error_kind == "error"

    def test_generic_exception_returns_error_kind_not_timeout(self, monkeypatch):
        def _raise(*a, **k):
            raise ValueError("something else broke")

        monkeypatch.setattr(cloud_llm.requests, 'post', _raise)

        text, error_kind = cloud_llm.send_ex("sys", "user", self._app())

        assert text is None
        assert error_kind == "error"

    def test_send_keeps_exact_string_contract_on_timeout(self, monkeypatch):
        """send() itself must be untouched -- other callers (ask_ollama,
        workflow_capture, ai_command_mode) keep receiving the exact
        "Error: ..." string contract they already handle."""
        def _raise(*a, **k):
            raise cloud_llm.requests.exceptions.Timeout("simulated")

        monkeypatch.setattr(cloud_llm.requests, 'post', _raise)

        result = cloud_llm.send("sys", "user", self._app(), timeout=5)

        assert result == "Error: Cloud LLM request timed out after 5s."

    def test_send_keeps_exact_string_contract_on_missing_key(self):
        app = SimpleNamespace(config={"cloud_llm": {"enabled": True, "api_key": ""}})

        result = cloud_llm.send("sys", "user", app)

        assert result == "Error: No API key configured for cloud LLM."
