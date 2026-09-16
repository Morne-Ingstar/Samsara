MODEL: gpt-6-astra

## Premise checks — working tree, not HEAD

Retain queue 93 as an observer. Do not activate the proposed unified gate or rename the command vocabulary on this evidence. The protection is worth pursuing; the blanket accessibility cost is not justified. Explicit command addressing offers a better basis for the next decision.

| Brief premise | Status | Evidence and correction |
|---|---|---|
| The gate is observer-only. | CONFIRMED | It is called after dictation dispatch; its decisions do not authorize actions. [dictation.py:8649](C:/Users/Morne/Projects/Samsara-dev/dictation.py:8649), [shadow.py:1](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:1). |
| Replay changed 18 command outcomes to 3. | CONFIRMED as reported output | [replay.txt](C:/Users/Morne/Documents/Claude/reports/93/artifacts/replay.txt). These are hypothetical command classifications, not 18 observed executions or independently established false positives. Replay not executed in this review. |
| All three single-word checks cover suggestions and commands. | STALE / contradicted by the tree | Existing suggestions bypass canonical/alias checks; inexact resolved candidates can become suggestions before those checks. Chains check only the first canonical ID. Sources and isolated probes below. |
| Zero word-penalty guarantees literal registered command words. | STALE / contradicted by the tree | Handwritten grammar rules can return nonliteral forms with default zero penalty. Argument matching has separate penalties. [grammar.py:138](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:138), [grammar.py:494](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:494), [grammar.py:390](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:390). |
| Prefix is optional and does not waive rule 2. | CONFIRMED | Empty default, prefix stripping, and rule ordering are explicit. [resolve.py:139](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:139), [resolve.py:300](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:300), [resolve.py:340](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:340). |
| 61 single-word canonicals and 21 additional commands with short aliases. | CONFIRMED for the checked-in catalog as currently modified | Direct JSON inventory reproduced both counts; this was not a live-registry reload. Output below. |
| The 17,797 positives represent the whole current 487-command catalog. | STALE | The fixture contains 481 command IDs; the catalog contains 487. Six current IDs are absent, including `quick_memo.open_memos`, one replay survivor. Output below. Live fixture-regeneration tests not executed. |
| “Zero misses” establishes command recall. | STALE as an interpretation | `miss` means no decision was produced, not an intended command was missed. [shadow.py:19](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:19). |
| The remaining cases are solved by focus. | STALE as an established conclusion | Replay demonstrates residual ambiguity, not that focus separates intended commands from dictation. This is a hypothesis to test. |

The underlying work already exists; I reviewed it without implementing or rewriting it. Relevant history includes `3c2265f` (grammar, explicitly not wired) and `21d5cba` (catalog). Current resolver, shadow, catalog and evaluation edits are uncommitted. Historical report line numbers were checked against today's tree rather than assumed current.

## 1. Is the trade worth taking?

**Not as offered.** For someone who cannot fall back to typing, preventing unintended actions and preserving access to intended actions are both requirements. A lower false-command count does not purchase permission to make frequently used commands unreachable. Equally, an accidental persistent write deserves more weight than a harmless suggestion; counting them as equivalent events would misprice the trade.

My condition is concrete: keep unaddressed dictation available as text, preserve familiar commands through an explicit voice command lane or one-shot prefix, and provide voice-only correction and confirmation. Until that exists and is measured, keep these changes in shadow mode. The current observer imposes no demonstrated vocabulary cost on live dispatch; the cost under discussion is the proposed integration.

The replay is encouraging evidence of fewer collisions with accepted dictation. It is not a complete benefit/cost experiment. The observer samples utterances already accepted by the DICTATE lane, so it omits the command workload needed to measure accessibility loss. [shadow.py:3](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:3).

Nor is the historical comparison cleanly attributable to these two rules alone: the replay compares logged old outcomes against current replay outcomes, and one surviving utterance changes command ID from `app_verbs.open` to `quick_memo.open_memos`. That establishes changed resolution, though not its cause. Re-run both policies against the same frozen catalog, context and inputs before claiming a causal reduction. The stored row records rules version but no catalog fingerprint. [replay.txt](C:/Users/Morne/Documents/Claude/reports/93/artifacts/replay.txt), [shadow.py:160](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:160).

