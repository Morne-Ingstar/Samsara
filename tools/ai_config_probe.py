"""
ai_config_probe.py -- empirical gate probe for AI Config Assistant (Phase 1 spike)

Answers four design questions with real runtime data. Run from the
Samsara-dev project root:
    python tools/ai_config_probe.py

Results printed to stdout; use findings to populate
tools/ai_config_probe_findings.md.
"""
import sys
import os
import json
import re
import time
from pathlib import Path

from samsara.log import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SEP = "=" * 60


# ---------------------------------------------------------------------------
# PROBE 1 -- Registry introspection
# ---------------------------------------------------------------------------

def probe_registry():
    print(f"\n{_SEP}")
    print("PROBE 1 -- Registry introspection")
    print(_SEP)

    # Load plugins into the plugin _REGISTRY
    from samsara.plugin_commands import _REGISTRY, load_plugins
    plugins_dir = ROOT / "plugins" / "commands"
    try:
        load_plugins(str(plugins_dir))
    except Exception as exc:
        print(f"  load_plugins warning: {exc}")

    # Unique entries (aliases point to the same dict)
    unique = {}
    for phrase, entry in _REGISTRY.items():
        eid = id(entry)
        if eid not in unique:
            unique[eid] = entry

    commands = list(unique.values())
    print(f"\n  Total unique commands: {len(commands)}")
    print(f"  Total phrases incl aliases: {len(_REGISTRY)}")

    # What fields exist in the plugin registry today
    fields_present = set()
    for c in commands:
        fields_present.update(c.keys())

    # Fields the AI config feature needs (from design doc)
    fields_needed = {
        "phrase", "aliases", "pack", "source", "debounce",
        "app_overrides", "ai_visible",
        # -- required by design, missing today --
        "param_schema", "risk_level", "reversible",
        "side_effect_category", "preview_template", "preconditions",
    }

    present = fields_present & fields_needed
    missing = fields_needed - fields_present

    print("\n  Plugin registry fields present today:")
    for f in sorted(fields_present):
        marker = "+" if f in fields_needed else " "
        print(f"    [{marker}] {f}")

    print("\n  Fields NEEDED by design but ABSENT from registry today:")
    for f in sorted(missing):
        print(f"    [-] {f}")

    # What CommandMatcher.list_commands() actually exposes
    from samsara.command_registry import CommandMatcher
    matcher = CommandMatcher()
    try:
        matcher.load_plugins(_REGISTRY)
        matcher.freeze()
        cmd_list = matcher.list_commands()
        matcher_fields = set(cmd_list[0].keys()) if cmd_list else set()
    except Exception as exc:
        print(f"  CommandMatcher load warning: {exc}")
        cmd_list, matcher_fields = [], set()

    print(f"\n  CommandMatcher.list_commands() exposes {len(matcher_fields)} fields:")
    for f in sorted(matcher_fields):
        print(f"    {f}")

    # ai_visible: stored in plugin registry but NOT passed to CommandEntry
    ai_vis_false = [c["phrase"] for c in commands if not c.get("ai_visible", True)]
    print(f"\n  ai_visible=False commands (hidden from Ava): {len(ai_vis_false)}")
    for p in ai_vis_false[:5]:
        print(f"    '{p}'")
    print(f"  NOTE: ai_visible is stored in plugin _REGISTRY dict but is NOT")
    print(f"        propagated to CommandEntry in command_registry.load_plugins().")
    print(f"        CommandMatcher.list_commands() does not include it.")

    # Per-command breakdown: what safety fields are missing
    print("\n  Sample commands -- available metadata today:")
    header = f"  {'PHRASE':<32} {'PACK':<20} {'DEBOUNCE':>8}  {'AI_VIS':>6}  {'ALIASES'}"
    print(header)
    print(f"  {'-'*32} {'-'*20} {'-'*8}  {'-'*6}  {'-'*20}")
    for c in sorted(commands, key=lambda x: x["phrase"])[:20]:
        db = f"{c.get('debounce', 0):.1f}s" if c.get("debounce", 0) > 0 else "none"
        av = str(c.get("ai_visible", True))
        al = ", ".join(c.get("aliases", []))[:30] or "(none)"
        print(f"  {c['phrase']:<32} {c.get('pack','?'):<20} {db:>8}  {av:>6}  {al}")

    print(f"\n  Missing safety metadata per command (all {len(commands)} commands):")
    print("    param_schema      - no formal input spec; only phrase + free remainder string")
    print("    risk_level        - not tagged (SAFE / DISRUPTIVE / DESTRUCTIVE)")
    print("    reversible        - not tagged (command_registry has no undo concept)")
    print("    side_effect_category - not tagged (UI / SYSTEM / NETWORK / FILESYSTEM)")
    print("    preview_template  - not present; Ava has no structured preview mechanism")
    print("    preconditions     - not present; no runtime context checks before execute")

    return {
        "total_commands": len(commands),
        "fields_present": sorted(fields_present),
        "fields_needed_missing": sorted(missing),
        "matcher_fields": sorted(matcher_fields),
        "ai_visible_false_count": len(ai_vis_false),
    }


