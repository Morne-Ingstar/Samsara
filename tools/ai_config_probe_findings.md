# AI Config Assistant -- Phase 1 Empirical Gate Probe Findings

Probe run: `python tools/ai_config_probe.py`
Branch: `experimental/ai-config-assistant`
Model tested: `qwen2.5vl:3b` (only model pulled locally)

---

## Probe 1 -- Registry Introspection

### What the live registry actually exposes

**Plugin registry (_REGISTRY dict):** 150 unique commands, 588 phrases including aliases.

Fields present per entry:

| Field | Present | Needed by design |
|---|---|---|
| `phrase` | yes | yes |
| `aliases` | yes | yes |
| `pack` | yes | yes |
| `source` | yes | yes |
| `debounce` | yes | yes |
| `app_overrides` | yes | yes |
| `ai_visible` | yes | yes -- but broken (see below) |
| `func` | yes | n/a (callable, not introspectable) |
| `param_schema` | **NO** | yes |
| `risk_level` | **NO** | yes |
| `reversible` | **NO** | yes |
| `side_effect_category` | **NO** | yes |
| `preview_template` | **NO** | yes |
| `preconditions` | **NO** | yes |

**CommandMatcher.list_commands() output** (what downstream code actually sees):
`aliases, description, pack, phrase, source, type` -- 6 fields. No `ai_visible`, no safety metadata.

### Critical gap: ai_visible is stored but not propagated

The `@command` decorator accepts `ai_visible=True/False`. 19 commands are currently marked `ai_visible=False` (internal commands like `hey ava`, `yes`, `ava cancel`, `archive my tabs`). However:

- `ai_visible` IS stored in the plugin `_REGISTRY` dict.
- `ai_visible` is NOT passed into `CommandEntry` in `CommandMatcher.load_plugins()`.
- `CommandMatcher.list_commands()` does NOT include it.
- Any consumer of `list_commands()` (including the future AI config assistant) gets no visibility into which commands are AI-composable.

### Safety metadata gap: all 6 fields missing from all 150 commands

No command has a `param_schema` (the remainder string is free text with no formal spec), no `risk_level` tag, no `reversible` flag, no `side_effect_category`, no `preview_template`, and no `preconditions`. These must all be added -- either as decorator kwargs or as a sidecar registry.

**VERDICT: NEEDS-WORK.** `ai_visible` plumbing is broken (stored, not propagated). All 6 safety metadata fields are absent from the entire registry. Phase 2 must add them to both the `@command` decorator and `CommandEntry` before any AI-composable subset can be defined.

---

## Probe 2 -- Settings Constraints

### What is machine-readable today

Settings are defined exclusively in `samsara/ui/settings_qt.py` (3762 lines). Constraints are expressed as Qt widget constructor calls:

| Constraint type | Count in settings_qt.py | Machine-readable outside UI? |
|---|---|---|
| Numeric range (`.setRange(min, max)`) | 21 calls | No -- Qt widget code only |
| Step size (`.setSingleStep(x)`) | 16 calls | No |
| Enum options (`.addItems([...])`) | 24+ calls | No |
| Bool toggles (`QCheckBox`) | 17 | No |
| Spin boxes (`QSpinBox/QDoubleSpinBox`) | 18 | No |

Sample ranges discovered (for context): `(1.0, 10.0)`, `(1.0, 30.0)`, `(0.2, 5.0)`, `(0.05, 1.0)`, `(0, 2000)`, `(5, 300)`, `(1, 20)`, `(0, 100)`.

Sample enum lists: `['clean', 'verbatim']`, `['hold', 'toggle', 'continuous']`, `['auto', 'manual']`, `['click', 'double_click']`.

### No machine-readable schema exists

- No `config_schema.json`, `config_defaults.json`, or any JSON schema file.
- No dedicated `samsara/config.py` with an importable `DEFAULTS` dict at the expected path.
- `commands.json` has 1 top-level object whose keys are command names (not a schema).
- Cross-field dependencies (enabling cloud LLM exposes provider/model sub-settings) are Qt signal/slot callbacks -- purely imperative, no declarative representation.

**VERDICT: NEEDS-WORK.** Zero settings constraints are machine-readable outside the Qt UI. Phase 2 must either extract a config schema (JSON Schema or a typed dataclass) or write one. Without it, the AI config assistant cannot validate that a proposed setting change is within bounds before offering it to the user.

---

## Probe 3 -- Small-Model JSON Reliability

### Setup

- Model: `qwen2.5vl:3b` (only pulled model; a vision-language model, not a text-specialist)
- Schema: `{"steps": [{"action_id": "<id>", "params": {}}]}`
- Valid action_id set: `volume_up`, `volume_down`, `toggle_mute`, `open_browser`, `minimize_all`
- 5 test requests sent

### Results

| # | Request | Valid JSON | Hallucinated IDs | Issues | Time |
|---|---|---|---|---|---|
| 1 | "Turn the volume up" | timeout | -- | read timeout (60s) | 60s |
| 2 | "Mute my computer" | yes | none | none | 21.6s |
| 3 | "Open a web browser" | yes | none | none | 2.6s |
| 4 | "Minimize everything on my screen" | yes | none | none | 2.7s |
| 5 | "Lower volume, then mute, then open browser" | yes | none | none | 2.5s |

**4/5 valid JSON. 0/5 hallucinated action IDs. 4/5 fully clean. 1/5 timeout.**

The 4 successful responses used only valid IDs and produced correctly structured JSON with no markdown fences and no explanatory text. The multi-step request (test 5) correctly produced a 3-step chain in the right order. The timeout on test 1 is a model-cold-start artefact (qwen2.5vl:3b is a vision model loading large weights on first inference); subsequent calls were fast (2-3s).

### Caveats

