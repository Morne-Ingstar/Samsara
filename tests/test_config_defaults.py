"""Gate test for tools/config_defaults_check.py.

Runs the static config-defaults checker in-process (no subprocess, no
module-level `import dictation`) and fails the suite if it finds a config
key read with two different literal defaults, or with a literal default
that disagrees with samsara.config_defaults.DEFAULTS. See
docs/reviews/build_and_config_audit.md Part B3c for the conflicts this
was built to catch.
"""
from tools import config_defaults_check


def test_no_conflicting_config_defaults():
    violations = config_defaults_check.find_violations()
    assert violations == [], "\n" + "\n".join(violations)


def test_previously_conflicting_keys_are_now_in_the_table():
    from samsara.config_defaults import DEFAULTS

    for key in (
        "wake_word_config.phrase",
        "wake_word_config.wake_abort_phrase",
        "wake_word_enabled",
        "command_mode.inactivity_timeout_s",
        "model_size",
        "compute_type",
        "device",
        "language",
        "mode",
        "performance_mode",
        "hotkey",
        "hyperion_host",
    ):
        assert key in DEFAULTS, f"{key} dropped out of config_defaults.DEFAULTS"
