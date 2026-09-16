MODEL: gpt-6


# Samsara independent review — 98 v2

This audits the **uncommitted working tree**, not HEAD. The only written file is this report, replacing the superseded v1 stop report at the same path. Recommendations are judgments; they do not claim implemented functionality.

Evidence labels: **IMPLEMENTED** = source exists and was read; **VERIFIED (SIMULATED)** = isolated execution with substituted dependencies, including pure numerical calculations; **VERIFIED (END-TO-END)** = real application path run and observed; **UNVERIFIED** = not checked. No application functionality receives VERIFIED (END-TO-END) in this review. Source-only consequences are reasoned failure paths, not observed live incidents. File references are repository-relative unless an absolute document path is given.

## Premise check — first findings

| Brief claim | Disposition and evidence |
|---|---|
| Windows Python/PySide6 voice app, registry, Ava plugin, session manager and intent directory | **CONFIRMED / IMPLEMENTED:** `dictation.py:1969`, `samsara/commands.py:285`, `plugins/commands/ask_ollama.py:1011`, `samsara/session_modes.py:1519`; measured intent inventory below. |
| Working tree differs substantially from HEAD | **CONFIRMED:** baseline command below reports 105 modified paths and 112 untracked status entries at final measurement. Entries are not a count of all files nested under untracked directories. |
| Extraction and startup fixes already landed | **CONFIRMED / IMPLEMENTED:** `samsara/ui/settings_qt.py:704`, `samsara/boot.py:1`; history and dated artifacts in §4 and §8. V1's numbers are **STALE**. |
| Intent gate might be live | **Observer-only / IMPLEMENTED:** `dictation.py:8276`, `samsara/intent/shadow.py:186`; details in §2. |
| Three state labels plus hold workflows | **CONFIRMED / IMPLEMENTED:** `samsara/session_modes.py:61`, `samsara/audio_engine/wake_consumer.py:463`. DICTATE already contains a combined lane, so this is not three wholly isolated user workflows. |
| All supplied PNGs are current | **STALE:** first-run PNG says six steps; current `samsara/ui/first_run_wizard_qt.py:295` has seven. Other PNG/current-code correspondence is **UNVERIFIED**. |
| Seven guidance surfaces are entirely handwritten; colour consolidation is future work | **PARTLY STALE / IMPLEMENTED:** `samsara/ui/tutorial_qt.py:96`, `samsara/ui/command_cheatsheet_qt.py:49`, `samsara/ui/theme.py:135`; §9 details the remaining work. |
| Historical silent failures, native crashes, one-user deployment history | **UNVERIFIED as historical incidents:** no permitted runtime evidence was used to authenticate them. Current guards and remaining source paths were checked independently (§5–7). |
| “Pre-beta” | **STALE as a version description / IMPLEMENTED:** `samsara/__init__.py:7` declares `0.23.0-beta.1`. Release maturity cannot be inferred from that string. |

No structural mismatch requires stopping this version of the audit. The screenshot limitations block current visual certification, not the code review.

## §0 — Baseline, measured before judgment and refreshed after concurrent edits

Branch before/after: **feature/v0.22**. HEAD before/after: `eeebf6874df1576cbfc13a0fa1a9553c0dfe8872`. Initial disk baseline was 227 application Python files / 110,712 lines; the final remeasurement below supersedes it. Other work changed Ava-edit and streaming source during the review; the thread finding and streaming citations were refreshed. No frozen snapshot was created.

Counting definition: application = `dictation.py` plus all Python under `samsara/` and `plugins/`, including stale/draft and ignored source physically present; tests = `tests/` plus other `test_*.py`; tools = `tools/` and `scripts/`; remaining Python separate. Physical plugin module count includes ignored `plugins/commands/demo_commands.py`; it is not proof that the live loader registered every module. The git-visible-only alternative was 224 app files / 109,910 lines and 36 plugin modules; the physical inventory adds two ignored app files totalling 743 lines. Baseline script, exact command and output are reproduced in Appendix A.

```text
application: 226 Python files; 110653 lines

other: 13 Python files; 2474 lines

tests: 280 Python files; 81715 lines

tools: 67 Python files; 16207 lines

Largest application modules:

dictation.py: 14320

samsara/session_modes.py: 3381

samsara/streaming.py: 2568

plugins/commands/ask_ollama.py: 2253

plugins/commands/show_numbers.py: 2235

samsara/ui/home_qt.py: 2116

samsara/ui/history_view.py: 1655

samsara/execution_policy.py: 1622

samsara/ui/first_run_wizard_qt.py: 1507

samsara/audio_ducking.py: 1504

samsara/ui/mic_setup_wizard_qt.py: 1494

samsara/updater.py: 1464

samsara/audio_engine/wake_consumer.py: 1354

samsara/ui/settings_qt.py: 1345

samsara/ui/wake_word_debug_qt.py: 1330

Plugin modules excluding __init__: 37

Catalog records: 487

Catalog plugin/builtin: {'plugin': 199, 'builtin': 288}

commands.json builtins: 288

App version: 0.23.0-beta.1

Configured tests static estimate: 265 files; 5045 test function definitions; collection not executed

Intent source:

samsara/intent/__init__.py 21

samsara/intent/grammar.py 567

samsara/intent/normalize.py 302

samsara/intent/resolve.py 290

samsara/intent/shadow.py 272

Guidance source:

samsara/ui/first_run_wizard_qt.py 1507

samsara/ui/mic_setup_wizard_qt.py 1494

samsara/ui/ava_guide_qt.py 1004

samsara/ui/command_cheatsheet_qt.py 1005

samsara/ui/tutorial_qt.py 915

samsara/ui/quick_reference_qt.py 669

samsara/ui/stress_wizard_qt.py 543

Working tree status counts: {' M': 105, '??': 112}
```

Test collection: **not executed**. `tests/conftest.py:79` creates a temporary home during collection; `tests/test_boot_sequence.py:23` imports dictation. Those effects conflict with this task's write/import restrictions. The final **5,045 test-function definitions** are a static estimate, not collected cases, parametrization expansion or passing tests. Application speech, OS effects, installer/updater and Qt paths were not executed. Safe isolated checks are explicitly labelled and reproduced below.

The decision-changing findings are F1 (Ava result contract), F6/F12 (lost input without truthful recovery), F7 (replaced schedule remains active), F9/F10 (installer and updater entry paths). Fix these before a broad mode collapse or cosmetic redesign.

## §1 — Command system

**IMPLEMENTED.** There are overlapping resolution paths, not two independent sets of hands. Normal dispatch uses exact normalized matches then longest token prefixes (`samsara/command_registry.py:603`, `:613`); `samsara/commands.py:490` adds Smart Actions routing verbs after a miss. Ava command sessions use registry → focus/open/close grammar → fuzzy shortlist/model (`samsara/ava_command_session.py:431`). Ordinary Ava conversation uses another prompt and ACTION/ACTION2/SCHEDULE protocol (`plugins/commands/ask_ollama.py:75`, `:1011`). ACTION2 shares the deterministic app resolvers and policy (`:1144`). The newer general intent grammar is not this older three-verb grammar.

### F1 — Ava offers commands its execution path cannot run
**VERIFIED (SIMULATED). Severity: high. Confidence: high.**
Ordinary Ava takes only 100 alphabetically sorted names from the built-in dictionary (`plugins/commands/ask_ollama.py:877`), omitting plugins and even later built-ins such as “volume up.” The command-session shortlist does include plugins (`samsara/ava_command_session.py:199`), but both model paths eventually send ACTION to `CommandExecutor.execute_command`, which immediately rejects anything absent from the built-in dictionary (`samsara/commands.py:297`). Thus “show numbers” is selectable by the command-session model but cannot execute as ACTION. `handle_response` discards the false result (`plugins/commands/ask_ollama.py:1103`); the command session calls it a hit (`samsara/ava_command_session.py:343`), and ordinary Ava can display a success chip (`plugins/commands/ask_ollama.py:1905`, `:572`). The isolated real-function reproduction below returns no spoken refusal and a success chip.

**Cost:** the user learns that a capability works by exact phrase but becomes a silent no-op when paraphrased to Ava.
**Smallest fix:** route validated model proposals through one registry-entry execution API accepting canonical ID and arguments, with a DispatchResult returned to both Ava callers; build both menus from that same executable set.

### F2 — Scope and pack restrictions stop at matching
**IMPLEMENTED. Severity: high. Confidence: high for the missing check; real unintended action UNVERIFIED.**
Matching excludes disabled packs and nonmatching scopes (`samsara/command_registry.py:604`, `:617`; `samsara/command_scope.py:217`). Direct built-in execution does not consult either (`samsara/commands.py:297`–327). The entire policy decision at `samsara/execution_policy.py:788`–867 checks generation, existence, risk, schema and model allow-list, but no scope/pack. Ava's menus also do not filter either (`plugins/commands/ask_ollama.py:879`; `samsara/ava_command_session.py:213`). A direct/model/scheduled built-in can therefore get past a restriction that prevented its spoken match. A confirmed delayed plugin similarly reauthorizes risk without rematching scope (`samsara/commands.py:521`).

