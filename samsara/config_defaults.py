"""
Single source of truth for config-key default values.

docs/reviews/build_and_config_audit.md Part B3c found 12 config keys read
with two or more different literal defaults across the codebase (e.g.
`wake_word_config.phrase` defaulting to 'jarvis' in one file and 'samsara'
in another). Any new literal default for one of those keys -- or for any
key added here in future -- must come from this table instead of being
retyped at the call site, so the same drift can't happen again.
tools/config_defaults_check.py statically enforces that: it fails the gate
if the same key is ever read with two different literal defaults, or with a
literal default that disagrees with DEFAULTS below.

Built on top of samsara.config_schema.SETTINGS_SCHEMA (the existing
settings-UI schema, which already carries a canonical "default" for most
keys) plus the handful of keys SETTINGS_SCHEMA doesn't cover.
"""

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.constants import DEFAULT_WAKE_PHRASE

# Keys with no settings-UI widget (so absent from SETTINGS_SCHEMA) that were
# nonetheless found with conflicting literal defaults in the audit. See
# docs/reviews/build_and_config_audit.md Part B3c and the "keys unified"
# section of this task's report for the reasoning behind each choice.
_EXTRA_DEFAULTS = {
    # Matches samsara.constants.DEFAULT_WAKE_PHRASE and the live config
    # value; the debug UI's 'samsara' / 'hey samsara' literals were the
    # outliers.
    "wake_word_config.phrase": DEFAULT_WAKE_PHRASE,
    # The 3-phrase list is what dictation.py's load_config() writes when
    # scaffolding a fresh config and what the main abort-word handling
    # already honours; the 1-phrase ['cancel'] list was the outlier.
    "wake_word_config.wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
    "hotkey": "ctrl+shift",
    # Absent from the live config and from every settings-UI widget -- a
    # plugin-only demo key (plugins/commands/demo_commands.py,
    # hyperion_lights.py). '' ("unconfigured") is the safe default; the two
    # hardcoded LAN addresses found during the audit ('192.168.50.247',
    # 'discoball.local') were leftover dev/demo values, not an intended
    # shipped default.
    "hyperion_host": "",
    # 2026-09-14: inside an open wake session (quick/long dictation, wake
    # session, post-wake command window) skip the adaptive RMS gate and use
    # only the near-silence floor (wake_consumer.IN_SESSION_NEAR_SILENCE_RMS).
    # False restores the pre-fix behaviour. The asleep gate is unaffected.
    "wake_word_config.audio.bypass_adaptive_gate_in_session": True,
}

DEFAULTS = {key: entry["default"] for key, entry in SETTINGS_SCHEMA.items() if "default" in entry}
DEFAULTS.update(_EXTRA_DEFAULTS)


def cfg_get(config, key):
    """Read a dotted-path config key, falling back to the single canonical
    default in DEFAULTS.

    Raises KeyError if `key` has no registered default -- register it in
    DEFAULTS (or SETTINGS_SCHEMA) instead of passing a fresh literal at the
    call site.
    """
    node = config
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            if key not in DEFAULTS:
                raise KeyError(f"no registered default for config key {key!r}")
            return DEFAULTS[key]
    return node
