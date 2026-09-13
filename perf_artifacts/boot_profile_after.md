# Samsara boot after fixes 1, 2, 3, 4, 6, 8 -- 2026-09-12

Spec: `perf_artifacts/boot_profile.md` section 5. Fixes 5 (parallel model loads) and 7 (lazy
sounddevice/scipy) were deliberately NOT done.

## How this was measured

- `perf_artifacts/boot_run.py <code_dir> <label> [--wake-on] [--hold] [--home H]` boots one instance
  through `perf_artifacts/boot_run_child.py` (same steps as `dictation.py`'s `__main__`: instance lock,
  early UI scale, splash, `DictationApp(splash)`) with `SAMSARA_HOME_DIR` = a temp dir holding a copy
  of `~/.samsara/config.json`. The live Samsara kept running, and its profile was never written.
  The child installs no global key/mouse/CapsLock hooks or key macros, plays no earcons, and
  shows no main window (the tray icon still appears). It exits via `os._exit` after startup completes.
- **before** = `git archive HEAD` (7e6d5d5) plus the two files that were already uncommitted at
  session start (`samsara/constants.py`, `samsara/languages.py`, without which HEAD's `dictation.py`
  does not import). **after** = the working tree. Note the working tree also carries the parallel
  02a session's uncommitted edits (`command_registry.py`, `plugin_commands.py`, `ava_command_session.py`,
  execution-policy hunks in `dictation.py`).
- **cold-ish** = first boot of a fresh temp profile: no filter cache, no stored calibration,
  migrations pending. **warm** = the same profile booted again (mean of 2 runs). The OS page
  cache was warm for every run. A true cold boot (after reboot) cannot be produced without a
  reboot; see "Cold boot projection" below.
- Runs were interleaved before/after, 12 in total. Raw records: `perf_artifacts/boot_runs.jsonl`.
- All times are ms from the parent's `Popen()`:
  - **tray/window** = boot thread done (`[CONFIG] File watcher started`, the line right before
    `create_tray_icon()` schedules the tray and main window).
  - **hotkey ready** = max(ACE started, Whisper loaded, key listener).
  - **wake ready** = `[WAKE] wake_ready` (lazy), or the `[OWW] Loaded` line (old eager load).
  - **total** = `[INIT] Startup complete.`

## Before / after

Live config (`wake_word_enabled: false`):

| run | tray/window | hotkey dictation ready | wake ready | total ("Ready") |
|---|---:|---:|---:|---:|
| before, cold-ish | 4,496 | 6,080 | 6,789 (**loaded although off**) | 6,830 |
| after, cold-ish | 3,873 | 6,358 | off | 6,444 |
| before, warm | 4,479 | 5,781 | 6,494 (**loaded although off**) | 6,519 |
| after, warm | **1,370** | **3,798** | off | **3,887** |

Same config with `wake_word_enabled: true`:

| run | tray/window | hotkey dictation ready | wake ready | total ("Ready") |
|---|---:|---:|---:|---:|
| before, cold-ish | 4,452 | 5,976 | 6,750 | 7,084 |
| after, cold-ish | 4,054 | 6,662 | 7,533 | 6,747 |
| before, warm | 4,875 | 6,146 | 6,845 | 7,169 |
| after, warm | **1,243** | **3,580** | 5,137 | **3,672** |

Warm boot: tray/window **~3.2-3.6 s earlier**, hotkey dictation **~2.0-2.6 s earlier**, "Ready"
**~2.6-3.5 s earlier**. With wake on, "Ready" no longer waits for OpenWakeWord. Wake becomes
ready ~1.5 s after "Ready" on its own thread, still ~1.7 s earlier than before.

The first boot of a profile (cold-ish) gains less on hotkey readiness, 280-690 ms slower here,
and this is expected. That one boot designs the filter (1,051-1,114 ms, then cached forever)
and still records calibration (1.5 s, then reused for 24 h). Because ACE now starts before the
model thread, that one-time filter design pushes the model kick-off back by ~1 s. Every later
boot reads the filter in 4-7 ms.

