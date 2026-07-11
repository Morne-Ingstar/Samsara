"""Tests for samsara.wake_profiles: multi-wakeword profile config validation
and legacy mode/send_word normalization (tribunal spec, docs design review
arc_20260629_170545.md).

wake_profiles.py is pure config validation -- no audio/OWW/Whisper touched --
so no mocking is required to import or exercise it. Structured like
tests/test_session_modes.py.
"""
import pytest

from samsara.wake_profiles import (
    MIN_SYLLABLES,
    count_syllables,
    validate_wake_profiles,
    normalize_profile_mode_and_send_word,
)


# ---------------------------------------------------------------------------
# count_syllables
# ---------------------------------------------------------------------------

class TestCountSyllables:
    def test_two_syllable_phrase(self):
        assert count_syllables("hey claude") == 2

    def test_three_syllable_phrase(self):
        assert count_syllables("hello claude") == 3

    def test_single_word_minimum_one_syllable(self):
        assert count_syllables("go") >= 1


# ---------------------------------------------------------------------------
# validate_wake_profiles
# ---------------------------------------------------------------------------

class TestValidateWakeProfiles:
    def test_below_syllable_floor_disables_profile(self):
        assert count_syllables("hey claude") < MIN_SYLLABLES
        profiles = [{"id": "short", "phrase": "hey claude", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is False

    def test_at_syllable_floor_stays_enabled(self):
        assert count_syllables("hello claude") == MIN_SYLLABLES
        profiles = [{"id": "ok", "phrase": "hello claude", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is True

    def test_duplicate_phrase_disables_the_later_profile(self):
        profiles = [
            {"id": "first", "phrase": "activate claude", "enabled": True},
            {"id": "second", "phrase": "activate claude", "enabled": True},
        ]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is True
        assert profiles[1]["enabled"] is False

    def test_duplicate_check_is_case_and_whitespace_insensitive(self):
        profiles = [
            {"id": "first", "phrase": "activate claude", "enabled": True},
            {"id": "second", "phrase": "  Activate Claude  ", "enabled": True},
        ]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is True
        assert profiles[1]["enabled"] is False

    def test_empty_phrase_disables_profile(self):
        profiles = [{"id": "empty", "phrase": "", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is False

    def test_whitespace_only_phrase_disables_profile(self):
        profiles = [{"id": "blank", "phrase": "   ", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is False

    def test_missing_oww_model_key_does_not_crash(self):
        # No 'oww_model' key at all -- validate_wake_profiles never touches
        # OWW/model loading, so a profile that hasn't been assigned a model
        # yet (Whisper-transcript fallback) must validate exactly like one
        # that has a model file configured.
        profiles = [{"id": "no_model", "phrase": "activate claude", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is True

    def test_already_disabled_profile_is_left_untouched(self):
        profiles = [{"id": "off", "phrase": "hey", "enabled": False}]
        validate_wake_profiles(profiles)
        assert profiles[0]["enabled"] is False

    def test_non_dict_profile_entry_does_not_crash(self):
        profiles = ["not a dict", {"id": "ok", "phrase": "activate claude", "enabled": True}]
        validate_wake_profiles(profiles)
        assert profiles[1]["enabled"] is True

    def test_returns_the_mutated_list(self):
        profiles = [{"id": "ok", "phrase": "activate claude", "enabled": True}]
        result = validate_wake_profiles(profiles)
        assert result is profiles


# ---------------------------------------------------------------------------
# normalize_profile_mode_and_send_word
# ---------------------------------------------------------------------------

class TestNormalizeProfileModeAndSendWord:
    def test_legacy_enter_maps_to_focus_dictate(self):
        profile = {"id": "claude", "send_policy": "enter"}
        normalize_profile_mode_and_send_word(profile)
        assert profile["mode"] == "focus_dictate"
        assert "send_policy" not in profile

    def test_legacy_stage_only_maps_to_stage_send(self):
        profile = {"id": "hermes", "send_policy": "stage_only"}
        normalize_profile_mode_and_send_word(profile)
        assert profile["mode"] == "stage_send"
        assert "send_policy" not in profile

    def test_unknown_legacy_send_policy_defaults_to_focus_dictate(self):
        profile = {"id": "weird", "send_policy": "something_else"}
        normalize_profile_mode_and_send_word(profile)
        assert profile["mode"] == "focus_dictate"

    def test_send_word_default_filled_when_absent(self):
        profile = {"id": "claude"}
        normalize_profile_mode_and_send_word(profile, default_send_word="over")
        assert profile["send_word"] == "over"

    def test_mode_defaults_to_focus_dictate_when_absent(self):
        profile = {"id": "claude"}
        normalize_profile_mode_and_send_word(profile)
        assert profile["mode"] == "focus_dictate"

    def test_explicit_mode_and_send_word_are_preserved(self):
        profile = {"id": "hermes", "mode": "stage_send", "send_word": "send"}
        normalize_profile_mode_and_send_word(profile, default_send_word="over")
        assert profile["mode"] == "stage_send"
        assert profile["send_word"] == "send"

    def test_explicit_send_word_not_overwritten_by_default(self):
        profile = {"id": "claude", "mode": "focus_dictate", "send_word": "finished"}
        normalize_profile_mode_and_send_word(profile, default_send_word="over")
        assert profile["send_word"] == "finished"

    def test_legacy_send_policy_does_not_override_explicit_mode(self):
        # setdefault only fills mode from the legacy mapping when 'mode' is
        # still absent -- an explicit 'mode' key already present wins.
        profile = {"id": "claude", "send_policy": "stage_only", "mode": "focus_dictate"}
        normalize_profile_mode_and_send_word(profile)
        assert profile["mode"] == "focus_dictate"