**Cost:** “available in this app” and “disabled” are not execution guarantees.
**Smallest fix:** enforce current scope and enabled-pack membership at the common effect boundary, including confirmed callbacks; report the refusal. Retain matcher filtering for discoverability.

**IMPLEMENTED, sound boundary:** claimed FAILED/REJECTED/QUEUED commands do not become dictation merely because execution failed (`samsara/commands.py:394`, `:515`–555; `samsara/ava_command_session.py:444`). Preserve that distinction.

## §2 — Dispatch and state

**IMPLEMENTED.** Collapse is feasible incrementally, but it is not a mode-enum deletion. `SessionModeManager` already receives injected command, agent, injection and feedback functions (`dictation.py:6681`), so the utterance decision seam is real. DICTATE already combines buffered text with a curated command probe (`samsara/session_modes.py:1869`, `_dispatch_utterance_locked`, combined-lane branch; `dictation.py:6582`). The general IntentResolver is an observer: `dictation.py:8276` calls shadow observation after dispatch, `:8324` builds it from live matcher rows, and `samsara/intent/shadow.py:186` accepts only staged/injected dictation outcomes. Its resolver is exact → grammar → lexical similarity, with no model tier (`samsara/intent/resolve.py:223`–272).

Specific couplings:
- Capture reads application booleans **and** manager mode: `samsara/audio_engine/wake_consumer.py:463`–495; hold suspension reads config at `:478`.
- Fatal capture cleanup writes those application state flags itself: `samsara/audio_engine/wake_consumer.py:623`–677.
- Session entry sets `command_mode_active` while resetting the manager to DICTATE: `dictation.py:7260`–7285. The old name is now also the combined-lane lifetime flag.
- Miss counters/timers and history are side effects in the manager's adapter closure: `dictation.py:6692`–6755.
- `samsara/streaming.py:2418` and `:2429` inspect mode and hold policy; `dictation.py:7238` separately maps mode to indicator colour.
- Ava has its own generation, queue and session lifetime: `samsara/ava_command_session.py:326`, `:431`–485; `dictation.py` methods `enter_ava_command_session` and `exit_ava_command_session`.

### F3 — Shadow evidence cannot establish addressing accuracy
**IMPLEMENTED. Severity: high for the proposed mode-collapse decision. Confidence: high.**
`samsara/intent/shadow.py:50` samples only utterances already accepted as dictation. `:136`–154 stores transcript, proposed decision, confidence, timing and app, but no user-intent label; `:109` explicitly lacks titles. It cannot measure missed audio, rejected utterances, command-path errors or whether background speech was addressed to Samsara. The cached resolver is built once (`:220`), so its catalog is also not automatically a continuously updated view.

**Cost:** a week of this log can look reassuring while saying little about the dangerous errors in an always-listening model.
**Smallest fix:** add an explicit user label and observed route/outcome to a bounded review sample spanning all accepted utterance routes, with a catalog version. Do not activate the gate based on raw shadow counts alone. Do not collect more dictated content by default merely to improve a dashboard.

## §3 — Tests

**IMPLEMENTED** for test bodies read; tests not run through pytest. The configured `tests/` tree contains **5,045 static test-function definitions in 265 test files** at the final measurement (initially 5,010 in 264). Neither is a collected count. Current line coverage and the number of behaviourally trustworthy tests are **UNVERIFIED**. No full-suite or coverage run was executed.

### F4 — Ranking quality is absent from the ranking regression gate
**VERIFIED (SIMULATED). Severity: medium. Confidence: high.**
All four methods in `tests/test_ava_command_session_shortlist_cap.py:35`–56 check size only. I executed those exact test bodies with the pure production shortlist helpers, then replaced the scorer with constant zero **in memory only**. All four still passed, while the top result for “command 287” became “command 000.” This does not mean the whole command subsystem can be deleted without failing tests. It means ranking can be deleted while this entire four-test file still passes.

`tests/test_ava_command_session_closed_world.py:127` additionally replaces shortlist, model and `handle_response`; its legitimate-selection test proves forwarding, not real plugin execution. That is a useful parser boundary test, but cannot detect F1.
**Cost:** an irrelevant shortlist can pass the gate and leave valid spoken requests unresolved or offered the wrong candidates.
**Smallest fix:** add a real-ranking utterance→candidate assertion and a shortlist→parsed plugin proposal→actual registry dispatch integration test with only the final OS effect substituted.

### F5 — Frozen smoke's “clean shutdown” never exercises cleanup
**IMPLEMENTED. Severity: high for release confidence. Confidence: high.**
`tools/frozen_smoke.py:239`–264 labels a `proc.terminate()` result “clean shutdown.” Windows TerminateProcess bypasses `quit_app`; it cannot test duck restoration, draft/history flushing or thread teardown. `tests/test_frozen_smoke_unit.py:15` checks the generated minimal config's own fields, not whether production onboarding uses them. The transport tests at `tests/test_component_fetch.py:75` exercise download bytes and sidecars, not the installer→EXE argument path.

**Cost:** green packaging evidence can coexist with the broken installer entry point in §7 and untested teardown.
**Smallest fix:** label the existing check “forced termination”; add one frozen-process test that invokes the real app quit path and verifies its completion marker.

The checked-in collision/orphan fixture is a **debt snapshot**, not proof of command reachability: `tests/test_command_canonical.py:32`–43 demands exact equality with `tests/command_catalog_known_collisions.txt`. Its original fixture commit is `21d5cba`. Such a test detects drift but explicitly permits the listed collisions; it does not resolve them.

**IMPLEMENTED, stronger test example:** `tests/test_mode_transition_crash_60.py:154`–228 creates the real Qt runtime and extracts production preview methods into a child-process scenario, with synthetic utterances. It is materially stronger than `tests/test_crash_diagnostics_87.py:305`, which replaces `_post` and disposal with list appenders. Neither was executed here; neither establishes that the currently reported native crashes are fixed.

## §4 — Extraction

**IMPLEMENTED.** Settings page extraction exists (`samsara/ui/settings_qt.py:704`–713, commit `e47c316`); boot utilities exist (`samsara/boot.py:1`, commit `c5eb7a6`); clipboard preservation is delegated (`dictation.py:11384` to `samsara/clipboard.py`); matching/execution is already in `samsara/commands.py`. Do not repeat those extractions.

| Proposed seam | Assessment based on source |
|---|---|
| Session dispatch | Real decision seam; the app adapter still owns paste/history/timers/feedback (`dictation.py:6681`–6755). Moving it wholesale with an unrestricted app reference merely moves the coupling. |
| Clipboard | Low-level preservation already extracted. Remaining wrapper selects Unicode versus paste, captures focus and records undo/learning (`:11310`–11409); that is delivery orchestration. |
| Hotkeys | Real capture ownership boundary, high risk: command/hold/session flags, suppression and locks intersect (`:6448`, `:12130`; `samsara/audio_engine/wake_consumer.py:391`). |
| Boot | Partly extracted; app construction and ACE construction remain in the monolith (`:1969`, `:3014`, `:14302`). |
| Plugin executor | Existing module; fix its route contract before another file move (`samsara/commands.py:285`, `:372`). |
| Ava session | Existing `ava_command_session.py` owns the waterfall; app lifecycle/generations still bridge it (`dictation.py:7520` vicinity). |
| Indicator wiring | A real adapter seam: `_update_mode_overlay`, `_indicator_success_and_reset`, `_indicator_reset` (`:7244`, `:10888`, `:10900`). It still needs a current-state source for delayed callbacks. |

**Recommendation:** the safest *next* cut is smaller than these lifecycle clusters: extract the pure transcription-quality predicates, starting with `dictation.py:1033` `_is_hallucinated_segments`, with their constants and explicit inputs. Its callers at `:1292`, `:1302`, `:7103`, `:8207` can delegate without changing locks, audio or Qt lifetime. This reduces the need for app imports in quality tests. It is not the highest-impact product work; §1's broken dispatch contract takes priority over a line-count target.

## §5 — Failure behaviour

### F6 — Fatal hands-free failure has cleanup but no persistent recovery explanation
**IMPLEMENTED. Severity: high for a mouse-free user. Confidence: high.**
The old “bad frame simply kills the thread” description is incomplete. `samsara/audio_engine/wake_consumer.py:687` catches exceptions and calls `_handle_fatal`, which clears queues and session flags (`:594`–677). But production constructs it without `on_fatal` (`dictation.py:3024`); the fallback is just `play_sound('error')` (`samsara/audio_engine/wake_consumer.py:679`–683). No restart is scheduled in that path.

**Cost:** listening stops; a deaf user gets no durable reason or recovery action from this fatal path, and voice may no longer be available to recover.
**Smallest fix:** wire the fatal callback to a persistent visible “Hands-free stopped” state and a large restart action; only auto-retry failures classified as transient, not arbitrary programming errors.