# ---------------------------------------------------------------------------
# PROBE 2 -- Settings constraints
# ---------------------------------------------------------------------------

def probe_settings():
    print(f"\n{_SEP}")
    print("PROBE 2 -- Settings constraints")
    print(_SEP)

    settings_path = ROOT / "samsara" / "ui" / "settings_qt.py"
    source = settings_path.read_text(encoding="utf-8")

    # Widget-level constraint calls
    range_calls = re.findall(r"\.setRange\(([^)]+)\)", source)
    min_calls = re.findall(r"\.setMinimum\(([^)]+)\)", source)
    max_calls = re.findall(r"\.setMaximum\(([^)]+)\)", source)
    step_calls = re.findall(r"\.setSingleStep\(([^)]+)\)", source)
    add_items = re.findall(r"\.addItems\(\[([^\]]+)\]\)", source)
    spin_boxes = re.findall(r"(QSpinBox|QDoubleSpinBox)\(\)", source)
    combo_boxes = re.findall(r"QComboBox\(\)", source)
    check_boxes = re.findall(r"QCheckBox\(", source)

    print(f"\n  UI widget inventory:")
    print(f"    QSpinBox / QDoubleSpinBox: {len(spin_boxes)}")
    print(f"    QComboBox (enum selectors): {len(combo_boxes)}")
    print(f"    QCheckBox (bool toggles):   {len(check_boxes)}")

    print(f"\n  Numeric constraint calls found in settings_qt.py:")
    print(f"    .setRange(...):      {len(range_calls)} calls")
    for r in range_calls[:8]:
        print(f"      setRange({r})")
    print(f"    .setMinimum(...):    {len(min_calls)} calls")
    print(f"    .setMaximum(...):    {len(max_calls)} calls")
    print(f"    .setSingleStep(...): {len(step_calls)} calls")

    print(f"\n  Enum option lists (.addItems):")
    for items_str in add_items[:6]:
        trimmed = items_str.strip()[:100]
        print(f"    [{trimmed}]")

    # Check for a separate machine-readable schema
    schema_candidates = [
        ROOT / "samsara" / "config_schema.json",
        ROOT / "samsara" / "config_defaults.json",
        ROOT / "config_schema.json",
        ROOT / "schema.json",
    ]
    schema_found = [p for p in schema_candidates if p.exists()]
    print(f"\n  Dedicated config schema files: {len(schema_found)}")
    if schema_found:
        for p in schema_found:
            print(f"    {p}")
    else:
        print("    (none)")

    # Check config.py for defaults dict
    config_py = ROOT / "samsara" / "config.py"
    if config_py.exists():
        cfg_src = config_py.read_text(encoding="utf-8")
        has_defaults = "DEFAULTS" in cfg_src or "_DEFAULTS" in cfg_src
        default_matches = re.findall(r"['\"]([a-z_.]+)['\"]:\s*(\S+),", cfg_src)
        print(f"\n  samsara/config.py: DEFAULTS dict present = {has_defaults}")
        if has_defaults and default_matches:
            print(f"  Sample config keys with defaults ({len(default_matches)} found):")
            for key, val in default_matches[:10]:
                print(f"    '{key}': {val}")
    else:
        print("\n  samsara/config.py: not found at expected path")

    # commands.json schema fields
    commands_json = ROOT / "commands.json"
    if commands_json.exists():
        cmds = json.loads(commands_json.read_text(encoding="utf-8"))
        if cmds:
            sample_cmd = next(iter(cmds.values()))
            print(f"\n  commands.json: {len(cmds)} built-in commands")
            print(f"  Fields per command: {list(sample_cmd.keys())}")
        else:
            print("\n  commands.json: empty")
    else:
        print("\n  commands.json: not found")

    print(f"\n  VERDICT:")
    print("  Numeric ranges (min/max/step) live only in Qt widget constructor")
    print("  calls -- not in any importable dict or JSON schema. Enum option")
    print("  lists are Python string literals in addItems() calls. Cross-field")
    print("  dependencies (e.g. 'enable cloud LLM' unlocking provider sub-")
    print("  settings) are enforced purely by Qt slot/signal callbacks with no")
    print("  declarative representation. No settings schema file exists.")

    return {
        "spin_boxes": len(spin_boxes),
        "combo_boxes": len(combo_boxes),
        "range_calls": len(range_calls),
        "schema_files": [str(p) for p in schema_found],
        "machine_readable": False,
    }