## Per-fix evidence (after, warm, wake on; per-thread `[BOOT]` lines, verbatim)

```
   805 [BOOT] config load: 16ms  (total 16ms, thread=MainThread)
   822 [BOOT] mic calibration: 0ms  (total 47ms, thread=MainThread)       <- fix 4 ("cached": 1ms)
  1119 [BOOT] plugin discovery + command executor: 265ms  (total 344ms, thread=MainThread)
  1159 [BOOT] TTS engine init (deferred): 31ms  (total 375ms, thread=MainThread)  <- fix 3
  1187 [BOOT-DIAG] ACE engine.start() returned: 22ms                    <- fix 2 (was 1,318-1,442 warm, 13,371 cold)
  1191 [BOOT] model load kicked off (async): 0ms  (total 407ms, thread=MainThread)
  1577 [TTS] AudioCoordinator ready (tts_ready in 326ms, off the boot thread)
  3598 [BOOT] async: Whisper model load (hotkey dictation ready): 2406ms  (total 2813ms, thread=dictation.load)
  3668 [BOOT] async: Silero VAD load: 78ms  (total 2891ms, thread=dictation.load)   <- fix 8: real 78 ms
  3674 [WAKE] Loading wake word models on their own thread (listening starts when ready)
  3689 [BOOT] async: startup complete: 16ms  (total 2907ms, thread=dictation.load)
  5162 [WAKE] wake_ready: models loaded in 1487ms                        <- fix 1
```

| fix | what changed | proof from the runs |
|---|---|---|
| 1 OpenWakeWord lazy | `load_model_async` never loads OWW. `start_wake_word_mode()` calls `_request_wake_models()`, which spawns `dictation.wake_models_load`. The loader loads the primary and profile detectors, sets `wake_ready`, then starts listening if a start is still pending. Stop/disable during the load cancels it. A phrase change made during the load is applied. | wake off: no `[OWW]` line in any after run (every before run loads it). wake on: `Startup complete. (wake: loading)`, then `wake_ready` 0.8-1.5 s later, then `Wake word mode ACTIVE` (3 of 3 runs). |
| 2 ACE first + filter cache | `_start_ace_engine()` now runs before `load_model_async()`. `engine._get_polyphase_filter(up, down)` checks the module dict, then disk, then designs and persists. | `Polyphase filter 160/441: designed in 1051ms` (first boot), then `disk cache in 4-7ms`. `ACE engine.start()` 22 ms warm. |
| 3 TTS off boot thread | `_tts_init_worker` waits for `_startup_shell_ready`, builds the engine and coordinator, publishes the coordinator last, sets `tts_ready`, then runs the Ava migration announcement. The `tts_engine is None` guards are unchanged. | Boot-thread TTS step 16-31 ms (before: 969 ms in the smoke run, 1,630 ms cold in the live log). `tts_ready in 318-326ms` off-thread. |
| 4 calibration persisted | `<home>/mic_calibration.json` stores threshold, device id, device name, capture rate, multiplier and timestamp. The boot recording is skipped for the same device (by name, else by id) at the same rate and multiplier if it is < 24 h old. After `Startup complete` a background re-measure runs; it is discarded if a hold recording is live at either end. Manual recalibrate and mic switch always record. | `mic calibration (cached): 0-1ms` on every warm after run (before: 1,530-1,542 ms every run). The background refresh ran and was correctly discarded in the harness because the hold test was recording at that moment. |
| 6 migrations finish | `load_config` sets `_config_migration_save` around the three migrations, so their `save_config()` writes bypass the three-way merge. The merge treats "absent in memory, present on disk" as "take disk", which restored the deleted key. The flag is cleared in `finally`. | See the migration proof below. |
| 8 per-thread timers | `_BootStageTimer`: one last-mark per thread, `begin_thread()` at the top of the model worker, `thread=` on every line. | "Silero VAD load: 78ms" on `dictation.load` (the old timer reported 12,547 ms for the same step). |

