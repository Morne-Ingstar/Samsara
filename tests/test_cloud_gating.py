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