## 2. What does the corpus measure?

**18.1 percentage points is a real loss on this generated fixture, not an estimate of your daily failure rate.** It could overstate your cost by counting unused synthetic variants, or understate it by giving a frequently used command the same weight as a rarely used one. We cannot choose between those explanations from this corpus.

The generator allocates 37 positives per command and pads short commands with filler, case and punctuation combinations. My count found 12,185 positives labelled `filler`. These are useful regression inputs, but they are neither independent voice trials nor usage frequencies. [gen_intent_eval.py:206](C:/Users/Morne/Projects/Samsara-dev/tools/gen_intent_eval.py:206), [test_intent_grammar_eval.py:123](C:/Users/Morne/Projects/Samsara-dev/tests/test_intent_grammar_eval.py:123).

The score also has a narrower meaning than successful control: “identified” accepts a matching ID anywhere in the suggestions, and “executable” checks the resolved ID, without asserting the intended arguments, target or complete chain. A correct command name with the wrong destination can score as success. [test_intent_grammar_eval.py:81](C:/Users/Morne/Projects/Samsara-dev/tests/test_intent_grammar_eval.py:81).

Splitting the metrics is useful, but retaining 0.85 for identification does not preserve the former execution guarantee. The 0.76 execution floor was chosen just below the observed result. It detects further regression; it does not independently establish that today's regression is acceptable. [test_intent_grammar_eval.py:39](C:/Users/Morne/Projects/Samsara-dev/tests/test_intent_grammar_eval.py:39).

Evidence that would settle the cost: a paired evaluation of your actual, frequency-weighted tasks, including intended commands, ordinary dictation, literal command phrases being dictated, short answers, retries and focus changes. Label intended action/text, arguments and destination before scoring. Use your microphone and transcription path, retain uncertain labels, and reserve later sessions as held-out evaluation. Test the policies on the same frozen inputs and catalog. Learning on all 1,789 examples and reporting performance on those same examples is development evidence, not generalization evidence.

## 3. Attack rule 1's three legs

**Canonical length is the weakest leg.** It is catalog metadata, not evidence of user intent. In an isolated probe, changing only the canonical word count changed an identical two-word matched alias from dictation to executable. Adding a longer alias does not repair this: the canonical check still blocks it. [resolve.py:149](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:149), [resolve.py:306](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:306).

There is a concrete error in the implementation report: it says the longer aliases of `ask_ollama.yes`, including `go ahead`, still execute. The current catalog plus the actual exact-tier and rule methods instead produce `dictation / one_word_command` without a prefix. [93/FINAL.md:344](C:/Users/Morne/Documents/Claude/reports/93/FINAL.md:344). Probe 7 below verifies the contradiction. This is a resolver finding, not a claim that the live app currently rejects that answer.

**Matched-alias length is useful ambiguity evidence but a bad universal veto.** A one-word verb plus a required object is a complete command interaction. The catalog's `app_verbs.open` has alias `open` and required `app_name`; a synthetic request such as `open notepad` is not merely a stray one-word utterance. Conversely, a short trigger followed by unrestricted prose remains highly ambiguous. Treat argument structure and explicit addressing as relevant; don't count only the verb. Catalog output below verifies the example.

**Utterance length is a sensible conservative default outside an owned interaction.** Inside a pending confirmation or numbered selection, short answers are the intended vocabulary. Preserve them only while that specific interaction owns the response, with a bound target, expiry/cancellation and clear audible state. Overlay visibility alone is insufficient. This is a design recommendation, not a claim that those safeguards already exist.

Additional verified implementation limits:

| Finding | SEVERITY if used for authorization | CONFIDENCE | Evidence |
|---|---|---|---|
| Canonical and alias checks do not cover all suggestions; inexactness takes precedence. | High if a future suggestion claims and suppresses text; no such loss demonstrated today. | High | [resolve.py:300](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:300), [resolve.py:327](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:327); probes 2–3. |
| A chain can pass with a single-word canonical in a later member when its matched alias has two words. | High: violates the declared all-command restriction. | High for the rule-path defect; end-to-end live reachability not tested. | [resolve.py:241](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:241), [resolve.py:306](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:306); probe 4. |
| Canonical metadata blocks existing longer confirmation aliases. | High accessibility cost if integrated unchanged. | High for resolver behavior. | Probe 7 and sources above. |