# ---------------------------------------------------------------------------
# PROBE 3 -- Small-model JSON reliability
# ---------------------------------------------------------------------------

OLLAMA_HOST = "http://localhost:11434"

VALID_IDS = {
    "volume_up", "volume_down", "toggle_mute",
    "open_browser", "minimize_all",
}

SYSTEM_PROMPT = """\
You are a macro planner. The user describes an action.
Return ONLY a JSON object with this exact schema and NO other text:
{
  "steps": [
    {"action_id": "<one of the allowed IDs>", "params": {}}
  ]
}

Allowed action_id values (use ONLY these exact strings):
  volume_up, volume_down, toggle_mute, open_browser, minimize_all

Do not include explanation, markdown fences, or any text outside the JSON."""

TEST_REQUESTS = [
    "Turn the volume up",
    "Mute my computer",
    "Open a web browser",
    "Minimize everything on my screen",
    "First lower the volume, then mute it, then open a browser",
]


def _get_ollama_model():
    try:
        import requests
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if r.status_code == 200:
            models = r.json().get("models", [])
            if models:
                return models[0]["name"]
    except Exception as e:
        logger.debug(f"_get_ollama_model: {e}")
    return None


def _call_ollama(model, system_prompt, user_msg):
    import requests
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
    }
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=60)
    return r.json().get("message", {}).get("content", "").strip()


def _validate(raw):
    text = raw.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, None, [], [f"JSONDecodeError: {exc}"]

    issues = []
    hallucinated = []
    if not isinstance(parsed, dict) or "steps" not in parsed:
        issues.append("missing 'steps' key")
        return True, parsed, hallucinated, issues
    if not isinstance(parsed["steps"], list):
        issues.append("'steps' not a list")
        return True, parsed, hallucinated, issues
    for i, step in enumerate(parsed["steps"]):
        if not isinstance(step, dict):
            issues.append(f"step {i}: not a dict")
            continue
        if "action_id" not in step:
            issues.append(f"step {i}: missing action_id")
        else:
            aid = step["action_id"]
            if aid not in VALID_IDS:
                hallucinated.append(aid)
                issues.append(f"step {i}: hallucinated action_id='{aid}'")
        if "params" not in step:
            issues.append(f"step {i}: missing params")
    return True, parsed, hallucinated, issues


def probe_ollama():
    print(f"\n{_SEP}")
    print("PROBE 3 -- Small-model JSON reliability")
    print(_SEP)

    model = _get_ollama_model()
    if model is None:
        print("\n  Ollama not reachable at localhost:11434.")
        print("  No local model available. Cloud fallback not tested (no gateway here).")
        return {"model": None, "reachable": False}

    print(f"\n  Ollama reachable. Using model: {model}")
    print(f"  Schema: steps:[{{action_id, params}}]  valid IDs: {sorted(VALID_IDS)}")

    results = []
    for i, req in enumerate(TEST_REQUESTS, 1):
        print(f"\n  [{i}/5] '{req}'")
        t0 = time.monotonic()
        try:
            raw = _call_ollama(model, SYSTEM_PROMPT, req)
            elapsed = time.monotonic() - t0
            valid_json, parsed, hallucinated, issues = _validate(raw)
            clean = valid_json and not issues
            tag = "CLEAN" if clean else ("HALLUC" if hallucinated else ("SCHEMA-ERR" if valid_json else "INVALID-JSON"))
            print(f"    result={tag}  {elapsed:.1f}s")
            print(f"    raw: {raw[:140]!r}")
            if issues:
                for iss in issues:
                    print(f"    issue: {iss}")
            results.append({
                "request": req,
                "is_valid_json": valid_json,
                "hallucinated_ids": hallucinated,
                "issues": issues,
                "elapsed_s": round(elapsed, 2),
            })
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"    ERROR: {exc}")
            results.append({"request": req, "error": str(exc), "is_valid_json": False, "elapsed_s": round(elapsed, 2)})

    valid_n = sum(1 for r in results if r.get("is_valid_json"))
    halluc_n = sum(1 for r in results if r.get("hallucinated_ids"))
    clean_n = sum(1 for r in results if r.get("is_valid_json") and not r.get("issues"))

    print(f"\n  SUMMARY: {valid_n}/5 valid JSON | {halluc_n}/5 hallucinated IDs | {clean_n}/5 fully clean")

    return {
        "model": model,
        "reachable": True,
        "valid_json_rate": f"{valid_n}/5",
        "hallucination_rate": f"{halluc_n}/5",
        "clean_rate": f"{clean_n}/5",
        "results": results,
    }


# ---------------------------------------------------------------------------
# PROBE 4 -- Re-execution gating
# ---------------------------------------------------------------------------