Other failure outcomes:
- **IMPLEMENTED:** elevated-window injection now returns failure at `dictation.py:11316`; the previous blanket claim that it always reports success is stale. The delivery guard exists; a real elevated-window run was **not executed**.
- **IMPLEMENTED:** Ava answers are exempt from command-feedback length suppression (`plugins/commands/ask_ollama.py:523`; `samsara/tts/coordinator.py:68`, `:180`). The old character-limit incident is not evidence of the same current bug.
- **VERIFIED (SIMULATED):** plugin ACTION failure can still yield an Ava success chip (F1).
- **IMPLEMENTED:** shadow I/O failures are counted and logged once (`samsara/intent/shadow.py:267`); appropriate for an observer, but the home screen must not treat missing observations as evidence of success.
- **IMPLEMENTED:** `qt_runtime.post` drops callbacks with only a log if runtime is not RUNNING (`samsara/ui/qt_runtime.py:81`); this is not a user-facing delivery acknowledgement. No reproduction of an unintended drop was executed.

## §6 — Concurrency and lifecycle

### F7 — A replaced scheduler can resume the old task
**VERIFIED (SIMULATED). Severity: high. Confidence: high.**
`plugins/commands/ask_ollama.py:1287` signals one shared stop event; `:1291` immediately clears it for the replacement. An old worker already inside its effect returns to the loop and observes the cleared event. The isolated test below used real threads and the real start/stop functions, with effects recorded in memory: old task fired twice after the replacement was started.
**Cost:** a supposedly replaced repeating action can continue alongside its replacement.
**Smallest fix:** give each schedule its own stop event captured by its closure; never clear an event owned by an older worker.

### F8 — Thread registration is neither clean nor a lifecycle guarantee
**IMPLEMENTED. Severity: medium. Confidence: high.** The actual checker result:
```text
Thread discipline check FAILED — 3 violation(s):



  samsara/ava_edit/session.py:337

    threading.Thread(target=_run, name="ava-edit-propose", daemon=True).start()

  samsara/tts/coordinator.py:254

    t = threading.Timer(duration_ms / 1000.0, self._on_duck_restore)

  plugins/assets/sleep_overlay.py:76

    threading.Thread(target=watch_stdin, daemon=True).start()
```
Command: `F:\envs\sami\python.exe -X utf8 -B tools/check_thread_discipline.py`; exit 1.

Only the Ava edit worker is a confirmed registration omission (`samsara/ava_edit/session.py:337`). The TTS timer is registered at `samsara/tts/coordinator.py:259`; its allowlist still says line 216. The standalone sleep overlay is an intended exception whose line moved from 75 to 76 (`tools/thread_discipline_allow.txt:7`). Do not report all three as leaked production threads.

The checker matches only literal `threading.Thread(` and `threading.Timer(` strings (`tools/check_thread_discipline.py:64`, `:132`), so aliases/subclasses can bypass it. The workflows read do not invoke this checker; `.github/workflows/ci.yml:59` runs pytest, with triggers limited to main/master at `:12`. The registry explicitly omits daemon joins (`samsara/runtime/thread_registry.py:218`, `:242`); `quit_app` ends with `os._exit(0)` (`dictation.py:14264`).

**Smallest fix:** register the Ava-edit worker, refresh the two reviewed exceptions, and require the checker in CI. Separately, treat service-owned cancellation/drain as a shutdown contract; a green registry scan does not supply it.

**IMPLEMENTED:** Windows instance control retains a named mutex handle (`samsara/single_instance.py:93`, `:159`). Boot deliberately fails open on unexpected mutex errors (`samsara/boot.py:547`). **Risk, severity medium, confidence high in code / UNVERIFIED occurrence:** this can admit two processes to one profile. Smallest correction: a visible lock-check failure with explicit retry, rather than a log-only unguarded launch.

