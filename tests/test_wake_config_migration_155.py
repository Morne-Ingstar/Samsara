"""Queue 155 wake listener config migration regression."""

from types import SimpleNamespace
from unittest.mock import Mock


def test_nested_wake_enabled_is_removed_and_top_level_wins():
    import dictation

    app = SimpleNamespace(
        config={"wake_word_enabled": False, "wake_word_config": {"enabled": True},
                "wake_profiles": []},
        save_config=Mock(),
        _wake_word_config_already_migrated=lambda: True,
    )

    dictation.DictationApp._migrate_wake_word_config(app, {})

    assert app.config["wake_word_enabled"] is False
    assert "enabled" not in app.config["wake_word_config"]
    app.save_config.assert_called_once_with()
