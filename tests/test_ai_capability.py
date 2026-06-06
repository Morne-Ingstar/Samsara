"""
Unit tests for samsara.ai_capability -- capability snapshot and proposal validator.

Tests use a hand-built snapshot so they run without loading any plugins,
touching the filesystem, or needing a live CommandMatcher.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.ai_capability import (
    get_capability_snapshot,
    get_settings_constraints,
    validate_proposal,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_snapshot(extra_commands=None, extra_all_ids=None):
    """Build a minimal but realistic snapshot for testing."""
    composable = {
        "volume up": {
            "description": "Increase system volume by 20%.",
            "pack": "media",
            "risk_class": "safe",
            "ai_composable": True,
            "side_effects": ["audio"],
            "preconditions": [],
            "voice_triggerable": True,
            "aliases": ["louder", "turn it up"],
            "param_schema": {},
        },
        "volume down": {
            "description": "Decrease system volume by 20%.",
            "pack": "media",
            "risk_class": "safe",
            "ai_composable": True,
            "side_effects": ["audio"],
            "preconditions": [],
            "voice_triggerable": True,
            "aliases": ["quieter"],
            "param_schema": {},
        },
        "set brightness": {
            "description": "Set display brightness to a specific level.",
            "pack": "display",
            "risk_class": "safe",
            "ai_composable": True,
            "side_effects": ["display"],
            "preconditions": [],
            "voice_triggerable": True,
            "aliases": [],
            "param_schema": {
                "level": {"type": "int", "min": 0, "max": 100, "required": True},
            },
        },
    }
    if extra_commands:
        composable.update(extra_commands)

    all_ids = list(composable.keys()) + [
        # Present in registry but NOT composable:
        "going dark",
        "focus mode",
        "toggle mute",
    ]
    if extra_all_ids:
        all_ids.extend(extra_all_ids)

    return {
        "version": "test0000deadbeef",
        "commands": composable,
        "all_action_ids": sorted(all_ids),
        "settings": get_settings_constraints(),
    }


SNAPSHOT = _make_snapshot()


# ---------------------------------------------------------------------------
# Tests: get_settings_constraints
# ---------------------------------------------------------------------------

class TestSettingsConstraints:
    def test_returns_dict(self):
        s = get_settings_constraints()
        assert isinstance(s, dict)
        assert len(s) > 0

    def test_required_keys_present(self):
        s = get_settings_constraints()
        assert "ollama.timeout_seconds" in s
        assert "ollama.enabled" in s
        assert "command.trigger_mode" in s

    def test_numeric_entries_have_min_max(self):
        s = get_settings_constraints()
        timeout = s["ollama.timeout_seconds"]
        assert timeout["type"] == "int"
        assert timeout["min"] < timeout["max"]

    def test_enum_entries_have_options(self):
        s = get_settings_constraints()
        mode = s["command.trigger_mode"]
        assert mode["type"] == "enum"
        assert isinstance(mode["options"], list)
        assert len(mode["options"]) >= 2

    def test_returns_independent_copy(self):
        s1 = get_settings_constraints()
        s2 = get_settings_constraints()
        s1["__sentinel__"] = True
        assert "__sentinel__" not in s2


# ---------------------------------------------------------------------------
# Tests: get_capability_snapshot
# ---------------------------------------------------------------------------

class TestGetCapabilitySnapshot:
    def test_empty_snapshot_when_no_matcher(self):
        snap = get_capability_snapshot(None)
        assert snap["commands"] == {}
        assert snap["all_action_ids"] == []
        assert "version" in snap
        assert "settings" in snap

    def test_snapshot_from_fake_matcher(self):
        """Use a duck-typed mock matcher to test snapshot construction."""
        class FakeMatcher:
            def list_commands(self):
                return [
                    {
                        "phrase": "volume up",
                        "source": "plugin",
                        "type": "plugin",
                        "aliases": ["louder"],
                        "pack": "media",
                        "description": "Increase volume.",
                        "ai_visible": True,
                        "risk_class": "safe",
                        "ai_composable": True,
                        "side_effects": ["audio"],
                        "preconditions": [],
                        "voice_triggerable": True,
                        "param_schema": {},
                    },
                    {
                        "phrase": "going dark",
                        "source": "plugin",
                        "type": "plugin",
                        "aliases": ["goodnight"],
                        "pack": "macros",
                        "description": "Mute and lock.",
                        "ai_visible": True,
                        "risk_class": "destructive",
                        "ai_composable": False,   # must be excluded
                        "side_effects": ["audio", "system"],
                        "preconditions": [],
                        "voice_triggerable": False,
                        "param_schema": {},
                    },
                ]

        snap = get_capability_snapshot(FakeMatcher())
        assert "volume up" in snap["commands"]
        assert "going dark" not in snap["commands"]   # not composable
        assert "going dark" in snap["all_action_ids"]
        assert "volume up" in snap["all_action_ids"]

    def test_version_is_hex_string(self):
        snap = get_capability_snapshot(None)
        assert isinstance(snap["version"], str)
        assert len(snap["version"]) == 16
        int(snap["version"], 16)  # raises ValueError if not hex

    def test_version_changes_when_composable_set_changes(self):
        class MatcherA:
            def list_commands(self):
                return [{"phrase": "volume up", "ai_composable": True,
                         "risk_class": "safe", "side_effects": ["audio"],
                         "aliases": [], "pack": "media", "description": "",
                         "ai_visible": True, "preconditions": [],
                         "voice_triggerable": True, "param_schema": {}}]

        class MatcherB:
            def list_commands(self):
                return [{"phrase": "volume up", "ai_composable": True,
                         "risk_class": "destructive",  # changed
                         "side_effects": ["audio"],
                         "aliases": [], "pack": "media", "description": "",
                         "ai_visible": True, "preconditions": [],
                         "voice_triggerable": True, "param_schema": {}}]

        snap_a = get_capability_snapshot(MatcherA())
        snap_b = get_capability_snapshot(MatcherB())
        assert snap_a["version"] != snap_b["version"]

    def test_settings_included(self):
        snap = get_capability_snapshot(None)
        assert "ollama.timeout_seconds" in snap["settings"]

    def test_custom_settings_override(self):
        custom = {"my.key": {"type": "int", "min": 0, "max": 9}}
        snap = get_capability_snapshot(None, settings_constraints=custom)
        assert snap["settings"] == custom


# ---------------------------------------------------------------------------
# Tests: validate_proposal -- valid cases
# ---------------------------------------------------------------------------

class TestValidateProposalAccepts:
    def test_single_composable_step_no_params(self):
        proposal = {"steps": [{"action_id": "volume up", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True
        assert result["errors"] == []

    def test_multiple_composable_steps(self):
        proposal = {"steps": [
            {"action_id": "volume down", "params": {}},
            {"action_id": "volume up",   "params": {}},
        ]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True

    def test_step_with_valid_param_in_range(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {"level": 50}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True

    def test_step_with_param_at_boundary_min(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {"level": 0}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True

    def test_step_with_param_at_boundary_max(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {"level": 100}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True

    def test_extra_unrecognised_params_are_ignored(self):
        # Unknown params should not cause failure; the schema only validates keys it knows
        proposal = {"steps": [{"action_id": "volume up",
                                "params": {"unknown_extra": "ignored"}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is True


# ---------------------------------------------------------------------------
# Tests: validate_proposal -- nonexistent action_id
# ---------------------------------------------------------------------------

class TestValidateProposalRejectsNonexistent:
    def test_completely_unknown_action_id(self):
        proposal = {"steps": [{"action_id": "does_not_exist", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("does not exist" in e for e in result["errors"])

    def test_error_message_names_the_bad_action(self):
        proposal = {"steps": [{"action_id": "phantom_command", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert any("phantom_command" in e for e in result["errors"])

    def test_partial_failure_other_steps_still_checked(self):
        # First step bad; second step good -- both should be reported
        proposal = {"steps": [
            {"action_id": "nonexistent", "params": {}},
            {"action_id": "volume up",   "params": {}},
        ]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        # Only step 0 should have an error
        assert len(result["errors"]) == 1
        assert "step 0" in result["errors"][0]


# ---------------------------------------------------------------------------
# Tests: validate_proposal -- non-composable action_id
# ---------------------------------------------------------------------------

class TestValidateProposalRejectsNonComposable:
    def test_registry_command_that_is_not_ai_composable(self):
        # "going dark" is in all_action_ids but ai_composable=False
        proposal = {"steps": [{"action_id": "going dark", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False

    def test_error_distinguishes_noncomposable_from_nonexistent(self):
        proposal_nc = {"steps": [{"action_id": "going dark", "params": {}}]}
        proposal_ne = {"steps": [{"action_id": "made_up_id", "params": {}}]}

        err_nc = validate_proposal(proposal_nc, SNAPSHOT)["errors"][0]
        err_ne = validate_proposal(proposal_ne, SNAPSHOT)["errors"][0]

        # Non-composable error should say "not AI-composable" or similar
        assert "composable" in err_nc.lower()
        # Nonexistent error should say "does not exist" or similar
        assert "not exist" in err_ne.lower() or "does not exist" in err_ne.lower()

    def test_focus_mode_not_composable(self):
        proposal = {"steps": [{"action_id": "focus mode", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("composable" in e.lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# Tests: validate_proposal -- out-of-range param
# ---------------------------------------------------------------------------

class TestValidateProposalRejectsOutOfRange:
    def test_param_above_max(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {"level": 150}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("max" in e for e in result["errors"])

    def test_param_below_min(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {"level": -10}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("min" in e for e in result["errors"])

    def test_param_non_numeric_rejected(self):
        proposal = {"steps": [{"action_id": "set brightness",
                                "params": {"level": "bright"}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("numeric" in e for e in result["errors"])

    def test_missing_required_param(self):
        proposal = {"steps": [{"action_id": "set brightness", "params": {}}]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert any("level" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Tests: validate_proposal -- structural validation
# ---------------------------------------------------------------------------

class TestValidateProposalStructure:
    def test_rejects_non_dict_proposal(self):
        result = validate_proposal(["steps"], SNAPSHOT)
        assert result["valid"] is False

    def test_rejects_missing_steps_key(self):
        result = validate_proposal({"actions": []}, SNAPSHOT)
        assert result["valid"] is False
        assert any("steps" in e for e in result["errors"])

    def test_rejects_empty_steps(self):
        result = validate_proposal({"steps": []}, SNAPSHOT)
        assert result["valid"] is False

    def test_rejects_non_list_steps(self):
        result = validate_proposal({"steps": "volume up"}, SNAPSHOT)
        assert result["valid"] is False

    def test_rejects_non_dict_step(self):
        result = validate_proposal({"steps": ["volume up"]}, SNAPSHOT)
        assert result["valid"] is False

    def test_rejects_step_missing_action_id(self):
        result = validate_proposal({"steps": [{"params": {}}]}, SNAPSHOT)
        assert result["valid"] is False
        assert any("action_id" in e for e in result["errors"])

    def test_rejects_step_missing_params(self):
        result = validate_proposal({"steps": [{"action_id": "volume up"}]}, SNAPSHOT)
        assert result["valid"] is False
        assert any("params" in e for e in result["errors"])

    def test_rejects_non_dict_params(self):
        result = validate_proposal({"steps": [{"action_id": "volume up",
                                               "params": "loud"}]}, SNAPSHOT)
        assert result["valid"] is False

    def test_collects_all_errors_not_just_first(self):
        proposal = {"steps": [
            {"action_id": "nonexistent_a", "params": {}},
            {"action_id": "nonexistent_b", "params": {}},
        ]}
        result = validate_proposal(proposal, SNAPSHOT)
        assert result["valid"] is False
        assert len(result["errors"]) == 2


# ---------------------------------------------------------------------------
# Tests: decorator backward-compat (import check only -- no live plugins loaded)
# ---------------------------------------------------------------------------

class TestDecoratorBackwardCompat:
    def test_command_decorator_accepts_new_kwargs(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test probe alpha", pack="test",
                 risk_class="safe", ai_composable=True,
                 side_effects=["audio"], preconditions=["no_unsaved_changes"],
                 voice_triggerable=True)
        def _test_handler(app, remainder):
            return True

        entry = _REGISTRY.get("test probe alpha")
        assert entry is not None
        assert entry["risk_class"] == "safe"
        assert entry["ai_composable"] is True
        assert entry["side_effects"] == ["audio"]
        assert entry["preconditions"] == ["no_unsaved_changes"]
        assert entry["voice_triggerable"] is True
        assert entry["param_schema"] == {}

        # clean up
        _REGISTRY.pop("test probe alpha", None)

    def test_command_decorator_defaults_remain_safe(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test probe beta", pack="test")
        def _test_handler2(app, remainder):
            return True

        entry = _REGISTRY.get("test probe beta")
        assert entry is not None
        assert entry["risk_class"] == "safe"
        assert entry["ai_composable"] is False    # opt-in, must be False by default
        assert entry["voice_triggerable"] is True
        assert entry["side_effects"] == []
        assert entry["preconditions"] == []
        assert entry["param_schema"] == {}
        assert entry["ai_visible"] is True

        _REGISTRY.pop("test probe beta", None)

    def test_destructive_command_can_set_voice_triggerable_false(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test probe gamma", pack="test",
                 risk_class="destructive", ai_composable=False,
                 voice_triggerable=False, side_effects=["system"])
        def _test_handler3(app, remainder):
            return True

        entry = _REGISTRY.get("test probe gamma")
        assert entry is not None
        assert entry["risk_class"] == "destructive"
        assert entry["voice_triggerable"] is False
        assert entry["ai_composable"] is False

        _REGISTRY.pop("test probe gamma", None)


# ---------------------------------------------------------------------------
# Tests: ai_visible=False filtering in snapshot
# ---------------------------------------------------------------------------

class TestAiVisibleSnapshotFiltering:
    def _make_matcher_with_hidden(self):
        class _M:
            def list_commands(self):
                return [
                    {
                        "phrase": "volume up",
                        "ai_visible": True,
                        "ai_composable": True,
                        "risk_class": "safe",
                        "side_effects": ["audio"],
                        "aliases": [], "pack": "media",
                        "description": "Increase volume.",
                        "preconditions": [], "voice_triggerable": True,
                        "param_schema": {}, "reversible": False, "preview_template": "",
                    },
                    {
                        "phrase": "hidden internal",
                        "ai_visible": False,
                        "ai_composable": False,
                        "risk_class": "safe",
                        "side_effects": [],
                        "aliases": [], "pack": "core",
                        "description": "Internal only.",
                        "preconditions": [], "voice_triggerable": True,
                        "param_schema": {}, "reversible": False, "preview_template": "",
                    },
                    {
                        "phrase": "hidden composable",
                        "ai_visible": False,
                        "ai_composable": True,
                        "risk_class": "safe",
                        "side_effects": [],
                        "aliases": [], "pack": "core",
                        "description": "Composable but hidden.",
                        "preconditions": [], "voice_triggerable": True,
                        "param_schema": {}, "reversible": False, "preview_template": "",
                    },
                ]
        return _M()

    def test_hidden_commands_not_in_all_action_ids(self):
        snap = get_capability_snapshot(self._make_matcher_with_hidden())
        assert "hidden internal" not in snap["all_action_ids"]
        assert "hidden composable" not in snap["all_action_ids"]

    def test_hidden_commands_not_in_composable_set(self):
        snap = get_capability_snapshot(self._make_matcher_with_hidden())
        assert "hidden internal" not in snap["commands"]
        assert "hidden composable" not in snap["commands"]

    def test_visible_commands_still_present(self):
        snap = get_capability_snapshot(self._make_matcher_with_hidden())
        assert "volume up" in snap["all_action_ids"]
        assert "volume up" in snap["commands"]

    def test_validator_treats_hidden_as_nonexistent(self):
        snap = get_capability_snapshot(self._make_matcher_with_hidden())
        proposal = {"steps": [{"action_id": "hidden composable", "params": {}}]}
        result = validate_proposal(proposal, snap)
        assert result["valid"] is False
        assert any("does not exist" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Tests: reversible and preview_template new fields
# ---------------------------------------------------------------------------

class TestNewMetadataFields:
    def test_reversible_defaults_false_in_registry(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test meta reversible default", pack="test")
        def _h(app, r):
            return True

        entry = _REGISTRY.get("test meta reversible default")
        assert entry is not None
        assert entry["reversible"] is False
        _REGISTRY.pop("test meta reversible default", None)

    def test_preview_template_defaults_empty_in_registry(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test meta preview default", pack="test")
        def _h(app, r):
            return True

        entry = _REGISTRY.get("test meta preview default")
        assert entry is not None
        assert entry["preview_template"] == ""
        _REGISTRY.pop("test meta preview default", None)

    def test_reversible_and_preview_template_can_be_set(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test meta fields set", pack="test",
                 reversible=True, preview_template="Undo the last action")
        def _h(app, r):
            return True

        entry = _REGISTRY.get("test meta fields set")
        assert entry is not None
        assert entry["reversible"] is True
        assert entry["preview_template"] == "Undo the last action"
        _REGISTRY.pop("test meta fields set", None)

    def test_reversible_and_preview_template_in_list_commands(self):
        from samsara.plugin_commands import command, _REGISTRY, list_commands

        @command("test meta lc fields", pack="test",
                 reversible=True, preview_template="Do the thing")
        def _h(app, r):
            return True

        cmds = list_commands()
        found = next((c for c in cmds if c["phrase"] == "test meta lc fields"), None)
        assert found is not None
        assert found["reversible"] is True
        assert found["preview_template"] == "Do the thing"
        _REGISTRY.pop("test meta lc fields", None)

    def test_side_effect_category_alias_in_list_commands(self):
        from samsara.plugin_commands import command, _REGISTRY, list_commands

        @command("test meta sec alias", pack="test", side_effects=["audio", "ui"])
        def _h(app, r):
            return True

        cmds = list_commands()
        found = next((c for c in cmds if c["phrase"] == "test meta sec alias"), None)
        assert found is not None
        assert found["side_effect_category"] == ["audio", "ui"]
        assert found["side_effects"] == found["side_effect_category"]
        _REGISTRY.pop("test meta sec alias", None)

    def test_side_effect_category_param_accepted(self):
        from samsara.plugin_commands import command, _REGISTRY

        @command("test meta sec param", pack="test", side_effect_category=["file"])
        def _h(app, r):
            return True

        entry = _REGISTRY.get("test meta sec param")
        assert entry is not None
        assert entry["side_effects"] == ["file"]
        _REGISTRY.pop("test meta sec param", None)

    def test_new_fields_in_snapshot(self):
        class _M:
            def list_commands(self):
                return [{
                    "phrase": "undo last",
                    "ai_visible": True,
                    "ai_composable": True,
                    "risk_class": "reversible",
                    "side_effects": ["ui"],
                    "aliases": [], "pack": "core",
                    "description": "Undo.",
                    "preconditions": [],
                    "voice_triggerable": True,
                    "param_schema": {},
                    "reversible": True,
                    "preview_template": "Undo the last typed text",
                }]

        snap = get_capability_snapshot(_M())
        cmd = snap["commands"].get("undo last")
        assert cmd is not None
        assert cmd["reversible"] is True
        assert cmd["preview_template"] == "Undo the last typed text"


# ---------------------------------------------------------------------------
# Tests: expanded config schema
# ---------------------------------------------------------------------------

class TestExpandedConfigSchema:
    def test_model_size_present_and_enum(self):
        s = get_settings_constraints()
        assert "model_size" in s
        assert s["model_size"]["type"] == "enum"
        assert "base" in s["model_size"]["options"]

    def test_tts_speed_present(self):
        s = get_settings_constraints()
        assert "tts.speed" in s
        assert s["tts.speed"]["min"] < s["tts.speed"]["max"]

    def test_command_mode_debounce_present(self):
        s = get_settings_constraints()
        assert "command_mode.enter_debounce_ms" in s
        e = s["command_mode.enter_debounce_ms"]
        assert e["type"] == "int"
        assert e["min"] == 0
        assert e["max"] == 2000

    def test_cloud_llm_provider_options(self):
        s = get_settings_constraints()
        assert "cloud_llm.provider" in s
        assert "anthropic" in s["cloud_llm.provider"]["options"]
        assert "deepseek" in s["cloud_llm.provider"]["options"]

    def test_depends_on_present_for_conditional_settings(self):
        s = get_settings_constraints()
        assert "echo_cancellation.latency_ms" in s
        assert "depends_on" in s["echo_cancellation.latency_ms"]

    def test_listening_indicator_position_enum(self):
        s = get_settings_constraints()
        assert "listening_indicator_position" in s
        pos = s["listening_indicator_position"]
        assert pos["type"] == "enum"
        assert "bottom-center" in pos["options"]

    def test_ava_memory_max_turns_bounds(self):
        s = get_settings_constraints()
        assert "ava_memory.max_turns" in s
        e = s["ava_memory.max_turns"]
        assert e["min"] == 5
        assert e["max"] == 500

    def test_total_key_count_exceeds_phase2a(self):
        s = get_settings_constraints()
        assert len(s) > 13