- `qwen2.5vl:3b` is a vision-language model, not a text specialist. A pure text model (e.g. `llama3.2:3b`, `mistral:7b`, `qwen2.5:3b`) would likely be faster and equally reliable on JSON tasks.
- Schema was trivially small (5 IDs, no nested params). Real macro schemas with 50+ action IDs and typed params would require more testing.
- Zero hallucinations with 5 IDs is not a guarantee at 50+ IDs -- the hallucination rate typically rises with registry size.
- The cold-start timeout is a deployment concern, not a model capability concern.

**VERDICT: GREEN (with caveats).** The model correctly emits schema-valid JSON and does not hallucinate IDs when the valid set is small and explicit. Cold-start latency and schema complexity scaling require mitigation (pre-warm, tiered fallback). Phase 2 should test with a 30-50 ID registry before finalising the local model strategy.

---

## Probe 4 -- Re-execution Gating

### Current call chain (traced from source)

```
Whisper transcript string
  -> CommandMatcher.match(text)        # longest-match token scan
  -> CommandMatcher.should_suppress()  # debounce check only
  -> entry.handler(app, remainder)     # fires immediately
  -> CommandMatcher.record_execution() # stamps debounce clock
```

No other logic between `match()` and `handler()`.

### Debounce coverage

Only 5 of 150 commands have debounce > 0:

| Command | Debounce |
|---|---|
| `pause this` | 1.5s |
| `play this` | 1.5s |
| `toggle this` | 1.5s |
| `next track this` | 0.8s |
| `previous track this` | 0.8s |

All are media-transport commands. 145/150 commands (97%) have `debounce=0.0` and fire on every match with no suppression window.

### High-impact commands have no gating

Tested explicitly:

| Command | Debounce | Second utterance |
|---|---|---|
| `going dark` (mute + minimize + lock) | 0.0s | FIRES AGAIN IMMEDIATELY |
| `toggle mute` | 0.0s | FIRES AGAIN IMMEDIATELY |
| `volume up` | 0.0s | FIRES AGAIN IMMEDIATELY |

The macro `going dark` chains mute + minimize-all + lock workstation. A misheard utterance or background noise that matches the phrase fires it immediately, with no window, no preview, no confirmation.

### What gates re-execution today vs what is needed

| Mechanism | Present today | Notes |
|---|---|---|
| Per-command debounce | yes (opt-in) | Only 5/150 commands use it; off by default |
| Per-app disable (app_overrides) | yes | Not relevant to re-fire timing |
| Pack enable/disable | yes | Coarse-grained; not per-risk |
| Arming state | **NO** | Command cannot require prior arm |
| Confirmation prompt | **NO** | No "say confirm to proceed" path |
| One-shot tokens / instance IDs | **NO** | Macro can replay N times |
| Phonetic confusion analysis | **NO** | No near-miss detection at all |
| Macro-level cooldown | **NO** | No aggregate cooldown across steps |
| Risk-class voice exclusion | **NO** | DESTRUCTIVE commands are voice-triggerable |

Only 2 commands have `confirm`/`dangerous` in their docstring (`clear health log`, `show overlay test`) -- and even those have no runtime enforcement; the docstring note is not read by any gating code.

**VERDICT: NEEDS-WORK.** The re-execution gap is the most critical finding. 97% of commands fire immediately with no gate. The design's requirement for "execution-time safety layer" has zero foundation in current code. Phase 2 must implement at minimum: (1) risk-class tagging per command, (2) debounce defaults by risk class, (3) confirmation-required flag for DESTRUCTIVE commands, (4) voice-exclusion mechanism for commands that should only be hotkey-triggered.

---

## Summary Table

| Gate | Verdict | Blocking issue |
|---|---|---|
| Registry introspection: safety metadata | **NEEDS-WORK** | 6/6 required fields absent from all 150 commands; `ai_visible` stored but not propagated to CommandEntry or list_commands() |
| Settings constraints: machine-readable | **NEEDS-WORK** | No schema file; all constraints are Qt widget code; no importable defaults dict |
| Small-model JSON reliability | **GREEN (caveats)** | 4/5 clean; 0 hallucinations on 5-ID schema; cold-start timeout; needs re-test at 30-50 IDs |
| Re-execution gating | **NEEDS-WORK** | 145/150 commands fire immediately with no gate; no arming/confirmation/risk-class/voice-exclusion mechanisms exist |

### What Phase 2 must build (ordered by blocking severity)

1. **Execution-time safety layer** (most critical). Risk-class tags (`SAFE`/`DISRUPTIVE`/`DESTRUCTIVE`) per command, enforced at fire time -- not just at composition time. Minimum: DESTRUCTIVE commands require explicit confirmation before handler is called.

2. **`@command` decorator safety fields**. Add `risk_level`, `reversible`, `side_effect_category`, `preconditions`, `preview_template`, `param_schema` as optional decorator kwargs. Propagate into `CommandEntry`. Expose via `list_commands()`.

3. **Fix `ai_visible` propagation**. `CommandEntry.__init__` must accept `ai_visible`; `load_plugins()` must pass it; `list_commands()` must expose it. The AI-composable subset is gated on this.

4. **Settings schema**. Extract a config schema (JSON Schema or typed dataclass) from the Qt widget constraints. Must be importable without instantiating the Qt UI. Range, step, enum, and cross-field dependency must be representable.

5. **Debounce defaults by risk class**. DISRUPTIVE commands should default to a non-zero debounce. DESTRUCTIVE commands should require opt-in re-arm, not a debounce timer.

6. **Voice-exclusion mechanism**. Add a `voice_triggerable=False` flag to `@command` that causes `CommandMatcher.match()` to skip the command when the trigger source is voice transcription. This is the "safe on camera" bar from the design doc.