### Migration proof (temp profiles, after the 3 boots of each series)

| profile | `wake_targets` | `command_mode_enabled` | `ai_command_mode` | `[MIGRATE]` lines per boot | config backups after 3 boots |
|---|---|---|---|---|---|
| before, wake off | still on disk | still on disk | still on disk | 3, 3, 3 | 9 |
| after, wake off | gone | gone | gone | 3, **0, 0** | 3 |
| before, wake on | still on disk | still on disk | still on disk | 3, 3, 3 | 9 |
| after, wake on | gone | gone | gone | 3, **0, 0** | 3 |

Also covered by `tests/test_config_migrations.py`. Its negative control, with the bypass
disabled, fails `test_first_load_migrates_and_the_keys_are_gone_on_disk` and
`test_second_load_runs_no_migrations`.

### Hold-to-dictate before wake is loaded (after runs)

The child drove the hold hotkey's own calls: `hotkey_pressed=True`, `start_recording(streaming=False)`,
1.5 s, then `stop_recording()`. The presence gate was forced open so ambient audio reached
Whisper, and text output and command dispatch were captured, not executed.

| run | wake state at press | recording | ACE consumer | Whisper decode |
|---|---|---|---|---|
| after, wake off, cold-ish / warm1 / warm2 | off / off / off | yes | yes | 2.6 s audio in 471 / 368 / 340 ms |
| after, wake on, cold-ish / warm1 / warm2 | **loading** / **loading** / **loading** | yes | yes | 2.6-2.7 s audio in 324 / 344 / 335 ms |

Whisper hallucinated on room noise ("Thanks for watching!") and the hotkey path filtered that to
empty text, so no output or command was produced.

## How readiness surfaces to the user

- **Wake loading**: tray tooltip / listening-indicator mode label read `Hold + Wake (loading)` (via
  `_get_mode_display`, refreshed by `_publish_wake_state` at load start and at ready). The tray menu
  item reads `Wake Word (jarvis)  - loading...`. The splash's final line reads "Dictation ready; wake
  word still loading". The existing `start` earcon plus `[LISTEN] Wake word mode ACTIVE` now fire
  only once the detectors exist, so the earcon means "listening", not "requested".
- **Wake off**: no label change; the models are never loaded.
- **TTS**: `app.tts_ready` (Event). Speakers keep their existing `audio_coordinator`/`tts_engine`
  None guards, so anything spoken in the first ~0.3 s after the tray appears is skipped silently,
  as it already was when TTS failed to initialize. No UI indicator was added.

## Cold boot projection (not measured)

The live-log cold boot (section 1 of the spec) had these costs, which this change removes or moves:
- the boot thread stalled 13.4 s in ACE while the model thread cold-loaded DLLs. ACE now starts
  before the model thread and reads the filter from disk, so no filter design competes for the GIL.
- 8.7 s of OWW sat on the "Ready" path, now off it (wake off: never loaded; wake on: after Ready).
- 1.6 s of TTS and 1.8 s of calibration sat on the boot thread.

The spec's expectation of boot thread ~3 s to tray and "Ready" ~12.5 s cold (gated by the
Whisper load) is consistent with the warm numbers above, but it was not verified: that needs a
reboot.

## Not done / caveats

- Fixes 5 and 7 (out of scope for this prompt).
- The background calibration refresh opens a second `InputStream` on the capture device while ACE
  runs, at the same rate. That is the same thing the existing manual "recalibrate" already does.
- `update_config` / external-edit phrase changes no longer build a `WakeWordDetector` before the
  first wake request (the loader reads the current phrase instead).
- Full suite: 14 failures, none in files this change touches. `test_ava_command_session_g2_matrix`,
  `test_plugin_commands`, and `test_command_packs` are in the parallel session's files.
  `test_typed_injection_routing` (3) is in an unmodified test whose target code is untouched.
  `test_quick_memo` passed on re-run. `tests/audio_engine/test_equivalence.py` records the live
  mic and failed 1 of 5 runs.