The reported four access violations and RPC_E_DISCONNECTED causality remain **UNVERIFIED**. I did not inspect the live log (outside this task's allowed read paths) or reproduce a crash. Python thread registration cannot establish safe Qt/native object lifetime.

## §7 — Release surface

### F9 — Installer component fetching never reaches its entry point
**IMPLEMENTED. Severity: high. Confidence: high; installer run not executed.**
`installer/samsara.iss:279` passes `--fetch-components` and `:286` waits for process termination. `dictation.py:14266` enters the normal lock/splash/app startup without argument dispatch. `samsara/ui/first_run_qt.py:25` and `installer/README.md:60` explicitly acknowledge the missing wiring; the source search finds no caller of `fetch_components_main`.

**Cost:** without a running instance, post-install can wait on the normal long-running app; with one already running, the second invocation can exit 0 at the lock (`samsara/boot.py:542`), allowing a success interpretation without a fetch. The wizard's optional-components page exists (`samsara/ui/first_run_wizard_qt.py:814`), but it does not implement the installer's requested invocation.
**Smallest fix:** dispatch this CLI mode before normal app startup/instance locking and add a frozen EXE argument-path check against a local component fixture.

### F10 — The current beta cannot check for its stable successor
**VERIFIED (SIMULATED). Severity: high for beta distribution. Confidence: high.**
`samsara/__init__.py:7` declares `0.23.0-beta.1`. `samsara/ui/update_qt.py:168` passes that value to `check_for_update`; `samsara/updater.py:257` sends it to a stable-only parser before any network request. The real parser reproduction below raises ReleaseMetadataError.
**Cost:** a user on this build cannot use the updater to discover even a later stable release.
**Smallest fix:** allow a prerelease *installed version* in comparison while keeping the offered-update policy stable-only if desired.

**IMPLEMENTED:** published CPU assets are downloaded from the successful release-build run and rehashed (`.github/workflows/release.yml:75`, `:99`); the old parallel-unverified-build criticism is stale. The spec asserts OWW model presence (`scripts/samsara.spec:80`). These are useful packaging boundaries, not evidence of working speech, injection or installation.

The local smoke harness launches isolated profiles and checks log markers/liveness (`tools/frozen_smoke.py:295`–363). CI intentionally runs a trimmed headless harness (`.github/workflows/release-build.yml:136`–159). Neither shown path invokes the installer or updater's swap/rollback. The updater contains archive validation, size/hash checks and rollback (`samsara/updater.py:367`, `:412`, `:935`–975); real interrupted updates, locked installs, permissions, proxy failures and rollback remain **UNVERIFIED**, not asserted failures. Signing is disabled in the workflow (`.github/workflows/release-build.yml:321`–342); a fresh-user trust-prompt experience was not executed.

## §8 — Startup

**UNVERIFIED:** current cold start to wake-word-ready cannot be established without a real boot under a defined cold-cache/device/model configuration. Boot profiling was **not executed**.

The most recent boot summary found is `perf_artifacts/boot_fixes_46.md`, mtime **2026-09-14 22:18:58 local**; `boot_fullboot_final.md` is **22:18:17**. The latter reports three warm-cache runs:
- startup complete: median **4.173 s**, range 4.049–4.332;
- ready for dictation: **4.168 s**;
- ACE start: **15 ms**;
- asynchronous Silero load: **63 ms**.

These are prior artifact claims, not VERIFIED (END-TO-END) results of this audit. `perf_artifacts/boot_fixes_46.md:7` disclaims cold reproduction; `:42` reports the old ACE/Silero stall already fixed. Its last section gives a prior cold startup-complete observation of **21.2 s**, and an inferred **17.2 s** after removing a four-second probe. Neither is a measured current cold wake-ready result. “Startup complete,” “dictation ready” and “wake models ready” must stay separate; `dictation.py` has explicit `wake_ready_state` / `_request_wake_models` paths.

**IMPLEMENTED:** the current loader explicitly loads Silero on the model-loading worker (`dictation.py:5357`–5365, spawned at `:5484`); wake start is requested separately at `:5392`–5396. Reading those stages cannot assign a present-day elapsed duration to them.

**Recommendation:** retire the 26.4-second diagnosis as a current planning fact. The next startup measurement should timestamp capability-specific readiness in the release artifact; do not repeat the already-completed import/calibration work on the strength of the old map.


## §9 — Interface, ranked by impact on a user who cannot use a mouse

Priority order: **1. truthful listening/failure/recovery state; 2. operable command discovery; 3. readable feedback; 4. consolidate guidance; 5. visual consistency.** These are recommendations, not implemented features. Screenshot observations below establish what those PNGs depict; correspondence to the current application is **UNVERIFIED** unless a specific mismatch is established. No UI path was run.

### 9a — Home: recovery earns space before the infinity counter

**IMPLEMENTED:** current Home has three destination cards, “View commands,” “Show guides & help,” and “Open Dictionary” (`samsara/ui/home_qt.py:98`); two numerical identity cards including a static infinity symbol (`:1661`); creed/support/links (`:1683`–1735); and diagnostic → what's-new → hidden slot priority (`:1853`–1903). It is inside a QScrollArea (`:1448`), not a layout that categorically forbids scrolling. The stated viewport budget is 548 pixels (`:1474`). There are actual 900×650 layout assertions for empty/diagnostic/what's-new states (`tests/test_home_qt.py:848`–876). Those tests were **not executed**; neither a current Home PNG nor a live rendering establishes today's fit.

### F11 — The home miss diagnostic reads a schema the writer does not produce
**VERIFIED (SIMULATED). Severity: medium. Confidence: high.**
`dictation.py:8505`–8514 appends **tuples** `(label, kind, time)` to `_outcome_ring`. `samsara/ui/home_signals.py:291`–303 counts misses only in **dicts**. Feeding the real writer's three MISS outcomes to the real reader returns `(0, 3)`. The diagnostic consequently does not become eligible from these misses (`:330`). Its wording also divides by all recent outcomes, not a documented command-only sample.
**Cost:** repeated command failure receives no promised guidance.
**Smallest fix:** use one outcome record schema for writer and reader and count only applicable command outcomes. Add this producer→consumer assertion; injected Hint fixtures in `tests/test_home_qt.py:841`, `:856` cannot catch the mismatch.

The slot is worth keeping **for actionable work**, not for a generic assessment of the user's competence. **IMPLEMENTED:** its actual inputs are pending correction-queue entries (`samsara/ui/home_signals.py:262`; `samsara/correction_queue.py:195`), empty/gated capture diagnostics (`samsara/ui/home_signals.py:282`) and the broken outcome-ring reader. It does **not** read the intent shadow log. Corrections are captured review items, not a denominator of all dictation errors; empty/gated captures are outcomes, not proof of a microphone fault. The text should describe the observation, then offer the relevant check. Current priority and stability while the user reads are appropriate (`samsara/ui/home_qt.py:1875`–1885).

### F12 — Home's readiness model can hide stopped hands-free capture
**IMPLEMENTED. Severity: high. Confidence: high in source; visible live failure UNVERIFIED.**
`samsara/ui/home_signals.py:173`–188 declares hands-free READY from “enabled, not snoozed, microphone present.” It checks neither consumer running state nor wake-model readiness nor the fatal state from F6. `dictation_state` similarly treats microphone presence as readiness (`:159`–170). The notice selector only exposes states classified as faults (`:228`–233), so these conditions cannot explain a dead consumer.
**Cost:** a user who has lost their input method can get no Home explanation even while the microphone still exists.
**Smallest fix:** make capability state consume the production wake-ready/consumer lifecycle state and last fatal reason, then give that fault a persistent “Restart hands-free” action. The actual wake readiness API already exists at `dictation.py:9192`.

**Concrete layout recommendation:** retain the three destination cards; replace the large infinity counter and reduce the daily word count to a footer value. Use the recovered space for persistent listening state, current external target and pending-draft/last-failure information, with one large context-dependent action: Stop listening, Restart hands-free, or Review pending text. Draft recovery is a real capability already handled by voice (`samsara/session_modes.py:1983`–1990), but has no permanent destination among Home’s three routes. Direct access to Voice help earns a place beside a failure; it currently appears through the conditional diagnostic (`samsara/ui/home_signals.py:326`) and known-fault notice (`samsara/ui/home_qt.py:1546`). Do not turn every command into a card.

A proposed 548-pixel content budget at 100% scale: 68 px for listening/primary action, 60 for target/draft/last result, 96 for the three destinations, up to 88 for one diagnostic/update, 52 for words/free-software/support links, 48 for four inter-row gaps and 24 for top/bottom margins: **536 px**. This is a sizing proposal, **not a rendered fit claim**; allow readable wrapping before enforcing it. When the diagnostic slot is absent, give its room back to content, not another promotional element. Keep free/open-source/accessibility identity as one readable sentence; infinity is a marketing statement disguised as a metric.

### 9e / 9f — Discoverability and operation are the next priority

**IMPLEMENTED:** the catalog contains 487 records (288 built-in, 199 plugin), not 481; records include aliases and scope, and matching applies enabled packs plus scope (`samsara/command_registry.py:603`–617). A static catalog record is not proof of a callable command in this user's current context. The exact number **reachable by voice in the live profile is UNVERIFIED**: runtime registration, collisions, enabled packs, dependencies, scope and argument requirements change it. Live-registry construction and voice execution were **not executed**. The baseline count is potential vocabulary, not a claim that all 487 work simultaneously.

**IMPLEMENTED:** substantial contextual discovery already exists: catalog-derived rows (`samsara/ui/command_cheatsheet_qt.py:49`), foreground/last-external-app context (`:86`), live-scope annotations and filtering (`:97`–119), and disabled-pack filtering (`:165`). Build on that. The separate “what can I say” command still chooses hard-coded app suggestions and speaks them (`plugins/commands/core_utils.py:141`–149), so it does not share this discovery model.

### F13 — The command reference has mouse-only affordances
**IMPLEMENTED. Severity: high for the target audience. Confidence: high in source; keyboard/UIA end-to-end not executed.**
Close is a QLabel with only a mousePressEvent (`samsara/ui/command_cheatsheet_qt.py:369`–379); pin is an 18-pixel-wide QLabel with another mousePressEvent (`:418`–425). The command list uses NoSelection and connects itemClicked, not itemActivated (`:673`–677). Rows are 32 pixels high (`:410`). These are not standard focusable button contracts.
**Cost:** the very surface meant to teach voice control asks for precise mouse interaction for its own controls, and keyboard activation is not wired to the list action.
**Smallest fix:** use named focusable buttons for close/pin, connect activation as well as click, and provide visible focus plus voice-accessible names. Aim for 44–48 px primary action targets for this audience; WCAG AA's minimum is 24 px with exceptions, not a universal 44 px rule. An 18 px pin requires checking spacing/alternative-target exceptions before declaring formal failure. [WCAG 2.2 target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)

**Recommendation:** make “What can I say here?” open **and speak** the same small contextual reference: current external app, six useful executable examples, plain argument examples, and voice actions “more commands,” category name, and command name. Reuse the canonical catalog, pack filter and scope context; preserve the last external target when the help window takes focus. Keep the full searchable list as the second level. Show disabled entries with a reason when requested. This has more value than exposing a raw command count.

### F14 — Moving hints are a weak primary discovery surface
**IMPLEMENTED. Severity: medium. Confidence: high.**
The marquee deliberately has no focus or mouse interaction (`samsara/ui/command_marquee.py:159`–162), samples phrases (`:170`) and animates on a timer (`:173`). Reduced-motion detection exists (`:168`), but that is not an in-app pause/read/repeat action.
**Cost:** someone who reads slowly cannot hold an interesting command still or act on it; the fixed accessible name does not make the painted changing text an operable command browser.
**Smallest fix:** replace the scrolling strip with one static contextual example and an accessible “More commands” action backed by the reference above. Automatic scrolling alongside other content needs a pause/stop/hide mechanism unless an exception applies; system reduced motion alone is not the complete interaction. [WCAG pause, stop, hide](https://www.w3.org/WAI/WCAG22/Understanding/pause-stop-hide.html)

**IMPLEMENTED:** outcome chips default to 1.8 seconds (`samsara/ui/listening_indicator.py:102`, `:829`), with per-outcome overrides in dispatch (`dictation.py:8541`). Do not pretend every failure has the same TTL. Recommendation: make failed delivery and stopped listening persist in Home until acknowledged or superseded; a transient chip and a sound cannot be the only recovery record. F6 identifies a concrete sound-only fatal path. The microphone screenshot itself pairs its green meter with explanatory text (`mic_wizard_page2_level.png`); do not call that particular result colour-only.

### 9c — Colour: finish the existing decision

**IMPLEMENTED:** `samsara/ui/theme.py:135` already declares cyan the sole brand colour; `:152`–155 makes BRAND_RED a compatibility alias for RECORDING. Current chips consume theme tokens (`samsara/ui/listening_indicator.py:87`–98). The brief's description of four wholly independent palettes is stale for these parts. Hard-coded mode colours still exist in `dictation.py:7238`. My recommendation is **keep cyan as brand, reserve red for capture/error semantics with different labels and shapes**, and remove remaining hard-coded mode duplicates. Reverting to competing red/cyan branding offers no accessibility benefit.

**VERIFIED (SIMULATED):** token contrast calculated from actual `samsara/ui/theme.py:91`–156 constants, alpha composited over the named background, using sRGB relative luminance. No visual rendering was performed. Full code/output below.

| Foreground | BG0 #0b0e14 | BG1 #131820 | BG2 #1a2030 |
|---|---:|---:|---:|
| Primary text | 15.718 | 14.491 | 13.214 |
| Secondary text (white .75) | 10.925 | 10.332 | 9.628 |
| Disabled text (white .40) | **3.804** | **3.817** | **3.740** |
| Cyan accent | 9.476 | 8.736 | 7.966 |
| Recording red | **3.552** | **3.275** | **2.986** |
| Success green | 12.511 | 11.534 | 10.517 |
| Warning amber | 11.571 | 10.668 | 9.727 |
| Error red | 6.983 | 6.438 | 5.871 |
| Ava violet | 7.098 | 6.544 | 5.967 |
| Border (white .16) | 1.561 | 1.630 | 1.660 |

AA requires 4.5:1 for ordinary text and 3:1 for qualifying large text; essential nontext state/controls generally need 3:1 against adjacent colour. [Text contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), [nontext contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html). Dark BG0 text on cyan is 9.476:1.

### F15 — A disabled token is used for readable support text
**VERIFIED (SIMULATED) for contrast; source use is included in the same token calculation finding. Severity: medium. Confidence: high.**
The support sentence uses TEXT_DISABLED (`samsara/ui/home_qt.py:1711`), but is not a disabled control. It misses 4.5:1 on all three background tiers.
**Cost:** ordinary information becomes difficult for low-vision users to read.
**Smallest fix:** use TEXT_SECONDARY for readable copy; reserve TEXT_DISABLED for genuinely inactive controls. Do not use RECORDING red as small text; if used as a necessary state icon over BG2 it also misses 3:1 (2.986 is not a passing 3.0 after rounding). This last case is a token-use constraint, not a claim that the live wheel currently uses BG2.

Colour-blind access requires words/icons as well as hue: “Listening,” “Recording,” “Stopped,” “Failed” must remain distinguishable without red/green discrimination. Current type floors are 14 px for metadata and 16 px for body (`samsara/ui/theme.py:180`–193), so old screenshots with tiny text do not establish a current global 12 px defect. Font-scale clipping and screen-reader order are **UNVERIFIED**; UI/accessibility testing was **not executed**.

### 9d — Guidance: consolidate tasks, not all content into catalog records

**IMPLEMENTED**, current file sizes measured from source; individual surface completion and correctness are **UNVERIFIED** beyond cited paths.

| Surface | Python lines | Recommendation |
|---|---:|---|
| First-run wizard | 1,507 | Keep initial setup, but reuse the microphone check instead of maintaining separate teaching. |
| Mic wizard | 1,494 | Keep as a repair/calibration task available without repeating onboarding. |
| Ava guide | 1,004 | Merge provider setup/readiness into Ava settings; place usage examples in the shared reference. |
| Command cheatsheet | 1,005 | Keep as the contextual executable reference. |
| Tutorial | 915 | Keep an optional replayable practice flow inside Guides; do not make it another command encyclopedia. |
| Quick reference | 669 | Merge into the reference as a pinned “Your controls” section. |
| Stress wizard | 543 | Keep under diagnostics, separate from teaching. |
| **Total** | **7,137** | Fewer competing destinations; shared content and controllers. |

Generation has already started: `samsara/ui/tutorial_qt.py:83`–96 picks examples from the catalog and enabled packs; `samsara/ui/command_cheatsheet_qt.py:49` builds catalog rows. Quick reference resolves configured wake/abort/session phrases (`samsara/ui/quick_reference_qt.py:235`), which cannot be replaced by a static command table. Mic calibration derives levels from measured room noise (`samsara/ui/mic_setup_wizard_qt.py:96`); stress testing consumes actual diagnostic captures (`samsara/ui/stress_wizard_qt.py:385`–417). Those are workflows, not duplicate prose.

There is still misleading prose: `samsara/ui/ava_guide_qt.py:945`–951 promises that “anything that changes something” is confirmed, while policy distinguishes risk classes (`samsara/execution_policy.py:788`–867). **IMPLEMENTED; severity medium; confidence high:** unconditional teaching creates a stronger safety promise than the executor. Smallest fix: describe exactly which risk classes require confirmation and generate examples from those declarations.

The sources are newer than the PNGs in a demonstrable case: first-run now declares seven steps including Components (`samsara/ui/first_run_wizard_qt.py:295`–303); its PNG says six. Current tutorial also reads configured wake availability (`samsara/ui/tutorial_qt.py:533`–538), so the old hold-key instruction screenshot does not prove every current user is forced into that instruction.

### 9b — Visual coherence: lower priority than the failures above

These observations are confined to the named rendered evidence; **UNVERIFIED** as claims about the current UI. No current equivalent render was generated.

| PNGs inspected | Specific inconsistency / defect | User cost and smallest correction |
|---|---|---|
| `first_run_wizard_page1.png`, `first_run_wizard_page2_usecase.png`, `first_run_wizard_page4_model.png`, `first_run_wizard_page6_complete.png` versus `mic_wizard_page1.png`, `mic_wizard_page2_level.png` | First-run uses dots plus “Step n of 6,” huge empty lower areas and a distant Next button; mic uses named Device/Level/Wake word/Done tabs in a compact 560×480 window. | Different navigation models make repair feel unrelated to setup. Reuse named step/navigation components; retain the task-specific meter. Severity low; confidence high in PNG. |
| `tutorial_step4_done.png` | The completion checklist's second line is clipped inside its card. | A completion result cannot be fully read. Let the card take its content height and check the longest strings. Severity medium; confidence high in PNG, current clipping unverified. |
| `support_tab_after.png` versus tutorial PNGs | Support gives multiple actions the same bright cyan emphasis; tutorial reserves it for the next primary step. | No clear first action during trouble. Promote one context-dependent support action; make alternatives secondary. Severity low; confidence high in PNG. |
| `voice_training_after.png` versus wizard PNGs | Training uses nested outlined groups, strip headers and repeated small Test buttons; wizard uses large rounded choice cards. | Dense controls demand more precise targeting. Reuse row spacing and standard button sizing, rather than another decorative container. Severity medium for target size; confidence medium without pixel hit-area verification. |
| `modes_tab_after.png` versus `first_run_wizard_page2_usecase.png` | Modes uses slate panels and a mint accent; first-run uses darker panels and cyan. | Same-role controls look different. Replace local styles with the current shared tokens. Severity low; confidence high in PNG. |

`tutorial_step1.png`, `tutorial_step2.png`, `tutorial_step3_back_button.png`, `history_list_default.png` and `splash_spinner.png` were also inspected. The July history image is historical evidence only; no current history defect is asserted from it. Decorative icon/motion proof sheets are not substitutes for actual operational UI screenshots.

**Missing usable current PNGs:** Home; current seven-step first-run including component selection; mic wake-test step; Ava guide; command reference; quick reference; stress wizard; Voice help; Dictionary; updater; live listening/error states. Existing settings/tutorial/mic/history PNGs were not verified against the complete current rendering. Inventory dates are in the evidence appendix. This blocks a current whole-interface visual certification, not the source audit above.


## §10 — Direction documents: settle the routing question

Both documents were read. Their dates are document claims, not commit timestamps: MAP heading **2026-09-11** with a **2026-09-13** status delta (`C:/Users/Morne/Documents/Claude/SAMSARA_MAP.md:1`, `:140`); VISION heading **2026-09-12**, with September 13 updates (`C:/Users/Morne/Documents/Claude/SAMSARA_VISION.md:1`, `:35`). File mtimes are **September 13, 17:54:04 (MAP)** and **02:43:47 (VISION)** local, reproduced below. Neither describes this uncommitted tree completely.

**IMPLEMENTED:** the code is closer to MAP's deterministic-first order in Ava command sessions (`samsara/ava_command_session.py:431`), but there is no single global exact→grammar→model router. Normal matching, the combined dictation lane, ordinary Ava and observer intent use different paths (§1–2). The intent resolver itself has no model fallback (`samsara/intent/resolve.py:223`–272). These read paths do not implement VISION's model-primary global interface.

**Judgment:** make natural speech the user-facing promise, but keep deterministic exact/argument parsing first in execution. Use a model only for unresolved intent, proposing typed canonical actions through the same scope/risk/result contract. That combines VISION's useful product goal with MAP's more suitable runtime order. A model need not run before “stop listening” or “click 3” for the product to accept natural speech. Free-form questions still belong in the conversational path.

Do not silently replace explicit addressing/session evidence with a language model's guess about whether speech is meant for the app. In dictation, the sentence “delete the file” can be text. MAP's “command phrase never typed” and “dictated sentence never executed” (`:20`–21) cannot both be guaranteed from the words alone. Preserve an explicit context/escape path and require confirmation where an uncertain interpretation could cause an effect.

The contradiction is harmful when used as two executable specifications: MAP `:17` says model last, VISION `:12` says model primary; MAP `:66` separates small resolver and larger conversation model, VISION `:22` asks for one warm model. The code demonstrably has divergent vocabularies/results (F1), but **UNVERIFIED:** the documents caused that defect. Their authorship does not prove causality.

Specific stale or unsound claims:
- **STALE / IMPLEMENTED evidence:** MAP's baseline sizes (`:6`), settings extraction status (`:84`, `:147`), grammar/boot/shadow queue (`:154`–156) lag the measured tree and extracted modules (§0, §2, §4, §8).
- **STALE / IMPLEMENTED evidence:** MAP `:144` says 481 catalog commands; current file has 487. MAP `:145` says generated guidance landed, which is supported by `samsara/ui/tutorial_qt.py:96` and `samsara/ui/command_cheatsheet_qt.py:49`; it should not be planned as entirely new work.
- **STALE / IMPLEMENTED evidence:** MAP's colour work (`:113`) is partly complete in `samsara/ui/theme.py:135`–155.
- **UNVERIFIED:** VISION's “0.5–1 s” model latency (`:15`) is not a hardware-independent budget established by this audit. No model benchmark was executed.
- **IMPLEMENTED evidence against a universal promise:** VISION's invisible safety/universal undo (`:16`) conflicts with explicit destructive confirmation and partial action semantics in `samsara/execution_policy.py:788`–867. Sending something to another person cannot generally be made un-sent by a local inverse command.
- **IMPLEMENTED evidence against the platform assumption:** VISION `:38` treats Windows UIA exposure as universal. This tree itself supplies text/OCR and mouse-grid routes (`plugins/commands/show_numbers.py:1483`, `:1950`); those routes should remain supported escape paths, not be designed away by the assumption.

The smallest architectural correction is a written invariant shared by both documents: **every route proposes the same canonical action; one executor checks current eligibility, performs it, and returns the actual outcome.** Settle that before changing how often a model participates.


## Evidence appendix — commands actually executed

All Python verification below used `-B`, standard-library parsing and extracted functions. It did **not** import dictation, initialize the app, call real OS command effects, load Whisper, access the live user profile, or run pytest. AST extraction deliberately excludes module initialization: these results are **VERIFIED (SIMULATED)**, not whole-module or end-to-end integration results. Source-reading commands are represented by their file:line citations; the commands establishing numeric/check results are reproduced here.

### A. Final baseline

Exact executed command (real output appears in §0):
```powershell
@'
import ast,json,subprocess
from pathlib import Path
from collections import defaultdict,Counter
files=set(subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],text=True).splitlines())
# Include ignored source files physically present, so plugin totals describe this disk tree.
for root in ['samsara','plugins','tests','tools','scripts','perf_artifacts']:
 files.update(p.as_posix() for p in Path(root).rglob('*.py'))
files=sorted(files)
groups=defaultdict(list)
for f in files:
 p=Path(f)
 if p.suffix!='.py' or not p.is_file(): continue
 count=len(p.read_text(encoding='utf-8-sig').splitlines())
 if f=='dictation.py' or f.startswith(('samsara/','plugins/')): group='application'
 elif f.startswith('tests/') or p.name.startswith('test_'): group='tests'
 elif f.startswith(('tools/','scripts/')): group='tools'
 else: group='other'
 groups[group].append((f,count))
for group,rows in sorted(groups.items()): print(f'{group}: {len(rows)} Python files; {sum(c for _,c in rows)} lines')
print('Largest application modules:')
for f,c in sorted(groups['application'],key=lambda row:-row[1])[:15]: print(f'{f}: {c}')
print('Plugin modules excluding __init__:',sum(1 for f in files if f.startswith('plugins/commands/') and Path(f).parent==Path('plugins/commands') and f.endswith('.py') and Path(f).name!='__init__.py'))
catalog=json.loads(Path('commands_catalog.json').read_text(encoding='utf-8-sig'))['commands']
print('Catalog records:',len(catalog))
print('Catalog plugin/builtin:',dict(Counter('builtin' if r.get('plugin')=='builtin' else 'plugin' for r in catalog)))
print('commands.json builtins:',len(json.loads(Path('commands.json').read_text(encoding='utf-8-sig'))['commands']))
for n in ast.parse(Path('samsara/__init__.py').read_text(encoding='utf-8-sig')).body:
 if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='__version__' for t in n.targets): print('App version:',ast.literal_eval(n.value))
nfiles=nfunc=0
for f in files:
 if f.startswith('tests/') and Path(f).name.startswith('test_') and f.endswith('.py') and Path(f).exists():
  nfiles+=1
  nfunc+=sum(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in ast.walk(ast.parse(Path(f).read_text(encoding='utf-8-sig'))))
print('Configured tests static estimate:',nfiles,'files;',nfunc,'test function definitions; collection not executed')
print('Intent source:')
for p in sorted(Path('samsara/intent').glob('*.py')): print(p.as_posix(),len(p.read_text(encoding='utf-8-sig').splitlines()))
print('Guidance source:')
for stem in ['first_run_wizard','mic_setup_wizard','ava_guide','command_cheatsheet','tutorial','quick_reference','stress_wizard']:
 p=Path('samsara/ui')/(stem+'_qt.py'); print(p.as_posix(),len(p.read_text(encoding='utf-8-sig').splitlines()))
print('Working tree status counts:',dict(Counter(line[:2] for line in subprocess.check_output(['git','status','--porcelain'],text=True).splitlines())))
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```


### B. Updater parser, shortlist mutation and scheduler replacement

```powershell
@'
import ast,re,difflib,threading,types
from pathlib import Path
def extract(path,names,ns):
    tree=ast.parse(Path(path).read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    mod=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod),path,'exec'),ns)
# Real updater parser; no updater/module import and no network.
ns={'re':re}
tree=ast.parse(Path('samsara/updater.py').read_text(encoding='utf-8-sig'))
for n in tree.body:
    if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_STABLE_VERSION_RE' for t in n.targets):
        exec(compile(ast.Module(body=[n],type_ignores=[]),'samsara/updater.py','exec'),ns)
ns['ReleaseMetadataError']=type('ReleaseMetadataError',(Exception,),{})
extract('samsara/updater.py',{'_parse_stable_version'},ns)
try: ns['_parse_stable_version']('0.23.0-beta.1')
except Exception as e: print('Updater current version:',type(e).__name__+':',e)
print('Updater stable control:',ns['_parse_stable_version']('0.23.0'))
# Execute all four real shortlist-cap test bodies with pure production helpers.
ns={'difflib':difflib,'_DEFAULTS':{'shortlist_size':12}}
extract('samsara/ava_command_session.py',{'_fuzzy_score','_all_ai_visible_phrases','_build_shortlist'},ns)
api=types.SimpleNamespace(_build_shortlist=ns['_build_shortlist'],_DEFAULTS=ns['_DEFAULTS'])
tns={'SimpleNamespace':types.SimpleNamespace,'ava_command_session':api}
extract('tests/test_ava_command_session_shortlist_cap.py',{'_make_app','TestShortlistSizeCap'},tns)
for label in ['original','ranking replaced with constant zero']:
    if label!='original': ns['_fuzzy_score']=lambda *a:0
    obj=tns['TestShortlistSizeCap'](); count=0
    for name in sorted(vars(type(obj))):
        if name.startswith('test_'): getattr(obj,name)(); count+=1
    top=ns['_build_shortlist'](tns['_make_app'](288),'command 287',{'shortlist_size':1})
    print(f'Shortlist cap tests ({label}): {count}/4 passed; top for command 287 = {top}')
# Real scheduler start/stop functions, real threads; effects replaced with an in-memory recorder.
entered=threading.Event(); release=threading.Event(); repeated=threading.Event(); spawned=[]; calls=[]
def spawn(name,target,daemon=True):
    t=threading.Thread(name=name,target=target,daemon=daemon); spawned.append(t);t.start();return t
def effect(app,task):
    calls.append(task['confirm_text'])
    if task['confirm_text']=='old':
        if calls.count('old')==1: entered.set(); release.wait(2)
        else: repeated.set(); sns['_scheduler_stop'].set()
sns={'thread_registry':types.SimpleNamespace(spawn=spawn),'_scheduler_lock':threading.Lock(),'_scheduler_stop':threading.Event(),'_scheduled_task':None,'_scheduler_thread':None,'_execute_safe':effect}
extract('plugins/commands/ask_ollama.py',{'_start_schedule','_stop_schedule'},sns)
sns['_start_schedule'](None,{'interval_seconds':0.01,'confirm_text':'old'})
assert entered.wait(2)
sns['_start_schedule'](None,{'interval_seconds':60,'confirm_text':'replacement'})
release.set(); assert repeated.wait(2)
sns['_stop_schedule']()
for t in spawned:t.join(2)
print('Scheduler effects after replacement:',calls)
print('Scheduler cleanup: all test threads stopped =',all(not t.is_alive() for t in spawned))
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```

Actual output:
```text
Updater current version: ReleaseMetadataError: Expected a stable version such as v0.22.1, received '0.23.0-beta.1'.

Updater stable control: (0, 23, 0)

Shortlist cap tests (original): 4/4 passed; top for command 287 = ['command 287']

Shortlist cap tests (ranking replaced with constant zero): 4/4 passed; top for command 287 = ['command 000']

[AVA SCHEDULER] Started: old every 0.01s

[AVA SCHEDULER] Started: replacement every 60s

[AVA SCHEDULER] Fired: old

[AVA SCHEDULER] Fired: old

[AVA SCHEDULER] Stopped: old

[AVA SCHEDULER] Stopped: replacement

Scheduler effects after replacement: ['old', 'old']

Scheduler cleanup: all test threads stopped = True
```

The four shortlist assertions are the actual test methods extracted from the named file; pytest fixtures/imports were not run. The scheduler test substitutes an in-memory effect recorder and thread-spawn wrapper, uses real Python threads, and stops/joins every spawned test thread.

### C. Ava plugin action and truncated model menu

```powershell
@'
import ast,json,re,types
from pathlib import Path
def load_functions(path,names,ns,method=False):
 t=ast.parse(Path(path).read_text(encoding='utf-8-sig'))
 body=next(n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='CommandExecutor').body if method else t.body
 nodes=[n for n in body if isinstance(n,ast.FunctionDef) and n.name in names]
 mod=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])
 exec(compile(ast.fix_missing_locations(mod),path,'exec'),ns)
builtins=json.loads(Path('commands.json').read_text(encoding='utf-8-sig'))['commands']
print('Builtin command names:',len(builtins))
visible=sorted(k for k,v in builtins.items() if v.get('ai_visible',True))
print('Ava ordinary prompt visible builtins:',len(visible),'sent:',len(visible[:100]))
print('volume up in sent list:','volume up' in visible[:100])
spoken=[]
ns={'re':re,'Route':types.SimpleNamespace(EXACT='exact',MODEL='model'),'speak':lambda a,t:spoken.append(t),'MODEL_UNAVAILABLE':'unavailable','_track_alias_uses':lambda t:None,'_turn_local':types.SimpleNamespace(speech=[]),'_CHIP_CHECK':'CHECK'}
load_functions('samsara/commands.py',{'execute_command'},ns,True)
ex=types.SimpleNamespace(commands=builtins)
ex.execute_command=types.MethodType(ns['execute_command'],ex)
app=types.SimpleNamespace(command_executor=ex)
load_functions('plugins/commands/ask_ollama.py',{'_parse_structured_response','handle_response','_answered_chip'},ns)
print('show numbers is builtin:','show numbers' in builtins)
print('show numbers is catalog plugin:',any(r['kind']=='plugin' and 'show numbers' in r['aliases'] for r in json.loads(Path('commands_catalog.json').read_text())['commands']))
ns['handle_response'](app,'CONFIRM Show numbers.\nACTION show numbers',generation=1)
print('Plugin ACTION through real handle_response + execute_command: spoken =',spoken,'; next answered chip =',ns['_answered_chip']())
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```

Actual output:
```text
Builtin command names: 288

Ava ordinary prompt visible builtins: 261 sent: 100

volume up in sent list: False

show numbers is builtin: False

show numbers is catalog plugin: True

[AVA RAW] 'CONFIRM Show numbers.\nACTION show numbers'

Plugin ACTION through real handle_response + execute_command: spoken = [] ; next answered chip = ('Ava CHECK', 'success')
```

Only the rejected plugin branch of execute_command is exercised here. The success-chip call reproduces the caller's next statement at `plugins/commands/ask_ollama.py:1907`; it does not claim an actual model or desktop action ran.

### D. Contrast and home outcome producer/consumer

```powershell
@'
from pathlib import Path
import ast,re,types,collections,time
t=ast.parse(Path('samsara/ui/theme.py').read_text(encoding='utf-8-sig')); tokens={}
for n in t.body:
 if isinstance(n,ast.Assign):
  try:v=ast.literal_eval(n.value)
  except (ValueError,TypeError):continue
  for target in n.targets:
   if isinstance(target,ast.Name):tokens[target.id]=v
def rgb(s):
 if s.startswith('#'):return [int(s[i:i+2],16)/255 for i in (1,3,5)]
 return [float(x)/255 for x in re.findall(r'[0-9.]+',s)[:3]]
def lum(v):return sum(c*w for c,w in zip([x/12.92 if x<=0.04045 else ((x+0.055)/1.055)**2.4 for x in v],[0.2126,0.7152,0.0722]))
def contrast(f,b):
 bg=rgb(b);fg=rgb(f)
 if f.startswith('rgba'):
  a=float(re.findall(r'[0-9.]+',f)[3]);fg=[a*x+(1-a)*y for x,y in zip(fg,bg)]
 l1,l2=sorted([lum(fg),lum(bg)],reverse=True);return (l1+0.05)/(l2+0.05)
print('Contrast ratios; alpha composited over named background:')
for name in ['TEXT_PRIMARY','TEXT_SECONDARY','TEXT_DISABLED','ACCENT','RECORDING','SUCCESS','WARNING','ERROR','AVA','BORDER']:
 print(name,str(tokens[name]),' | '.join(f'{bg} {contrast(tokens[name],tokens[bg]):.3f}:1' for bg in ['BG0','BG1','BG2']))
print('Dark text BG0 on ACCENT:',f'{contrast(tokens["BG0"],tokens["ACCENT"]):.3f}:1')
# Exact production producer and consumer, without importing app or Qt.
tree=ast.parse(Path('dictation.py').read_text(encoding='utf-8-sig'))
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DictationApp')
writer=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_show_outcome_chip')
tree=ast.parse(Path('samsara/ui/home_signals.py').read_text(encoding='utf-8-sig'))
reader=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='recent_misses')
ns={'collections':collections,'time':time,'MISS_WINDOW':8}
exec(compile(ast.Module(body=[writer,reader],type_ignores=[]),'review-extracted-functions','exec'),ns)
app=types.SimpleNamespace()
for _ in range(3):ns['_show_outcome_chip'](app,'MISS','warning')
print('Production ring item types:',[type(x).__name__ for x in app._outcome_ring])
print('Production recent_misses after three MISS chips:',ns['recent_misses'](app))
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```

Actual output:
```text
Contrast ratios; alpha composited over named background:

TEXT_PRIMARY #e4e8ef BG0 15.718:1 | BG1 14.491:1 | BG2 13.214:1

TEXT_SECONDARY rgba(255,255,255,0.75) BG0 10.925:1 | BG1 10.332:1 | BG2 9.628:1

TEXT_DISABLED rgba(255,255,255,0.40) BG0 3.804:1 | BG1 3.817:1 | BG2 3.740:1

ACCENT #5cc4d4 BG0 9.476:1 | BG1 8.736:1 | BG2 7.966:1

RECORDING #c0392b BG0 3.552:1 | BG1 3.275:1 | BG2 2.986:1

SUCCESS #6ee7a0 BG0 12.511:1 | BG1 11.534:1 | BG2 10.517:1

WARNING #fbbf24 BG0 11.571:1 | BG1 10.668:1 | BG2 9.727:1

ERROR #f87171 BG0 6.983:1 | BG1 6.438:1 | BG2 5.871:1

AVA #a78bfa BG0 7.098:1 | BG1 6.544:1 | BG2 5.967:1

BORDER rgba(255,255,255,0.16) BG0 1.561:1 | BG1 1.630:1 | BG2 1.660:1

Dark text BG0 on ACCENT: 9.476:1

Production ring item types: ['tuple', 'tuple', 'tuple']

Production recent_misses after three MISS chips: (0, 3)
```

### E. Thread discipline, refreshed after another session edited Ava

```powershell
& 'F:\envs\sami\python.exe' -X utf8 -B tools/check_thread_discipline.py
```
Actual output, exit **1**:
```text
Thread discipline check FAILED — 3 violation(s):



  samsara/ava_edit/session.py:337

    threading.Thread(target=_run, name="ava-edit-propose", daemon=True).start()

  samsara/tts/coordinator.py:254

    t = threading.Timer(duration_ms / 1000.0, self._on_duck_restore)

  plugins/assets/sleep_overlay.py:76

    threading.Thread(target=watch_stdin, daemon=True).start()
```
An earlier run reported the same three sites, with the Ava line then at 333. The final line is 337. No checker fix was applied.

### F. Screenshot inventory and existing boot measurement

This command **reads** a prior boot report; it does not run a boot.
```powershell
@'
from pathlib import Path
from datetime import datetime
for p in sorted(Path(r'C:\Users\Morne\Documents\Claude\ui_proof').glob('*.png')):
 print(p.name,datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds'))
p=Path('perf_artifacts/boot_fullboot_final.md')
print('\n'+p.as_posix()+' | mtime '+datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds'))
print(p.read_text(encoding='utf-8-sig'))
for name in ['SAMSARA_MAP.md','SAMSARA_VISION.md']:
 p=Path(r'C:\Users\Morne\Documents\Claude')/name
 print(name+' | mtime '+datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds'))
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```
Actual output:
```text
first_run_wizard_page1.png 2026-09-13T16:15:35

first_run_wizard_page2_usecase.png 2026-09-13T16:15:36

first_run_wizard_page4_model.png 2026-09-13T16:15:36

first_run_wizard_page6_complete.png 2026-09-13T16:15:37

history_list_commands_filter.png 2026-07-08T02:04:44

history_list_default.png 2026-07-08T02:04:43

history_list_empty_state.png 2026-07-08T02:04:45

history_list_failed_filter.png 2026-07-08T18:46:01

history_list_row_selected.png 2026-07-08T02:04:44

icon_small_mark_proof.png 2026-07-18T19:09:40

icon_states.png 2026-09-13T17:28:48

main_window_history_tab.png 2026-07-08T18:46:03

mic_wizard_page1.png 2026-09-13T16:15:37

mic_wizard_page2_level.png 2026-09-13T16:15:38

modes_tab_after.png 2026-09-13T15:12:16

modes_tab_after_scrolled.png 2026-09-13T15:12:16

ouroboros_spin.png 2026-09-13T17:28:48

ouroboros_weights.png 2026-09-13T17:28:48

spin_legibility.png 2026-09-13T17:28:48

splash_spinner.png 2026-09-13T16:15:30

support_after.png 2026-09-13T12:25:21

support_tab_after.png 2026-09-13T12:37:33

taskbar_reality.png 2026-09-13T17:28:48

tutorial_step1.png 2026-09-13T16:15:33

tutorial_step2.png 2026-09-13T16:15:33

tutorial_step3_back_button.png 2026-09-13T16:15:34

tutorial_step4_done.png 2026-09-13T16:15:35

voice_training_after.png 2026-09-13T12:37:28



perf_artifacts/boot_fullboot_final.md | mtime 2026-09-14T22:18:17

# Full boot: final (3 runs, calibration cache on, overrides none, HEAD eeebf68)



Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).



| milestone | median s | min | max |

|---|---:|---:|---:|

| first_log | 0.296 | 0.281 | 0.303 |

| main_entry | 0.854 | 0.852 | 0.893 |

| splash_shown | 0.887 | 0.884 | 0.925 |

| init_entry | 0.887 | 0.887 | 0.928 |

| model_kickoff | 1.459 | 1.4 | 1.561 |

| config_watcher | 1.517 | 1.488 | 1.62 |

| shell_ready | 1.517 | 1.488 | 1.62 |

| tray_created | 1.535 | 1.514 | 1.639 |

| tts_ready | 2.16 | 2.093 | 2.231 |

| whisper_ready | 4.098 | 3.989 | 4.263 |

| silero_ready | 4.167 | 4.043 | 4.325 |

| ready_for_dictation | 4.168 | 4.043 | 4.325 |

| startup_complete | 4.173 | 4.049 | 4.332 |



| [BOOT] stage | median ms | min | max |

|---|---:|---:|---:|

| config load | 156 | 109 | 157 |

| audio device enumeration | 0 | 0 | 16 |

| mic calibration | 0 | 0 | 16 |

| sound setup | 16 | 15 | 31 |

| plugin discovery + command executor | 235 | 234 | 281 |

| history / SQLite init | 94 | 93 | 109 |

| TTS engine init (deferred) | 16 | 15 | 16 |

| smart actions init | 0 | 0 | 16 |

| keyboard/mouse listener setup | 16 | 0 | 16 |

| ACE audio engine start | 15 | 15 | 16 |

| model load kicked off (async) | 0 | 0 | 0 |

| shell ready (tray + main window scheduled) | 63 | 63 | 93 |

| tray icon created | 641 | 640 | 703 |

| async: Whisper model load (hotkey dictation ready) | 2641 | 2593 | 2703 |

| async: Silero VAD load | 63 | 47 | 78 |

| async: wake word + audio stream start | 16 | 0 | 16 |

| async: startup complete | 0 | 0 | 0 |



SAMSARA_MAP.md | mtime 2026-09-13T17:54:04

SAMSARA_VISION.md | mtime 2026-09-13T02:43:47
```

### G. Extraction and fixture history

```powershell
@'
import subprocess
for f in ['samsara/ui/settings_qt.py','samsara/boot.py','tests/command_catalog_known_collisions.txt']:
 print(f)
 print(subprocess.check_output(['git','log','-3','--format=%h %ad %s','--date=short','--',f],text=True))
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```
Actual output:
```text
samsara/ui/settings_qt.py

b17566e 2026-09-13 style(ui): one rounded scrollbar treatment app-wide, no arrow buttons

e47c316 2026-09-13 refactor(settings): one module per settings page behind the existing registry (no behaviour change)

b037725 2026-09-13 refactor(settings): settings_qt draws from theme tokens, not its own palette



samsara/boot.py

c5eb7a6 2026-09-13 refactor(boot): extract the boot sequence into samsara/boot.py (no behaviour change)



tests/command_catalog_known_collisions.txt

21d5cba 2026-09-13 feat(catalog): canonical command table generated from the live registry (no behaviour change)
```

### H. Branch before/after and not done

`git rev-parse --abbrev-ref HEAD` returned:
```text
feature/v0.22
```
`git rev-parse HEAD` returned:
```text
eeebf6874df1576cbfc13a0fa1a9553c0dfe8872
```
Both values matched the initial checks. A final physical-tree count replaced the initial snapshot rather than silently mixing them. Hash comparison found concurrent changes to Ava-edit files and streaming; the affected thread/coupling citations were reread. This report does not claim to review another session's whole in-progress Ava-edit feature.

**not done:** pytest collection and execution; coverage; live voice command reachability; actual microphone/wake readiness; current cold boot; native-crash reproduction; live Qt rendering, keyboard/screen-reader/UIA access checks; installer execution; actual updater download/swap/rollback; screenshot regeneration. Each was **not executed** under the task's import/write/live-app constraints or because it required a real application path. No source fixes, staging, commits or external work-log files were created. The sole review output is this uncommitted report.



### I. Report and working-tree verification

```powershell
@'
from pathlib import Path
import re,subprocess
p=Path('astra_review/SAMSARA_REVIEW_2026-09-15.md')
s=p.read_text(encoding='utf-8')
bt=chr(96)
refs=sorted(set(re.findall(bt+'([^'+bt+r'\n]+?\.(?:py|yml|spec|iss|md)):(\d+)',s)))
issues=[]
for name,line in refs:
 f=Path(name)
 if not f.is_file(): issues.append((name,line,'missing path'))
 elif int(line)>len(f.read_text(encoding='utf-8-sig').splitlines()): issues.append((name,line,'past end'))
print('Citation paths/line bounds checked:',len(refs))
print('Citation issues:',issues)
print('branch after:',subprocess.check_output(['git','rev-parse','--abbrev-ref','HEAD'],text=True).strip())
print('HEAD after:',subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())
print('Report git status:',subprocess.check_output(['git','status','--porcelain','--',str(p)],text=True).strip())
print('Report staged diff:',subprocess.check_output(['git','diff','--cached','--name-only','--',str(p)],text=True).strip() or '(none)')
print('Final line:',s.strip().splitlines()[-1])
'@ | & 'F:\envs\sami\python.exe' -X utf8 -B -
```
Actual output:
```text
Citation paths/line bounds checked: 124
Citation issues: []
branch after: feature/v0.22
HEAD after: eeebf6874df1576cbfc13a0fa1a9553c0dfe8872
Report git status: ?? astra_review/SAMSARA_REVIEW_2026-09-15.md
Report staged diff: (none)
Final line: PROMPT 98
```

This checks citation path existence and line bounds, not whether every cited line proves the associated judgment; the latter was assessed by reading the source.

## The three things I would do next, in order

1. **Make all command routes return the real execution result through one registry-entry API.** Start with the plugin ACTION no-op/success lie (F1), enforcing scope/pack eligibility there (F2) and isolating schedule cancellation (F7). Acceptance: the same canonical command is executable or explicitly refused through exact voice, model proposal and scheduled execution; replacing a schedule never revives its predecessor.
2. **Make lost listening and failed delivery recoverable without guessing.** Wire fatal capture state to Home (F6/F12), preserve a visible reason/action, and fix the outcome-ring schema (F11). Give that recovery area the infinity counter's space. Acceptance: a stopped consumer with a still-present microphone cannot be reported ready or disappear behind a beep.
3. **Exercise the actual release entry paths before distributing this beta.** Wire installer component mode before normal startup (F9), allow the installed beta version in updater comparison (F10), and replace “clean shutdown” confidence based on forced termination (F5). Acceptance: a frozen local-fixture installation/update/quit scenario runs through those real entry points.

## The three things in the direction documents I think are wrong or misprioritised

1. **VISION:14 — a wrong “nothing” is cheap, and content/context can supply addressing.** For someone whose only usable input is speech, silently doing nothing can remove control. The current shadow evidence cannot validate addressing (F3). Treat natural speech as the product goal, deterministic commands as the first execution path, and uncertain addressing as a separately measured decision with explicit user context.
2. **MAP:26 — a week of shadow logging is sufficient preparation for flipping the gate.** The implemented log does not contain the user's intended outcome and samples only accepted dictation. A calendar duration cannot substitute for labelled intent/error evidence. Fix the evaluation record before authorizing mode collapse.
3. **MAP:40 — one transient indicator should absorb every feedback surface.** Small chips cannot also be durable failure history, draft recovery and an accessible interactive explanation. Use the indicator for immediate state; keep persistent recovery on Home and task-specific controls where the work occurs. F6/F11/F12 make this more urgent than MAP:93's monolith line-count target.

PROMPT 98

