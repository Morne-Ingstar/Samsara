"""
AI capability snapshot and proposal validator.

Single source of truth for what the AI Config Assistant is allowed to compose.
All three consumers read from this module:
  - AI context building (what actions are available to describe)
  - Proposal validation (whitelist check before user confirmation)
  - Runtime execution gate (future phase: re-check at fire time)

Design: "AI proposes, app disposes." This module is the disposal side.
No AI calls here -- pure validation logic only.
"""

import hashlib
import json

from samsara.config_schema import SETTINGS_SCHEMA


def get_settings_constraints():
    """Return a copy of the machine-readable settings constraints dict."""
    return dict(SETTINGS_SCHEMA)


# ---------------------------------------------------------------------------
# Capability snapshot
# ---------------------------------------------------------------------------

def get_capability_snapshot(matcher, settings_constraints=None):
    """Build a versioned snapshot of all AI-composable commands.

    Commands with ai_visible=False are excluded from both the composable set
    and all_action_ids -- the AI should not know they exist.

    Args:
        matcher: a frozen CommandMatcher instance (or None for an empty snapshot).
        settings_constraints: override for settings constraints dict.
            If None, uses get_settings_constraints().

    Returns a dict:
        {
            "version": "<16-char sha256 prefix>",
            "commands": {
                "<action_id>": {
                    "description": str,
                    "pack": str,
                    "risk_class": str,
                    "ai_composable": True,
                    "side_effects": list[str],
                    "preconditions": list[str],
                    "voice_triggerable": bool,
                    "aliases": list[str],
                    "param_schema": dict,
                    "reversible": bool,
                    "preview_template": str,
                }
            },
            "all_action_ids": list[str],   # ai_visible commands, composable or not
            "settings": dict,              # settings_constraints
        }

    The "version" hash changes whenever the composable command set changes
    (command added/removed, risk_class or side_effects altered). Use it to
    detect stale snapshots between validate and apply.
    """
    if settings_constraints is None:
        settings_constraints = get_settings_constraints()

    all_cmds = matcher.list_commands() if matcher is not None else []

    composable = {}
    all_action_ids = []

    for cmd in all_cmds:
        if not cmd.get("ai_visible", True):
            continue
        all_action_ids.append(cmd["phrase"])
        if not cmd.get("ai_composable", False):
            continue
        composable[cmd["phrase"]] = {
            "description":       cmd.get("description", ""),
            "pack":              cmd.get("pack", "core"),
            "risk_class":        cmd.get("risk_class", "safe"),
            "ai_composable":     True,
            "side_effects":      cmd.get("side_effects", []),
            "preconditions":     cmd.get("preconditions", []),
            "voice_triggerable": cmd.get("voice_triggerable", True),
            "aliases":           cmd.get("aliases", []),
            "param_schema":      cmd.get("param_schema", {}),
            "reversible":        cmd.get("reversible", False),
            "preview_template":  cmd.get("preview_template", ""),
        }

    # Version: sha256 of the composable set's risk fingerprint.
    # Truncated to 16 chars for readability; collision risk is negligible
    # for this use case (detecting staleness, not security).
    fingerprint_data = json.dumps(
        {
            k: {
                "risk_class":  v["risk_class"],
                "side_effects": sorted(v["side_effects"]),
            }
            for k, v in sorted(composable.items())
        },
        sort_keys=True,
    ).encode()
    version = hashlib.sha256(fingerprint_data).hexdigest()[:16]

    return {
        "version":        version,
        "commands":       composable,
        "all_action_ids": sorted(all_action_ids),
        "settings":       settings_constraints,
    }


# ---------------------------------------------------------------------------
# Proposal validator
# ---------------------------------------------------------------------------

def validate_proposal(proposal_json, snapshot):
    """Validate a structured macro proposal against the capability snapshot.

    Args:
        proposal_json: dict with 'steps' list, each step:
            {"action_id": str, "params": dict}
        snapshot: result of get_capability_snapshot()

    Returns:
        {"valid": bool, "errors": list[str]}

    Checks performed (in order):
    1. proposal_json is a dict with a non-empty 'steps' list
    2. each step is a dict with 'action_id' and 'params' keys
    3. action_id exists in snapshot['all_action_ids'] (unknown vs non-composable)
    4. action_id is in snapshot['commands'] (i.e. ai_composable=True)
    5. params is a dict
    6. if the command has a param_schema, each param value satisfies its constraints

    All errors are collected; the function does not short-circuit after the first.
    """
    errors = []
    composable = snapshot.get("commands", {})
    all_ids = set(snapshot.get("all_action_ids", []))

    if not isinstance(proposal_json, dict):
        return {"valid": False, "errors": ["proposal must be a JSON object"]}

    steps = proposal_json.get("steps")
    if steps is None:
        return {"valid": False, "errors": ["missing 'steps' key"]}
    if not isinstance(steps, list):
        return {"valid": False, "errors": ["'steps' must be an array"]}
    if len(steps) == 0:
        return {"valid": False, "errors": ["'steps' must not be empty"]}

    for i, step in enumerate(steps):
        prefix = f"step {i}"

        if not isinstance(step, dict):
            errors.append(f"{prefix}: must be a JSON object, got {type(step).__name__}")
            continue

        action_id = step.get("action_id")
        params = step.get("params")

        # -- action_id checks --
        if action_id is None:
            errors.append(f"{prefix}: missing 'action_id'")
            continue

        if action_id not in composable:
            if action_id in all_ids:
                errors.append(
                    f"{prefix}: action_id '{action_id}' exists in registry "
                    f"but is not AI-composable (ai_composable=False)"
                )
            else:
                errors.append(
                    f"{prefix}: action_id '{action_id}' does not exist in registry"
                )
            # Can't validate params without a known command spec; skip rest for this step
            continue

        cmd_spec = composable[action_id]

        # -- params checks --
        if params is None:
            errors.append(f"{prefix}: missing 'params'")
            continue

        if not isinstance(params, dict):
            errors.append(f"{prefix}: 'params' must be a JSON object")
            continue

        param_schema = cmd_spec.get("param_schema") or {}
        for param_name, param_spec in param_schema.items():
            required = param_spec.get("required", False)

            if param_name not in params:
                if required:
                    errors.append(f"{prefix}: missing required param '{param_name}'")
                continue

            val = params[param_name]
            p_type = param_spec.get("type")

            if p_type in ("int", "float"):
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    errors.append(
                        f"{prefix}: param '{param_name}' must be numeric, got {val!r}"
                    )
                    continue
                p_min = param_spec.get("min")
                p_max = param_spec.get("max")
                if p_min is not None and num < p_min:
                    errors.append(
                        f"{prefix}: param '{param_name}' value {val} < min {p_min}"
                    )
                if p_max is not None and num > p_max:
                    errors.append(
                        f"{prefix}: param '{param_name}' value {val} > max {p_max}"
                    )

            elif p_type == "enum":
                allowed = param_spec.get("options", [])
                if val not in allowed:
                    errors.append(
                        f"{prefix}: param '{param_name}' value {val!r} "
                        f"not in allowed options {allowed}"
                    )

    return {"valid": len(errors) == 0, "errors": errors}