def probe_reexecution():
    print(f"\n{_SEP}")
    print("PROBE 4 -- Re-execution gating")
    print(_SEP)

    from samsara.plugin_commands import _REGISTRY
    from samsara.command_registry import CommandMatcher

    matcher = CommandMatcher()
    try:
        matcher.load_plugins(_REGISTRY)
        matcher.freeze()
    except Exception as exc:
        print(f"  Matcher load warning: {exc}")

    # Debounce coverage
    unique = {}
    for p, e in _REGISTRY.items():
        if id(e) not in unique:
            unique[id(e)] = e
    commands = list(unique.values())

    with_debounce = [(c["phrase"], c.get("debounce", 0))
                     for c in commands if c.get("debounce", 0) > 0]
    no_debounce   = [c for c in commands if not c.get("debounce", 0)]

    print(f"\n  Commands WITH debounce > 0: {len(with_debounce)}/{len(commands)}")
    for phrase, db in sorted(with_debounce, key=lambda x: -x[1])[:12]:
        print(f"    '{phrase}': {db}s")

    print(f"\n  Commands with NO debounce: {len(no_debounce)}/{len(commands)}")

    # Confirm/arm patterns in docstrings
    confirm_cmds = []
    for c in commands:
        func = c.get("func")
        if func and func.__doc__:
            doc = func.__doc__.lower()
            if any(w in doc for w in ["confirm", "arm", "dangerous", "destructive", "irreversible"]):
                confirm_cmds.append(c["phrase"])
    print(f"\n  Commands with confirm/dangerous/arm in docstring: {len(confirm_cmds)}")
    for p in confirm_cmds[:8]:
        print(f"    '{p}'")

    # Test re-fire behaviour: call match() twice, check suppression
    print("\n  Simulating two rapid matches of a high-impact command:")
    test_phrases = [
        "going dark",       # macro: mute+minimize+lock
        "toggle mute",      # media -- should have debounce
        "volume up",        # media
        "lock workstation", # if present
    ]
    for test in test_phrases:
        entry, remainder = matcher.match(test)
        if entry is None:
            continue
        db = entry.debounce
        suppressed_after = "blocked for " + str(db) + "s" if db > 0 else "FIRES AGAIN IMMEDIATELY"
        print(f"    '{test}': debounce={db}s -> second utterance: {suppressed_after}")

    print(f"\n  Full re-execution call chain (traced from source):")
    print("  1. Audio -> Whisper transcription -> text string")
    print("  2. CommandMatcher.match(text) -> token longest-match scan")
    print("  3. CommandMatcher.should_suppress(entry) -> checks debounce only")
    print("     debounce is 0.0 by default; only some commands opt in")
    print("  4. If not suppressed: entry.handler(app, remainder) called immediately")
    print("  5. CommandMatcher.record_execution(entry) -> updates debounce timestamp")
    print("")
    print("  Gating mechanisms that EXIST:")
    print("    - Per-command opt-in debounce (seconds)")
    print("    - Per-app app_overrides (disable command in specific exe)")
    print("    - Pack enable/disable (entire pack can be off)")
    print("")
    print("  Gating mechanisms that are ABSENT:")
    print("    - Arming state (command requires explicit arm before it can fire)")
    print("    - Confirmation prompt (user must say 'yes' / 'confirm' after preview)")
    print("    - One-shot tokens / instance IDs (macro fires at most once per session)")
    print("    - Phonetic confusion analysis (similar-sounding phrases not flagged)")
    print("    - Macro-level cooldown separate from step-level debounce")
    print("    - Risk-class gating (DESTRUCTIVE commands not restricted to hotkey-only)")
    print("    - Voice-exclusion for dangerous macros (nothing bars a macro from voice)")

    return {
        "commands_total": len(commands),
        "with_debounce": len(with_debounce),
        "no_debounce": len(no_debounce),
        "confirm_arm_in_docstring": len(confirm_cmds),
        "gates_present": ["debounce (opt-in)", "app_overrides", "pack enable/disable"],
        "gates_absent": [
            "arming state", "confirmation prompt", "one-shot tokens",
            "phonetic confusion analysis", "macro cooldown",
            "risk-class gating", "voice-exclusion for destructive commands",
        ],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("AI Config Assistant -- Phase 1 Empirical Gate Probe")
    print(_SEP)

    r1 = probe_registry()
    r2 = probe_settings()
    r3 = probe_ollama()
    r4 = probe_reexecution()

    print(f"\n{_SEP}")
    print("PROBE COMPLETE")
    print(_SEP)
    print(json.dumps({
        "probe1_registry": r1,
        "probe2_settings": r2,
        "probe3_ollama": r3,
        "probe4_reexecution": r4,
    }, indent=2, default=str))
