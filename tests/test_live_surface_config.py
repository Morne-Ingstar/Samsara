"""Pure persisted-presence migration and validation tests for package A."""
from samsara.live_surface.config import (
    DEFAULT_IDLE_DELAY_S, LIVE_SURFACE_DEFAULTS, migrate_live_surface,
    validate_live_surface_config,
)


def live(config):
    return config["ui"]["live_surface"]


def test_missing_config_gets_defaults_without_legacy_backup():
    result = migrate_live_surface({})
    assert live(result)["idle_delay_s"] == DEFAULT_IDLE_DELAY_S
    assert live(result)["show_idle_mark"] is True
    assert live(result)["partials"] == "auto"
    assert live(result)["legacy"] == {}


def test_explicit_zero_and_false_are_seen_before_defaults_and_archived():
    result = migrate_live_surface({"command_mode": {"preview_idle_delay_s": 0, "preview_idle_opacity": 0,
                                                        "session_streaming_preview": False},
                                   "listening_indicator_enabled": False})
    block = live(result)
    assert block["idle_delay_s"] == DEFAULT_IDLE_DELAY_S
    assert block["show_idle_mark"] is False
    assert set(block["legacy"]) == {"idle_delay_s", "show_idle_mark"}


def test_corrupt_config_is_bounded_and_legacy_never_copies_streaming_mode():
    original = {"ui": {"live_surface": {"idle_delay_s": "bad", "partials": "more", "placement": {"monitors": 2}}},
                "streaming_mode": True, "streaming_direct_paste": False}
    result = migrate_live_surface(original)
    block = live(result)
    assert block["idle_delay_s"] == DEFAULT_IDLE_DELAY_S and block["partials"] == "auto"
    assert block["placement"] == LIVE_SURFACE_DEFAULTS["placement"]
    assert result["streaming_mode"] is True and result["streaming_direct_paste"] is False
    assert "streaming_mode" not in block["legacy"] and "streaming_direct_paste" not in block["legacy"]


def test_already_migrated_new_keys_win_and_migration_is_idempotent():
    original = {"ui": {"live_surface": {"idle_delay_s": 12.0, "show_idle_mark": True,
                                           "placement": {"preferred_monitor": "x", "monitors": {"x": {"edge": "left", "t": .2}}}}},
                "command_mode": {"preview_idle_delay_s": 2.0, "preview_idle_opacity": 0.0}}
    once = migrate_live_surface(original)
    twice = migrate_live_surface(once)
    assert live(once)["idle_delay_s"] == 12.0 and live(once)["show_idle_mark"] is True
    assert twice == once


def test_legacy_position_and_validation_keep_only_bounded_monitor_records():
    result = migrate_live_surface({"command_mode": {"preview_position": "custom|DISPLAY2|.25|.75"}})
    placement = live(result)["placement"]
    assert placement["preferred_monitor"] == "DISPLAY2"
    assert placement["monitors"]["DISPLAY2"]["edge"] == "free"
    validated = validate_live_surface_config({"idle_delay_s": 61, "placement": {"preferred_monitor": "x",
        "monitors": {"x": {"edge": "right", "t": 2, "free_cx": -1, "free_cy": .5}, "": {"edge": "top"}}}})
    assert validated["idle_delay_s"] == DEFAULT_IDLE_DELAY_S
    assert validated["placement"]["monitors"] == {"x": {"edge": "right", "t": 1.0, "free_cx": 0.0, "free_cy": .5}}