Demoting 64 suggestions does not structurally eliminate all possible text suppression: suggestions still remain. The necessary guarantee concerns what the delivery path does with every unaddressed utterance, regardless of its classification. Today's observer already leaves delivery alone.

## 4. Should a prefix ever permit fuzzy execution?

**A prefix should establish that you are addressing the app; it should not automatically approve an uncertain action. But a spoken confirmation must be able to approve a candidate you have heard and understood.** Otherwise a persistent transcription error can make a command inaccessible despite the system repeatedly identifying it correctly.

The proposed interaction is: address the app, hear the proposed action and target, then confirm or correct it by voice. Confirmation approves that specific structured action through normal execution policy; it does not globally disable matching safeguards or reinterpret arbitrary text as permission. A failed explicitly addressed command should remain a recoverable command failure, not silently become typed text. The existing dispatch contract already distinguishes claimed failure from an unclaimed miss. [command_registry.py:44](C:/Users/Morne/Projects/Samsara-dev/samsara/command_registry.py:44).

**The current “exact” guarantee also needs correction before anyone relies on it.** The production scroll rule accepts the synthetic phrase `go page downwards`, returns no registered alias and zero word-penalty, and passes the execution rules in an isolated probe. Its catalog aliases are `down one page`, `page down`, and `scroll page down`. This is an explicit grammar expansion, not a literal alias match. [grammar.py:494](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:494), probe 5 below.

Arguments are another boundary: fuzzy application-name matching has a slot penalty that does not enter `word_penalty`; number parsing explicitly accepts homophones. Thus zero command-word penalty is not proof of an exact target or argument. [grammar.py:267](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:267), [grammar.py:390](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/grammar.py:390), [normalize.py:200](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/normalize.py:200). Argument cases were source-reviewed, not executed.

SEVERITY: high if “exact” is treated as authorization. CONFIDENCE: high in the mismatch; no claim that the synthetic scroll phrase would cause a harmful action. Deliberately supported grammar forms may be desirable, but they must be represented honestly rather than inherit an exactness guarantee by default.

## 5. Is focus the next lever?

**Use focus to constrain targets and applicable commands, not to decide that speech is addressed to Samsara.** You can dictate about a command and intentionally invoke that same command in the same chat box. The 76% concentration in chat does not distinguish those two intentions. A global app-based command/dictation switch would also recreate a mode the user must track, now driven by focus.

The tribunal is right to insist on valid destinations and explicit ownership of short replies. Its stronger claim that focus establishes intent is not demonstrated. The current shadow context does not even include window titles or a verified text control, so its logs cannot validate a detailed control-based policy. [shadow.py:133](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:133).

Binding delivery still matters: if the target becomes invalid or changes, preserve the text for voice recovery rather than silently sending it elsewhere. That is separate from granting command authority. “Literal exact phrase” also remains ambiguous when quoted or discussed; neither word count nor confidence solves identical words with different intentions.

## 6. The cheaper construction

Keep your command vocabulary. Preserve the explicit command lane while evaluating a one-shot prefix for commands during dictation. Within an explicitly owned interaction, keep short replies such as confirmation words and digits. In unaddressed dictation, preserve text, including command phrases; optional suggestions must not consume it.

This construction preserves muscle memory and places the distinguishing signal at the moment of addressing the app. A prefix still costs speech, so retaining an explicit command lane for bursts of commands matters. It does not require renaming 61 commands or retiring efficient numbered responses globally. Whether it is cheaper in your actual use must be measured, not assumed.

A literal-text escape must also remain available for dictating the prefix itself followed by command words. Voice-only undo, cancellation and recovery are part of the interaction, not an optional keyboard fallback.

## 7. How to select the prefix

Select it empirically with your voice, not from a list of supposedly distinctive invented sounds:

1. Shortlist a few familiar, comfortable words or short phrases. The current prefix matcher already accepts a token sequence. [resolve.py:340](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:340).
2. Record intended prefix-plus-command requests under your actual microphone, transcription settings, speaking pace and ordinary noise. Include short commands, long arguments, pauses and connected speech. Score the complete routing result, not recognition of the isolated prefix.
3. Replay ordinary narration as negative examples, including discussion of Samsara, the candidate prefix and command vocabulary. Measure unintended activation as well as missed addressing. Check literal-text escape and recovery entirely by voice.
4. Choose on development sessions, then validate on separate later sessions. Prefer the shortest comfortable candidate that meets a predeclared accidental-activation limit while keeping intended addressing reliable. Do not select a winner from this chat-heavy negative log alone.

