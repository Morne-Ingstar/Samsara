"""Safe, user-triggered support links and diagnostic summary helpers."""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping
from urllib.parse import quote

from samsara import __version__, config_defaults


BUG_REPORT_URL = (
    "https://github.com/Morne-Ingstar/Samsara/issues/new"
    "?template=bug_report.yml"
)
BETA_FEEDBACK_URL = (
    "https://github.com/Morne-Ingstar/Samsara/issues/new"
    "?template=beta_feedback.yml"
)
SUPPORT_URL = "https://morneis.com/samsara/support/"
DOCUMENTATION_URL = "https://morneis.com/samsara/docs/"
# This is intentionally a public beta-support address, not a hidden service
# endpoint.  It opens the user's own mail client only after an explicit click,
# and the Settings button copies the whole message first because a mailto:
# with no registered mail client silently goes nowhere.
BETA_SUPPORT_EMAIL = "morneingstarproductions@gmail.com"
BETA_SUPPORT_MAILTO = (
    "mailto:morneingstarproductions@gmail.com"
    "?subject=Samsara%20beta%20support"
)


def build_safe_diagnostic_summary(
    config: Mapping | None = None,
    *,
    frozen: bool | None = None,
    platform_text: str | None = None,
    python_version: str | None = None,
) -> str:
    """Return useful environment facts without logs, paths, or credentials.

    This deliberately uses an allowlist. API keys, supporter keys, webhooks,
    wake-profile targets, microphone names, dictated text, and filesystem paths
    can never enter the result through an unexpected config key.
    """

    cfg = config or {}
    command_mode = cfg.get("command_mode", {})
    if not isinstance(command_mode, Mapping):
        command_mode = {}

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if platform_text is None:
        platform_text = platform.platform()
    if python_version is None:
        python_version = platform.python_version()

    values = (
        ("Samsara", __version__),
        ("Execution", "packaged" if frozen else "source"),
        ("Windows", platform_text),
        ("Python", python_version),
        ("Model", cfg.get("model_size", config_defaults.DEFAULTS["model_size"])),
        ("Language", cfg.get("language", config_defaults.DEFAULTS["language"])),
        ("Requested device", cfg.get("device", config_defaults.DEFAULTS["device"])),
        ("Compute type", cfg.get("compute_type", config_defaults.DEFAULTS["compute_type"])),
        ("Performance mode", cfg.get("performance_mode", config_defaults.DEFAULTS["performance_mode"])),
        ("Recording mode", cfg.get("mode", config_defaults.DEFAULTS["mode"])),
        ("HANDS FREE enabled", bool(command_mode.get("enabled", False))),
        ("Wake listener enabled", bool(cfg.get("wake_word_enabled", False))),
        ("Interface scale", cfg.get("ui_scale", 1.0)),
    )
    lines = ["Samsara safe diagnostic summary"]
    lines.extend(f"{label}: {value}" for label, value in values)
    lines.append("Live log: not copied automatically (review and redact before sharing)")
    return "\n".join(lines)


BETA_SUPPORT_SUBJECT = "Samsara beta support"
SUPPORT_EMAIL_PROMPT = "What I said / What it did / What I expected:\n\n\n---\n"
#: Settings sidebar name of the support page (settings_qt._TAB_NAMES).
SUPPORT_TAB_NAME = "Help & Support"


def build_support_email_body(config: Mapping | None = None, **summary_kwargs) -> str:
    """The tester's prompt followed by the allow-listed diagnostic summary.

    Nothing but build_safe_diagnostic_summary() output is appended, so the
    body inherits its guarantee: no logs, paths, keys, or dictated text.
    """
    return SUPPORT_EMAIL_PROMPT + build_safe_diagnostic_summary(config, **summary_kwargs)


def build_support_email_text(config: Mapping | None = None, **summary_kwargs) -> str:
    """The full message (address, subject, body) for the clipboard, so it
    can be pasted into any mail app or webmail when mailto: goes nowhere."""
    return (
        f"To: {BETA_SUPPORT_EMAIL}\n"
        f"Subject: {BETA_SUPPORT_SUBJECT}\n\n"
        f"{build_support_email_body(config, **summary_kwargs)}"
    )


def build_support_mailto(config: Mapping | None = None, **summary_kwargs) -> str:
    """mailto: URL with the subject and URL-encoded body."""
    body = build_support_email_body(config, **summary_kwargs)
    return (
        f"mailto:{BETA_SUPPORT_EMAIL}"
        f"?subject={quote(BETA_SUPPORT_SUBJECT, safe='')}"
        f"&body={quote(body, safe='')}"
    )


def open_support_tab(app) -> None:
    """Open Settings on the Help & Support page. Call on the Qt thread.

    Reuses app.open_settings() (which posts window creation/show to the Qt
    loop) and posts the tab selection after it, so a first open selects the
    page once the window exists.
    """
    app.open_settings()

    def _select():
        window = getattr(getattr(app, "_settings_qt", None), "_window", None)
        if window is not None:
            window.show_tab(SUPPORT_TAB_NAME)

    from samsara.ui import qt_runtime

    qt_runtime.post(_select)


__all__ = [
    "BETA_FEEDBACK_URL",
    "BETA_SUPPORT_EMAIL",
    "BETA_SUPPORT_MAILTO",
    "BETA_SUPPORT_SUBJECT",
    "BUG_REPORT_URL",
    "DOCUMENTATION_URL",
    "SUPPORT_EMAIL_PROMPT",
    "SUPPORT_TAB_NAME",
    "SUPPORT_URL",
    "build_safe_diagnostic_summary",
    "build_support_email_body",
    "build_support_email_text",
    "build_support_mailto",
    "open_support_tab",
]
