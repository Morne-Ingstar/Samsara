"""Gate test for tools/config_defaults_check.py.

Runs the static config-defaults checker in-process (no subprocess, no
module-level `import dictation`) and fails the suite if it finds a config
key read with two different literal defaults, or with a literal default
that disagrees with samsara.config_defaults.DEFAULTS. The audit found
conflicts that this gate is designed to prevent.
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


def test_startup_options_default_off():
    from samsara.config_defaults import DEFAULTS

    assert DEFAULTS["launch_at_login"] is False
    assert DEFAULTS["start_hidden"] is False


def test_support_bundle_config_retains_startup_flags():
    from samsara.support_bundle import scrub_config

    flags = {"launch_at_login": True, "start_hidden": True}
    assert scrub_config(flags) == flags


def test_safe_diagnostics_include_startup_flags():
    from samsara.support_feedback import build_safe_diagnostic_summary

    summary = build_safe_diagnostic_summary(
        {"launch_at_login": True, "start_hidden": True},
        frozen=False,
        platform_text="Windows-test",
        python_version="3.11.test",
    )

    assert "Start at sign-in: True" in summary
    assert "Start hidden: True" in summary
