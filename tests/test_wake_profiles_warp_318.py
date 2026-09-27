"""Warp wake-profile default stays opt-in and passes phrase validation."""

from copy import deepcopy

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.wake_profiles import count_syllables, validate_wake_profiles


def test_warp_profile_is_in_schema_defaults_and_disabled():
    defaults = SETTINGS_SCHEMA["wake_profiles"]["default"]
    warp = next(profile for profile in defaults if profile["id"] == "warp")

    assert SETTINGS_SCHEMA["wake_profiles"]["type"] == "list"
    assert SETTINGS_SCHEMA["wake_profiles"]["item_type"] == "dict"
    assert warp == {
        "id": "warp",
        "phrase": "activate warp",
        "oww_model": "activate_warp.onnx",
        "target_process": "warp.exe",
        "enabled": False,
        "mode": "focus_dictate",
        "send_word": "over",
    }


def test_activate_warp_passes_wake_phrase_validation():
    defaults = deepcopy(SETTINGS_SCHEMA["wake_profiles"]["default"])
    warp = next(profile for profile in defaults if profile["id"] == "warp")
    warp["enabled"] = True

    assert count_syllables(warp["phrase"]) >= 3
    validate_wake_profiles(defaults)
    assert warp["enabled"] is True
