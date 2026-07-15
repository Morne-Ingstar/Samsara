"""Safe, user-triggered support links and diagnostic summary helpers."""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping

from samsara import __version__


BUG_REPORT_URL = (
    "https://github.com/Morne-Ingstar/Samsara/issues/new"
    "?template=bug_report.yml"
)
BETA_FEEDBACK_URL = (
    "https://github.com/Morne-Ingstar/Samsara/issues/new"
    "?template=beta_feedback.yml"
)
SUPPORT_URL = "https://morneis.com/samsara/support/"


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
        ("Model", cfg.get("model_size", "default")),
        ("Language", cfg.get("language", "default")),
        ("Requested device", cfg.get("device", "auto")),
        ("Compute type", cfg.get("compute_type", "default")),
        ("Performance mode", cfg.get("performance_mode", "default")),
        ("Recording mode", cfg.get("mode", "default")),
        ("HANDS FREE enabled", bool(command_mode.get("enabled", False))),
        ("Wake listener enabled", bool(cfg.get("wake_word_enabled", False))),
        ("Interface scale", cfg.get("ui_scale", 1.0)),
    )
    lines = ["Samsara safe diagnostic summary"]
    lines.extend(f"{label}: {value}" for label, value in values)
    lines.append("Live log: not copied automatically (review and redact before sharing)")
    return "\n".join(lines)


__all__ = [
    "BETA_FEEDBACK_URL",
    "BUG_REPORT_URL",
    "SUPPORT_URL",
    "build_safe_diagnostic_summary",
]