## Verification and limits

Executed: read-only source/history/status inspection, JSON inventory and seven isolated probes using `F:\envs\sami\python.exe -B -` with source supplied through stdin. The probes compiled selected production AST definitions unchanged into memory, used synthetic resolver state where stated, and called the actual rule methods. The chain probe supplied parsed members; it did not test end-to-end parsing. The confirmation probe used the current catalog's real aliases. No application handlers, live registry loader or `dictation` import ran.

The first probe harness exited with `NameError` because its AST selector omitted a tuple-assigned constant. The harness was corrected in memory; all seven subsequent assertions passed. This was a harness error, not an application failure.

Relevant pasted output:

```text
PROBE 1 same matched two-word alias; canonical count 1 -> dictation; count 2 -> resolved
PROBE 2 existing suggestion, one-word canonical and alias, three spoken words -> suggest, blocked=None
PROBE 3 fuzzy candidate, one-word canonical and alias, three spoken words -> suggest, inexact_match
PROBE 4 chain with canonical word counts [2,1], alias counts [2,2] -> resolved, two members
PROBE 5 production rule_scroll(go page downwards) -> scroll.page_down, source=rule, alias empty, word_penalty=0; rules -> resolved
PROBE 6 forced exact one-word command -> resolved; forced similarity -> suggest
PROBE 7 current catalog ask_ollama.yes alias go ahead -> tier exact -> dictation, one_word_command
CATALOG records 487
CATALOG one-word canonical 61
CATALOG multiword canonical with one-word alias 21
CATALOG app_verbs.open: aliases=['open']; required argument app_name
CATALOG scroll.page_down aliases=['down one page', 'page down', 'scroll page down']
FIXTURE rows 19240 positive 17797 command ids 481
FIXTURE filler positives 12185
CATALOG IDS NOT IN FIXTURE:
  builtin.scratch_everything
  quick_memo.open_memos
  show_numbers.grid_back
  show_numbers.hide_grid
  show_numbers.mouse_grid
  show_numbers.move_here
FIXTURE IDS NOT IN CATALOG []
```

The catalog/fixture discrepancy is verified. Whether the current live-registry fixture gate fails was not executed; the two JSON artifacts alone do not prove that outcome.

Not executed: pytest, full suite, full corpus rescoring, shadow replay, live app interaction, transcription trials or latency measurements. The tribunal's alleged latency violation compares different timing spans; the source explicitly separates `elapsed_us` from `t12_us`. I did not independently reproduce the reported timings. [shadow.py:23](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/shadow.py:23), [resolve.py:379](C:/Users/Morne/Projects/Samsara-dev/samsara/intent/resolve.py:379).

Branch before/after: `feature/v0.22` / `feature/v0.22`. Only this recommendation file was written. No implementation, catalog regeneration, configuration change, commit or external report/log write was performed, following this prompt's narrower one-file restriction.

SEVERITY of making the deployment decision from the current evidence: high, because voice is the only input method. CONFIDENCE in the source and fixture findings: high. CONFIDENCE in the exact real-world cost or the best prefix: undetermined until measured. The addressing recommendation is reasoned design judgment, not an experimentally proven result.

**ACCEPT WITH CHANGES — retain observer status; preserve familiar vocabulary through explicit addressing and owned replies; add voice confirmation of specific fuzzy candidates; guarantee text preservation; correct the rule-contract gaps and evaluate a frozen, representative workload before activation.**

**The one measurement that would change my mind:** the paired difference in first-attempt correct voice-only task completion rate on your held-out, frequency-weighted workload, comparing the unchanged blanket rules against the explicit-addressing design. Success requires the intended action or literal text, correct arguments and destination, and no unintended action, lost text or repair turn; an explicitly designed confirmation is part of the first attempt. If the unchanged rules reliably match or outperform the alternative under that definition, I would reconsider the vocabulary restriction. A higher generated-ID coverage score would not change my mind.

PROMPT EXECUTION-RULES-DECISION
