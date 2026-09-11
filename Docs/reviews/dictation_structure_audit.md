# dictation.py -- structural audit

Report only. No code was changed. `dictation.py` = 12,344 lines, one file.

- Branch: `feature/v0.22` (unchanged; no checkout/switch/merge/rebase/stash/commit performed)
- Interpreter used for analysis: `F:\envs\sami\python.exe`
- Samsara was running throughout. **`dictation.py` was never imported.** Importing it opens a
  second `RotatingFileHandler` on the live `~/.samsara/logs/samsara.log` (line 1608) and writes a
  marker line to it (line 1692 -> 1672); a rollover from either process renames the file out from
  under the other. All analysis is AST-based plus text cross-reference. Import cost was measured
  with a generated shim that imports dictation.py's 65 module-level dependencies in the same order,
  without executing dictation.py itself.
- No test suite was run.

---

## 1. Map of the file

### 1.1 Totals

| Metric | Count |
| --- | --- |
| Total lines | 12,344 |
| Top-level classes | 3 |
| Top-level functions | 38 |
| `DictationApp` methods (direct members) | 253 |
| Nested closures inside `DictationApp` methods | 39 |
| All function/method definitions in file | 336 |
| Module-level statements that are not `def`/`class`/`import` | 90 |
| `thread_registry.spawn`/`timer` call sites | 36 |
| Distinct `self.*` attributes on `DictationApp` | 419 (208 ever written) |
| Distinct `self.config.get('...')` keys | 80 (211 read sites) |
| Lock attributes (`with self.<lock>`) | 15 |

`DictationApp` occupies lines 2051-12268 = 10,218 lines, i.e. **82.8% of the file is one class**.
The remaining 2,126 lines are the module prologue (1-2050) and the `__main__` block (12270-12344).

### 1.2 Top-level classes and functions

| Name | Kind | Lines | Length |
| --- | --- | --- | --- |
| `_NullStream` | class | 13-21 | 9 |
| `_get_default_render_id` | function | 72-130 | 59 |
| `_hide_console_now` | function | 134-143 | 10 |
| `_is_samsara_process` | function | 151-212 | 62 |
| `_steal_stale_lock_if_any` | function | 215-247 | 33 |
| `_check_single_instance` | function | 250-351 | 102 |
| `_acquire_instance_lock` | function | 362-366 | 5 |
| `_raw_key_pressed` | function | 466-494 | 29 |
| `_create_whisper_model` | function | 526-528 | 3 |
| `_is_pending_cancel_utterance` | function | 657-661 | 5 |
| `_speech_rms_coverage` | function | 837-858 | 22 |
| `_suspected_silent_data_loss` | function | 861-874 | 14 |
| `_apply_retry_on_suspected_loss` | function | 885-937 | 53 |
| `_get_pynput_command_key` | function | 940-982 | 43 |
| `_matches_pynput_key` | function | 985-996 | 12 |
| `_split_audio_at_silences` | function | 999-1070 | 72 |
| `_fade_edges` | function | 1073-1094 | 22 |
| `_dump_hotkey_buffer` | function | 1097-1119 | 23 |
| `resample_audio` | function | 1122-1134 | 13 |
| `_is_hallucinated_segments` | function | 1169-1258 | 90 |
| `_trim_trailing_garbage_run` | function | 1276-1285 | 10 |
| `_drop_trailing_garbage_segments` | function | 1288-1312 | 25 |
| `_is_quality_exhausted` | function | 1315-1358 | 44 |
| `_keep_low_confidence_long_chunk` | function | 1361-1385 | 25 |
| `_apply_segment_quality_gates` | function | 1388-1469 | 82 |
| `hide_console` | function | 1472-1482 | 11 |
| `open_file_or_folder` | function | 1485-1497 | 13 |
| `_SafeRotatingFileHandler` | class | 1534-1604 | 71 |
| `_verify_logging_self_check` | function | 1657-1686 | 30 |
| `print` | function | 1704-1718 | 15 |
| `_uncaught_exception_handler` | function | 1723-1729 | 7 |
| `_patched_thread_init` | function | 1736-1748 | 13 |
| `_is_repeat_blacklisted` | function | 1824-1829 | 6 |
| `_deep_merge` | function | 1832-1844 | 13 |
| `_three_way_merge` | function | 1850-1893 | 44 |
| `_resolve_target_window` | function | 1896-1952 | 57 |
| `_read_preview_diagnostics` | function | 1960-1966 | 7 |
| `_show_preview_failure` | function | 1969-1990 | 22 |
| `_monitor_preview_startup` | function | 1993-2018 | 26 |
| `_reap_old_preview_profiles` | function | 2021-2048 | 28 |
| `DictationApp` | class | 2051-12268 | 10,218 |

Nested classes: `_NullStream` has 4 methods (13-21); `_SafeRotatingFileHandler` has
`doRollover` and `_truncate_and_continue` (1534-1604).

`print` at line 1704 shadows the builtin for the whole module (see section 5).

### 1.3 Functional bands inside `DictationApp`

`DictationApp` is one class row above, which is useless as a map. Contiguous line bands:

| Lines | Length | Band |
| --- | --- | --- |
| 2059-2942 | 884 | `__init__` |
| 2944-3198 | 255 | ACE engine start/stop, stream-death recovery |
| 3200-4400 | 1,201 | config: load, migrate, quarantine, backup rotation, save, three-way merge, disk-watch apply |
| 4402-4650 | 249 | mic calibration, device enumeration, output-device switching |
| 4650-4950 | 301 | transcription-param construction, hotkey decode, `process_transcription` |
| 4951-5455 | 505 | foreground-app resolution, mic switching, model load |
| 5456-5878 | 423 | `on_key_press` / `on_key_release` / CapsLock streaming / mouse listener |
| 5879-6420 | 542 | command-mode + Ava-mode key state machine, session-mode manager |
| 6420-7130 | 711 | enter/exit command mode, Ava mode, Ava command session, Ava routing |
| 7131-7452 | 322 | command-mode utterance handling, streaming preview |
| 7452-7702 | 251 | continuous mode |
| 7703-8000 | 298 | hands-free ducking (idle duck + capture duck + wake-gate freeze) |
| 8001-8340 | 340 | wake-word mode start/stop, VAD/OWW model loading, wake profiles, wake session |
| 8341-8645 | 305 | VAD probability/gate helpers, ZCR fallback, wake trace, mic calibration |
| 8647-9133 | 487 | wake RMS gate + `process_wake_word_buffer` |
| 9135-9560 | 426 | wake command dispatch, dictation-mode state machine, finalize/failsafe timers |
| 9561-9830 | 270 | injection routing, clipboard paste, undo, correction dialog |
| 9831-9993 | 163 | `_output_dictation`, history logging |
| 9994-10402 | 409 | sound loading, playback stream, output-device watcher, duck/restore |
| 10403-11170 | 768 | `start_recording` / `_stop_recording_impl` (hold path) |
| 11172-11365 | 194 | `apply_mode`, gesture lane, tray mode switching |
| 11367-11546 | 180 | tray tooltip, `_schedule_ui`, icon painting/animation |
| 11547-11663 | 117 | `open_*` window/dialog openers |
| 11664-11772 | 109 | snooze / resume |
| 11773-11929 | 157 | AEC calibration, cheat sheet, tray construction, mic switch+refresh |
| 11930-12056 | 127 | first-run preview |
| 12058-12268 | 211 | `quit_app` |

### 1.4 Twenty longest functions/methods

| # | Function | Lines | Length |
| --- | --- | --- | --- |
| 1 | `DictationApp.__init__` | 2059-2942 | 884 |
| 2 | `DictationApp._stop_recording_impl` | 10572-11129 | 558 |
| 3 | `DictationApp.load_config` | 3200-3708 | 509 |
| 4 | `DictationApp._stop_recording_impl.transcribe` | 10672-11127 | 456 |
| 5 | `DictationApp.process_wake_word_buffer` | 8705-9133 | 429 |
| 6 | `DictationApp.quit_app` | 12058-12268 | 211 |
| 7 | `DictationApp._handle_command_mode_utterance` | 7131-7332 | 202 |
| 8 | `DictationApp.on_key_press` | 5456-5653 | 198 |
| 9 | `DictationApp._ensure_session_mode_manager` | 6077-6270 | 194 |
| 10 | `DictationApp.load_model_async` | 5132-5312 | 181 |
| 11 | `DictationApp.load_model_async.load` | 5136-5310 | 175 |
| 12 | `DictationApp.transcribe_continuous_buffer` | 7452-7594 | 143 |
| 13 | `DictationApp._load_sound_cache` | 10088-10230 | 143 |
| 14 | `DictationApp._check_command_mode_key` | 5879-6009 | 131 |
| 15 | `DictationApp.save_config` | 4065-4191 | 127 |
| 16 | `DictationApp.start_recording` | 10403-10525 | 123 |
| 17 | `DictationApp._output_dictation` | 9831-9948 | 118 |
| 18 | `_check_single_instance` | 250-351 | 102 |
| 19 | `DictationApp._migrate_wake_word_config` | 3833-3934 | 102 |
| 20 | `DictationApp._setup_sounds` | 9994-10086 | 93 |

Note #4 is nested inside #2: `_stop_recording_impl` is 558 lines of which 456 are the
`transcribe` closure it spawns as a thread at line 11129. #11 is nested inside #10 the same way.

Sum of the top 5 = 2,836 lines = 23% of the file.

---

## 2. Dead code

Method used: AST collection of every definition in `dictation.py`, then per-name reference count
via (a) AST `Name`/`Attribute` loads inside `dictation.py` itself, and (b) word-boundary regex over
every `.py`, `.json`, `.md`, `.bat`, `.txt`, `.js`, `.vbs`, `.spec`, `.cfg`, `.ini` file in the repo
excluding `.git/`, `build/`, `dist/`, `release/`, `release_staging/`, `__pycache__/`, `website/`,
`tmp_samsara_home/`, `perf_artifacts/`, `.pytest*`, and all `*.bak*` files. `plugins/`, `tools/` and
`tests/` were all in scope.

### 2.1 Never referenced anywhere -- 8

Confirmed by whole-repo grep: the only file containing the name is `dictation.py` itself, at the
definition.

| Definition | Line | Length | Note |
| --- | --- | --- | --- |
| `_NullStream.isatty` | 20-21 | 2 | class is instantiated (24, 26) but `isatty` is never called |
| `_hide_console_now` | 134-143 | 10 | exact duplicate of `hide_console` (see 3.5) |
| `hide_console` | 1472-1482 | 11 | third copy of the same body; `samsara/platform.py:92` has `hide_console_window()` |
| `DictationApp.revoke_tier2_approvals` | 4213-4217 | 5 | docstring claims "Called from Settings UI"; no such call exists in `samsara/ui/settings_qt.py` or anywhere else |
| `DictationApp.get_active_keys` | 5352-5363 | 12 | docstring says "legacy, kept for compatibility" |
| `DictationApp.get_pressed_keys_debug` | 5380-5389 | 10 | polls 30 VKs via `_raw_key_pressed` |
| `DictationApp.toggle_wake_word_mode` | 7596-7601 | 6 | reads `self.wake_word_active`, calls start/stop |
| `DictationApp.reload_sounds` | 10299-10302 | 4 | wrapper over `_load_sound_cache` |

Total: 60 lines.

### 2.2 Possibly dynamic -- 4

Not referenced from any `.py` file. Reached only through `samsara/handlers.py:346-350`, which does
`getattr(ctx.app, cmd.get('method'))()` on a name that comes from `commands.json`. These are live via
that string table, not dead:

| Method | Line | `commands.json` reference |
| --- | --- | --- |
| `DictationApp.repeat_last_command` | 11807 | `commands.json:2238`, `commands.json:2245` |
| `DictationApp.show_cheat_sheet` | 11883 | `commands.json:2208` |
| `DictationApp.hide_cheat_sheet` | 11887 | `commands.json:2214` |
| `DictationApp.open_log_file` | 12042 | `commands.json:2508` |

No definition in `dictation.py` is referenced only via a string literal *inside* `dictation.py`, and
no definition is referenced only via a `getattr`/`hasattr`/`setattr` string constant elsewhere in the
repo -- the 45 methods that are reached from other modules are all reached by literal attribute
access or by `getattr(app, '<literal>')` guarded by a matching `hasattr`, and all 45 also appear as
plain source text (so 2.1 is not hiding dynamic hits).

### 2.3 Module-level constants never read -- 1

| Constant | Line | Value | Evidence |
| --- | --- | --- | --- |
| `_WAKE_SESSION_TIMEOUT_S` | 699 | `10.0` | zero `Load` contexts in `dictation.py`; zero repo-wide hits outside the definition. Comment says "inactivity ends the open-ended wake session"; the actual timeout in use is read from config in `_start_wake_session` (8272) |

The two neighbours `_WAKE_SESSION_CHUNK_GAP_S` (700) and `_WAKE_SESSION_SEND_WORDS` (701) *are*
read, so this is one stale constant in an otherwise live block.

### 2.4 Config keys read whose value never influences a branch

Method: locate every `self.config.get('KEY', <default>)` whose result is a leaf value (default not
`{}`/`[]` and not itself the base of a further `.get`) -- 141 sites, 63 distinct keys. For each, trace
whether the value (or the local/attribute it is assigned to) reaches an `If`/`While` test,
`Compare`, `BoolOp`, `UnaryOp` or `IfExp`. 12 keys failed that test; each was then read manually.
**5 are genuinely inert** (value formatted into a log line or a telemetry record, never consumed);
the other 7 are false positives of the static rule -- the value is passed into a callee that does
branch on it.

Genuinely inert:

| Key | Read at | What happens to the value |
| --- | --- | --- |
| `wake_word_config.audio.speech_threshold` | 4424-4425 | only interpolated into `logger.debug` at 4426, inside the `mode != 'auto'` early-return branch of `_run_calibration_if_auto`. Nothing else in that branch uses `thresh`. |
| `wake_word_config.phrase` | 7995 | only interpolated into `logger.info` at 7996 |
| `wake_word_config.phrase` | 8112 | log line only, in `_warn_wake_fallback_once` |
| `compute_type` | 9896, 10726, 11004, 11096, 10834 | only ever assigned to the `compute_type=` field of a `diagnostics.DiagRecord`. Never compared, never gates anything, in-process or cross-module. |
| `model_size` | 9894, 11002, 11094, 10724 | same four `DiagRecord` constructions (`model_name=`). This key *is* branched on at 2834 for model loading, so only these four telemetry reads are inert. |

False positives of the static rule, verified live (listed so the number above is not mistaken for
"12"): `ava_mode_key` (5952 -> `_get_pynput_command_key`), `cal_multiplier` (4430 ->
`calibrate_threshold(multiplier=)`), `clipboard_delay` (9613 -> `paste_with_preservation(restore_delay=)`
at 9681), `command_matching_enabled` (2329 -> `self.command_matching_enabled`, branched on in
`samsara/commands.py:361`), `oww_threshold` (8131 -> `WakeWordDetector(threshold=)`),
`recording_tail_silence_ms` / `recording_tail_max_ms` / `recording_tail_speech_threshold`
(10622-10625 -> `_dictation_consumer.drain_after_release`), `paste_min_chars` (9623),
`show_all_audio_devices` (2238, 4459, 4580 -> `list_microphones`).

### 2.5 Counts

- Definitions never referenced anywhere: **8**
- Definitions reachable only through the `commands.json` string dispatch table: **4** (possibly dynamic, not counted as dead)
- Module-level constants never read: **1**
- Config keys read whose value never influences a branch: **5** (over 13 read sites)

---

## 3. Duplicated logic

10 pairs/sets confirmed by reading both sides. Ordered by size of the duplicated body.

### 3.1 Five decode preambles around seven `self.model.transcribe()` call sites

Every audio lane re-implements the same sequence -- concatenate buffer, resample, compute duration,
minimum-duration guard, build params, force `vad_filter`, take `model_lock`, join segment text --
with different constants and a different subset of the steps.

| Lane | Method | Concat+resample | Min duration | `vad_filter` | In-progress flag | Energy gate | `transcribe()` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Ava command | `_handle_ava_command_utterance` 6847 | 6884-6886 | `< 0.3` @6887 | forced `False` @6897 | 6879-6882 | none | 6899 |
| Command mode | `_handle_command_mode_utterance` 7131 | 7159-7161 | `< 0.3` @7163 | forced `False` @7213 | 7155-7157 | none | 7228 |
| Continuous | `transcribe_continuous_buffer` 7452 | 7462-7464 | `< 0.51` @7485 | forced `False` @7480 | **absent** | none | 7491 |
| Wake | `process_wake_word_buffer` 8705 | 8719-8721 | **absent** | `False` only if `not self._vad_available` @8779-8780 | 8758-8761 | RMS @8748-8749 | 8785 |
| Hold (hotkey) | `_stop_recording_impl.transcribe` 10672 | 10673-10674 | `< 0.51` @10683 | forced `False` @4806 (via `_build_hotkey_transcribe_params`) | via `_stop_in_flight` | Silero contiguity @10707 | 4852/4861 |
| Dictate commit | `_dictate_commit_redecode` 6272 | 6291 | none | forced `False` @6309 | none | none | 6314 |

Four different minimum-duration constants/absences for the identical hallucination hazard: `0.3`,
`0.3`, `0.51`, `0.51`, none, none.

### 3.2 The gate split -- three different rejection criteria for "is there speech here"

| Path | Gate | Criterion | Thresholds | Diagnostics on reject |
| --- | --- | --- | --- | --- |
| Hold / hotkey | `_buffer_has_contiguous_speech` called at 10707-10712 | longest *contiguous* Silero run >= `min_ms`, and only when `audio_duration <= _GATE_MAX_BUFFER_S` | `_GATE_MIN_CONTIG_MS`=150 (732), `_GATE_VAD_PROB`=0.45 (733), `_GATE_MAX_BUFFER_S`=8.0 (723), head grace `_GATE_HEAD_GRACE_CLICK_PAD_MS`=60 (736) | yes, `DiagRecord(outcome="gated")` 10723-10734 |
| Wake / hands-free | `_wake_audio_is_below_gate` 8647-8703, called at 8749 | single whole-buffer RMS vs an EMA noise floor; no contiguity requirement at all | `_NOISE_FLOOR_ALPHA`=0.05 (1766), `_NOISE_FLOOR_SPEECH_RATIO`=2.0 (1771), `_NOISE_FLOOR_MIN`=0.0005 (1775), `_SPEECH_FLOOR_RATIO`=1.5 (1781), `_ABS_FLOOR_MIN`=0.002 (1785) | none |
| Command mode / Ava / continuous | -- | no energy or presence gate whatsoever | -- | -- |

Same problem, two disjoint threshold families and one lane with no gate. Neither gate can see the
other's state: the hold path never updates `_wake_noise_floor`, the wake path never runs Silero
contiguity.

### 3.3 The gate split, second instance -- segment quality gates

`_apply_segment_quality_gates` (1388-1469) is the composed pipeline: `_is_hallucinated_segments`
whole-decode -> per-segment `_is_hallucinated_segments` + `_is_quality_exhausted` ->
`_keep_low_confidence_long_chunk` rescue.

- Hold path calls the composed function: 4865.
- `_dictate_commit_redecode` hand-rolls a subset at 6321-6327: `_drop_trailing_garbage_segments`
  then `_is_hallucinated_segments`. No `_is_quality_exhausted`, no `_keep_low_confidence_long_chunk`.
- `_handle_command_mode_utterance` hand-rolls the same subset at 7270-7279, with a comment at
  7241 acknowledging it is reproducing "`_apply_segment_quality_gates`' whole-decode check (step 1)".

Two hand-rolled partial reimplementations of an 82-line function that already exists.

### 3.4 Contiguous-run accumulator, implemented twice

`_buffer_has_contiguous_speech` 8467-8487 and `_zcr_energy_contiguous_speech` 8544-8556. Both
compute `frame_ms`, `min_contig_frames = max(1, int(min_ms / frame_ms))`, then loop accumulating
`contig` / `best_contig`, then `passed = best_contig >= min_contig_frames`, then emit a
near-identical `"[GATE] pass: max contiguous speech %dms (buffer %.1fs)"` debug line. ~20 lines
duplicated. Only difference: the Silero version honours `head_grace_ms` (8477-8478); the ZCR
version has no grace concept. Frame size differs by construction (512 samples @16k = 32.0ms at
8455 vs `int(src_rate * 0.032)` at 8508).

### 3.5 `hide_console` -- three copies, two of them dead

- `_hide_console_now` 134-143
- `hide_console` 1472-1482
- `samsara/platform.py:92` `hide_console_window()`

All three: `if not win32: return` / `import ctypes` / `GetConsoleWindow()` / `ShowWindow(hwnd, 0)` /
swallow. The two in `dictation.py` are never called (2.1).

### 3.6 Windowed RMS, computed four different ways

| Site | Window | Implementation | dB conversion |
| --- | --- | --- | --- |
| `_speech_rms_coverage` 850-857 | `_SANITY_RMS_WINDOW_S`=0.5s (827) | Python `for` loop, `math.sqrt(np.mean(chunk**2)) + 1e-12` | yes, `20*math.log10(rms) > floor_db` (856) |
| `_split_audio_at_silences` 1030-1039 | 100ms hard-coded (1030) | vectorised `frames.reshape` + `np.sqrt(np.mean(frames**2, axis=1))` | no, raw threshold |
| `_zcr_energy_contiguous_speech` 8508-8516 | `int(src_rate*0.032)` (8508) | same vectorised reshape idiom | no, adaptive 10th-percentile floor |
| `calibrate_wake_mic` 8622 | per chunk | `float(np.sqrt(np.mean(chunk**2)))` | no |
| `process_wake_word_buffer` 8748 | whole buffer | `float(np.sqrt(np.mean(audio.astype(np.float32)**2)))` | no |

Five sites, one shared idiom, four window sizes, one of the five in a slow Python loop, one of the
five in dBFS and four in linear amplitude.

### 3.7 Retry loop over a Windows config-file share violation, twice

| Site | Attempts | Sleep | Exceptions caught | Fallback |
| --- | --- | --- | --- | --- |
| `__init__` 2126-2141 | `range(3)` | `0.1` @2138 | `(OSError, PermissionError)` | skip wizard |
| `save_config` 4163-4174 | `range(3)` | `0.02` @4172 | `OSError` | non-atomic `open('w')` rewrite |

Same hazard (a concurrent config-watcher read holding `config.json` without `FILE_SHARE_DELETE`),
same 3-attempt shape, sleeps differ 5x, exception tuples differ.

### 3.8 Duck engage/release, two near-identical bodies

`_start_hands_free_idle_duck` 7748-7781 and `_open_hands_free_capture_duck` 7813-7894 share the
same eight steps: read `config['ducking']`, bail on `hands_free_enabled` false, read a level, bail
on `level >= 1.0`, take `_hands_free_duck_lock`, bail if an instance already exists, construct
`audio_ducking.SessionDucker(duck_level=, exclude_pids=)`, `start()`, publish under the lock,
`_bump_wake_gate_freeze()`, `_log_duck_result()`, stop the loser if publication lost the race.
Differences: level key (`hands_free_idle_level` default 0.8 @7757 vs `hands_free_level` default
0.15 @7822) and the capture variant's owner-token / generation bookkeeping. The capture variant is
82 lines to the idle variant's 34.

### 3.9 Hotkey config lookup block, duplicated in the press and release handlers

`on_key_press` 5482-5490 and `on_key_release` 5663-5670:

```
mode           = self.config.get('mode', 'hold')
main_hotkey    = self.config['hotkey']
cont_hotkey    = self.config.get('continuous_hotkey', 'ctrl+alt+d')
wake_hotkey    = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
command_hotkey = self.config.get('command_hotkey', 'ctrl+alt+c')
memo_hotkey    = self.config.get('memo_hotkey', 'ctrl+alt+m')
```

Identical keys and identical defaults in both. The press copy additionally reads `cancel_hotkey`
(5489); the release copy omits it. Two places to edit for any hotkey-default change.

### 3.10 Path resolution -- one frozen-aware site, six that are not

`__init__` 2205-2209 does it correctly:

```
sys._MEIPASS if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')
else os.path.dirname(os.path.abspath(__file__))
```

Six other sites resolve bundled resources with bare `Path(__file__).parent`, which does not point
into the PyInstaller extraction directory:

`commands.json` 2323, `command_palette.json` 2636, `sounds/` 2692, `samsara/wake_models/` 8130,
`self.sounds_dir` 9999, legacy `config.json` 12289.

`samsara_home_dir()` is used for user-writable paths at 1527, 3077, 3752, 8730 -- that half is
consistent.

### 3.11 Timestamp formatting -- four formats for the same purpose

| Format | Sites | Purpose |
| --- | --- | --- |
| `%Y%m%dT%H%M%S_%f` | 1107 | hotkey audio dump filename |
| `%H%M%S_%f` | 8732 | wake audio dump filename (same `debug_audio` directory as 1107) |
| `%Y%m%d-%H%M%S` | 3735, 3765 | config quarantine / config backup filename |
| `%Y-%m-%d %H:%M:%S` | 1753, 3825, 3828, 4953 | human-readable log/display |

The two debug-audio dumps write into the same directory (`samsara_home_dir()/"debug_audio"`, 1104
and 8730) with non-sortable-together filenames.

### 3.12 Clipboard save/restore -- consolidated, not duplicated

`_paste_preserving_clipboard` 9611-9702 is the single implementation; the actual save/restore
lives in `samsara/clipboard.paste_with_preservation` and is invoked once at 9677. Four callers
(6178, 7567, 9710, 9930/9932, 11037) all funnel through it. `import pyperclip` at line 501 has
**zero uses** in the file -- dead import, and it pulls a module at import time.

Counted duplicate pairs/sets: **10** (3.1 through 3.11, excluding 3.12 which is clean).

---

## 4. Hidden coupling on `DictationApp`

Method: classify every `self.<attr>` store and load by its innermost enclosing function, then mark
that function "thread-reachable" if it is a resolved `thread_registry.spawn`/`timer` target (36 call
sites; 34 resolved to a named callable, 2 are lambdas) or transitively called from one via
`self.<method>()`. 128 scopes are thread-reachable. Attributes are then flagged when a store and a
load land on opposite sides and not every access is inside a `with self.<lock>` block.

**95 attributes cross that boundary. 78 of them are mutated after `__init__`.** Full listing is
mechanical; below are the ones where the writer and reader are in different subsystems or different
threads with no visible contract.

### 4.1 The largest hidden contract: `samsara/audio_engine/wake_consumer.py`

`WakeConsumer` runs two daemon threads -- `"wake-consumer"` (`wake_consumer.py:148-149`) and
`"cmd-utt-queue"` (`wake_consumer.py:337-338`) -- plus a freshly spawned daemon per dispatched
utterance (`wake_consumer.py:920-924`). From those threads it reads and writes **36 attributes and
methods of `DictationApp` directly**, none of them declared as an interface anywhere:

`app._command_executed_at`, `app._dictation_finalize_lock`, `app._dictation_silence_timeout`,
`app._expire_wake_session`, `app._handle_ava_command_utterance`, `app._handle_command_mode_utterance`,
`app._hotkey_recording`, `app._open_hands_free_capture_duck`, `app._close_hands_free_capture_duck`,
`app._oww_wake_detected`, `app._pending_transcriptions`, `app._process_wake_word_buffer_tracked`,
`app._restart_wake_session_timer`, `app._tts_last_speaking`, `app._vad_available`,
`app._vad_consec_errors`, `app._vad_error_last_log`, `app._vad_is_speech`, `app._vad_lock`,
`app._vad_model`, `app._vad_reset`, `app._wake_detector`, `app._wake_rearm_needs_onset`,
`app._wake_session_started_at`, `app.app_state`, `app.command_mode_active`, `app.config`,
`app.echo_canceller`, `app.exit_ava_command_session`, `app.exit_command_mode`, `app.is_speaking`,
`app.play_sound`, `app.process_wake_word_buffer`, `app.silence_start`, `app.speech_buffer`,
`app.wake_word_active`, `app.wake_word_triggered`.

`samsara/streaming.py` adds a second such surface (18 names: `app.model`, `app.model_lock`,
`app.model_rate`, `app.recording`, `app.stop_recording`, `app._paste_preserving_clipboard`,
`app._log_history`, ...). `samsara/audio_engine/continuous_consumer.py` adds three
(`app.config`, `app.continuous_active`, `app.transcribe_continuous_buffer`).

Consequence: the thread classification in 4.2 is a *lower bound*. Methods such as
`_handle_command_mode_utterance` (7131) and `_handle_ava_command_utterance` (6847) show up as
"main" below because `dictation.py` never spawns them -- in production they run on WakeConsumer
threads.

### 4.2 Attributes written in one scope and read in another thread, unguarded

| Attribute | Writer(s) | Reader(s) in a different thread | Lock |
| --- | --- | --- | --- |
| `self.hotkey_pressed` | `on_key_press` 5496, 5513, 5522, 5530, 5541 (+13 more); `load_model_async.load` **5272** | `_stop_recording_impl` 10598, 10629 (spawned `transcribe` thread's parent), `on_key_release` 5681 | none |
| `self._wake_transcription_in_progress` | `_handle_ava_command_utterance` 6882/6937, `_handle_command_mode_utterance` 7157/7331 (WakeConsumer threads), `process_wake_word_buffer` 8761/9130 | cross-read at 6879, 7155, 8758 | none. Three lanes use one non-atomic test-then-set as a mutual-exclusion flag |
| `self._command_mode_miss_count` | `_ensure_session_mode_manager._command_dispatch_fn` 6118, 6128; `_stop_recording_impl.transcribe` **10873, 10886** | 6131 (dispatch closure), 10890 (transcribe thread) | none. Read-modify-write across two threads |
| `self.app_state` | `_try_cancel_pending_wake_command` 6965, `_start_wake_session` 8283, `_process_wake_command` 9139/9196, `_start_dictation_mode` 9210, `_reset_wake_dictation` 9321 -- all unlocked | `_expire_wake_session` 8342, `_restart_wake_session_timer` 8322, `process_wake_word_buffer` 8715/8876, and 9416/9530 **under `_dictation_finalize_lock`** | partial: the timer-thread readers hold `_dictation_finalize_lock`, none of the writers do |
| `self.wake_dictation_buffer` | `_start_wake_session` 8286, `_start_dictation_mode` 9213, `_reset_wake_dictation` 9324 -- all unlocked | `process_wake_word_buffer` 8953, 9008, 9011 unlocked; `_maybe_finalize_dictation` 9501, `_finalize_dictation_timeout` 9390, `_absolute_failsafe_reset` 9533/9537, `reset_wake_word` 9975 **under `_dictation_finalize_lock`** | partial. A mutable list read both ways |
| `self._dictation_finalize_timer` | `_start_dictation_mode` 9223, `_reset_wake_dictation` 9337, `_restart_dictation_timer` 9382 | 9221, 9335, 9378 -- cancel-then-replace across the hardcap timer thread, failsafe timer thread and WakeConsumer | none |
| `self._stop_in_flight` | `on_key_release` 5690, 5700, 5710; `on_key_release._deferred_stop` **5686** (spawned thread) | `on_key_press` 5509, 5633; `start_recording` 10427 | none |
| `self._dictate_preview` | `_ensure_streaming_preview` 7673, 7677; `_release_streaming_preview` 7682 | `_commit_pending_hands_free_dictation` 5422, 5442 (spawned at 5566); `_handle_command_mode_utterance` 7300, 7312 (WakeConsumer thread) | none |
| `self._wake_noise_floor` | `calibrate_wake_mic` 8636 (spawned from `mic_setup_wizard_qt`); `_wake_audio_is_below_gate` 8678, 8683 (WakeConsumer thread) | 8677, 8681, 8684, 8689, 8693 | none. EMA read-modify-write from the wizard thread and the consumer thread |
| `self.capture_rate` | `switch_microphone` 5102, `update_config` 4247, `_apply_disk_config` 4363 | `process_wake_word_buffer` 8712, `_run_calibration_if_auto` 4432, `calibrate_echo_cancellation._run` 11785, `_vad_is_speech` 8396, `transcribe_continuous_buffer` 7460 -- i.e. read as the resample source rate on every consumer thread | none. Changing mics mid-decode changes the rate an in-flight resample is using |
| `self._sound_stream` | `_setup_sounds` 10078, `_start_sound_stream` 10254/10279, `stop_sound_stream` 10355 | `stop_sound_stream` 10349-10352, `_start_sound_stream` 10262 -- plus the PortAudio callback thread `_sound_stream_callback` 10281 | none (the *buffer* is locked, the stream handle is not) |
| `self._snooze_timer` | `snooze_listening` 11706, `quit_app` 12148, `_on_snooze_expire` **11731** (timer thread), `resume_listening` 11742 | `quit_app` 12146-12147, `resume_listening` 11740-11741 | none |
| `self._icon_chase_counter`, `_icon_chase_offset`, `_icon_rotation` | `_icon_chase_tick` 11528, 11530 (self-rescheduling timer thread, 11543) | 11529 and the paint path | none |
| `self._last_command` / `_last_command_name` | `_route_to_ava` 6993, `_process_wake_command` 9169, `_stop_recording_impl.transcribe` 10858 (three different threads), `_command_dispatch_fn` 6103, `transcribe_continuous_buffer` 7514 | `_dispatch_command` 11805, `repeat_last_command` 11812 (the "repeat last command" voice command) | none |
| `self._gesture_loop` | `_start_gesture_lane` 11254, 11259; `_stop_gesture_lane` 11269 | `set_gesture_enabled` 11284, 11287; `_start_gesture_lane` 11241 | none |
| `self.sound_files` | `_setup_sounds` 10003 | `_load_sound_cache` 10112 (spawned via `_setup_sounds` 10084 output-device watcher chain) | none |
| `self.config` | `save_config` 4181, `load_config` 3626/3640/3689/3698, `_apply_disk_config` 4343 | 270 read sites across every thread in the file | partial: writes in `save_config` are under `_config_lock` (asserted at 4098), the 270 reads are almost all bare |

`self.config` is the single most-shared object: 270 reads, 6 writes, one of which
(`_apply_disk_config` 4343, driven by the on-disk config watcher `_on_config_file_changed` 4317)
replaces the dict wholesale while consumer threads are reading it.

### 4.3 Lock inventory

15 lock attributes exist: `_ava_cmd_mode_lock`, `_ava_cmd_timer_lock`, `_ava_mode_lock`,
`_ava_session_dispatch_lock`, `_buffer_lock`, `_capslock_lifecycle_lock`, `_command_mode_lock`,
`_command_mode_timer_lock`, `_config_lock`, `_dictation_finalize_lock`, `_hands_free_duck_lock`,
`_vad_lock`, `_wake_consumer_lock`, `buffer_lock`, `model_lock`.

Two of them are near-homonyms with different scopes -- `self.buffer_lock` guards
`self.speech_buffer`, `self._buffer_lock` guards `self._playback_buffer` (sound output). Nothing in
the names distinguishes them.

Only 17 of the 95 cross-thread attributes have every access inside a lock. `_playback_buffer`
(10253, 10285-10292, 10344) is the one fully-guarded example.

---

## 5. Import-time work

90 module-level statements are neither `def`, `class`, nor `import`. Executing
`import dictation` runs all of them, plus 65 module-level dependency imports.

### 5.1 Measured cost

Import cost of the 65 module-level dependencies, in dictation.py's own order, via a generated shim
(dictation.py itself not imported):

- Warm: **724-773 ms** (3 runs)
- First run after cache miss: **1,384 ms**

Cumulative `-X importtime`, top contributors (microseconds):

| Module | Cumulative | dictation.py line |
| --- | --- | --- |
| `sounddevice` (PortAudio init) | 137,955 | 387 |
| `samsara.smart_corrections` (pulls `requests` 136,843) | 137,817 | 576 |
| `pyautogui` (pulls `pyscreeze` 63,030) | 79,613 | 502 |
| `samsara.ui.tray_qt` (pulls `PySide6.QtCore` 45,729) | 71,830 | 537 |
| `numpy` | 58,561 | 385 |
| `samsara.torch_guard` | 47,688 | 384 |
| `samsara.ui.ava_guide_qt` | 31,081 | 570 |

The file's own instrumentation expects worse: lines 392, 430 and the `__main__` diagnostics branch
at 12281 all test for `> 5000 ms`, and the comment at 407-419 records a measured 11.4 s for the
`samsara.audio_engine` import on a cold/frozen start (that import measured 9 ms warm from source
here). The multi-second import is a cold-cache and frozen-build phenomenon; the warm floor is
~0.75 s of dependency loading before a single line of dictation.py's own module body runs.

### 5.2 Module-level work by category

**Processes that exit or divert during import**

| Line | What |
| --- | --- |
| 36-42 | `if os.environ.get("SAMSARA_DUCKING_HOST") == "1":` imports `samsara.ducking_host`, reconfigures stdin/stdout, and calls `sys.exit(_ducking_host_main())`. `import dictation` in that environment never returns. |
| 509-524 | Loads `msvcp140.dll`; on `OSError` constructs a **`QApplication`** and a modal **`QMessageBox.critical`**, then `sys.exit(1)`. GUI construction and process exit at import time. |

**Process-global mutations (visible to every other module in the interpreter)**

| Line | What |
| --- | --- |
| 23-26 | replaces `sys.stdout` / `sys.stderr` with `_NullStream` when they are `None` |
| 51-64 | `SetCurrentProcessExplicitAppUserModelID("MorneIngstar.Samsara")` -- process-wide Win32 taskbar identity |
| 369 | `os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"` |
| 384 | `samsara.torch_guard.install()` -- mutates the import machinery |
| 550-557 | sets 14 third-party loggers to `WARNING` |
| 1504-1513 | `sys.stdout.reconfigure(encoding="utf-8")` and the same for stderr |
| 1643-1651 | clears tagged handlers off the **root logger**, sets root level to `DEBUG`, attaches two handlers. Any importer's logging configuration is replaced. |
| 1695-1700 | six more third-party loggers to `WARNING` |
| 1703-1718 | `_original_print = print`; then `def print(...)` at 1704 shadows the builtin for the entire module |
| 1731 | `sys.excepthook = _uncaught_exception_handler` |
| 1734-1750 | `threading.Thread.__init__ = _patched_thread_init` -- **every thread created anywhere in the process afterwards goes through dictation.py's wrapper** |

**Filesystem and console I/O**

| Line | What |
| --- | --- |
| 387 | `import sounddevice` -> PortAudio initialisation, audio device enumeration |
| 390-391, 428-429, 503-505, 531-533, 1515-1517 | 7 `sys.stdout.write` + `flush` pairs of `[BOOT-DIAG]`/`[PRE-LOG]` timing lines |
| 1527-1528 | `LOG_DIR = samsara_home_dir()/"logs"`; **`LOG_DIR.mkdir(parents=True, exist_ok=True)`** -- directory creation |
| 1608-1613 | opens `~/.samsara/logs/samsara.log` in a `_SafeRotatingFileHandler` (`maxBytes=5 MB`, `backupCount=3`) |
| 1621-1623 | `open(sys.stdout.fileno(), mode='w', ...)` -- second handle on the console fd |
| 1692 -> 1672 | `LOGGING_SELF_CHECK_OK = _verify_logging_self_check()` -- **stats the log file, writes a marker line through the logger, flushes, re-stats** |
| 1752-1754 | three more `logger.info` lines into the log file |

**Config reads at module level:** none. `samsara.paths` is imported at 1520-1524 and
`samsara_home_dir()` is called at 1527, but no config file is read until
`DictationApp.__init__` -> `load_config`. The only config-adjacent import-time read is the
`SAMSARA_DUCKING_HOST` environment variable at 36.

**Threads started at module level:** none directly. `_patched_thread_init` (1736) is installed but
not invoked, and the 36 `thread_registry` sites are all inside methods. Note however that
`import samsara.smart_corrections` (576) documents at its own lines 11-16 that it deliberately
avoids importing `plugins.commands.ask_ollama` precisely because *that* module starts a
health-monitor thread at import -- so the no-threads-at-import property is one transitive import
away from breaking.

**Models loaded at module level:** none. `faster_whisper` is deferred behind
`_create_whisper_model` (526-528); the Silero VAD and OpenWakeWord models load in `_load_vad_model`
(8041) and `_load_oww_model` (8083).

### 5.3 Why a guard at line 383 is load-bearing

Line 383 is `from datetime import datetime`; line 384 is
`import samsara.torch_guard; samsara.torch_guard.install()`. Anything inserted at 383 executes:

- **before** `torch_guard.install()` (384), `numpy` (385), `sounddevice` (387), the
  `samsara.audio_engine` block (407-432), `pynput` (434-435), and every `samsara.*` import (573-622);
- **before all logging exists.** `LOG_DIR` and the file handler are created at 1527-1613 and
  `logger` is not bound until 1654. At line 383 the only output channels are `sys.stdout.write`
  and the still-builtin `print`. A guard that logs, or that raises, produces **no log record at all**
  -- the failure is invisible in `samsara.log`;
- **before `sys.excepthook`** is replaced (1731), so an exception there bypasses
  `_uncaught_exception_handler`;
- **before the single-instance lock.** `_check_single_instance` is only called from
  `_acquire_instance_lock` (362) which is only called from `__main__` at 12278 -- deliberately, per
  the comment at 353-358 (acquiring at import time made every `import dictation` in pytest call
  `sys.exit(0)`). A guard at 383 therefore runs in *every* process that imports the module,
  including test collection and the ducking-host child;
- **after** the ducking-host divert (36-42) and the AUMID call (51-64), but **before** the
  `msvcp140.dll` check (509) -- so it precedes the only existing import-time error dialog;
- `datetime` is bound at 383 and first used at 1107, 1753, 3735 etc. A guard placed *above* the
  import rather than below it breaks those; placed below, it still precedes `numpy`, so it cannot
  use `np`.

The practical constraint: the only safe place for a boot guard that needs to log is after 1692
(logging verified) and before 2059 (`DictationApp.__init__`); the only safe place for a guard that
must run before heavy imports is 36-44, where it has neither logging nor numpy and must write to
`sys.stdout` directly.

---

## 6. Ranked "extract first" list

Ranked by cross-reference cost, measured per candidate region as: distinct `self.*` attributes read
and written, outbound `self.<method>()` calls to methods left behind, module-level globals used, and
inbound references from the rest of the file to names defined inside.

### 1. Decode-quality gate functions -- lines 655-1470, 816 lines

Module-level functions only: `_is_pending_cancel_utterance`, `_speech_rms_coverage`,
`_suspected_silent_data_loss`, `_apply_retry_on_suspected_loss`, `_get_pynput_command_key`,
`_matches_pynput_key`, `_split_audio_at_silences`, `_fade_edges`, `_dump_hotkey_buffer`,
`resample_audio`, `_is_hallucinated_segments`, `_trim_trailing_garbage_run`,
`_drop_trailing_garbage_segments`, `_is_quality_exhausted`, `_keep_low_confidence_long_chunk`,
`_apply_segment_quality_gates`, plus the ~25 tuning constants at 668-1166 and
`_HotkeyDecodeResult` (880).

- `self.*` attributes needed: **0** (nothing in the region touches `self`)
- outbound method calls: **0**
- module globals needed: `logger`, `_PENDING_CANCEL_UTTERANCES` (653, moves with the region)
- inbound: 52 references over 28 names -- becomes one `from samsara.decode_gates import ...`
- Pass in: nothing. These are pure functions of `(audio, sample_rate, segments, params)`.

Largest single extraction available and the only one with a zero-attribute interface.

### 2. Single-instance, console and preview-profile helpers -- lines 72-351 + 1472-1497 + 1955-2048, 400 lines

`_get_default_render_id`, `_hide_console_now`, `_is_samsara_process`, `_steal_stale_lock_if_any`,
`_check_single_instance`, `_acquire_instance_lock`, `hide_console`, `open_file_or_folder`,
`_read_preview_diagnostics`, `_show_preview_failure`, `_monitor_preview_startup`,
`_reap_old_preview_profiles`, and the `_PREVIEW_*` constants (1955-1957).

- `self.*` attributes needed: **0**
- outbound method calls: **0**
- module globals needed: `logger`
- inbound: 11 references over 7 names (`open_file_or_folder` x3, `_get_default_render_id` x2,
  `_show_preview_failure` x2, and one each for `_check_single_instance`, `_reap_old_preview_profiles`,
  `_PREVIEW_DIAGNOSTIC_NAME`, `_monitor_preview_startup`)
- Pass in: nothing. `_get_default_render_id` is called from the sound subsystem (candidate 4) with
  no arguments from `self`.
- Bonus: 2 of the 8 dead functions (2.1) live here and would be deleted rather than moved, and
  `hide_console`'s third copy already exists at `samsara/platform.py:92`.

### 3. Window/dialog openers -- lines 11547-11663, 117 lines

13 methods: `open_settings`, `open_voice_training`, `open_mic_setup_guide`, `open_ava_guide`,
`open_history`, `open_dictation_diagnostics`, `open_quick_reference`, `open_benchmark_review`,
`open_log_viewer`, `open_stress_test_wizard`, `open_wake_word_debug`, plus two helpers.

- outbound method calls: **1** (`_schedule_ui`)
- inbound: **2** (`open_correction_capture` 1, `show_tutorial` 1)
- module globals needed: `logger`
- Pass in: `_schedule_ui`, and 12 attribute slots -- but **7 of the 12 are created and owned
  entirely inside the region** (`_settings_qt`, `_history_qt`, `_diagnostics_qt`,
  `_quick_reference_qt`, `_benchmark_review_qt`, `_log_viewer_qt`, `_stress_wizard_qt`) and would
  become state of the extracted object. Only 5 are genuinely inbound:
  `voice_training_window`, `mic_setup_wizard`, `ava_guide`, `wake_word_debug_window`, `history_store`.

Lowest inbound coupling of any region measured. Cheapest win per line.

### 4. Sound output subsystem -- lines 9994-10402, 409 lines

11 methods: `_setup_sounds`, `_load_sound_cache`, `reload_sounds`, `_start_sound_stream`,
`_sound_stream_callback`, `play_sound`, `stop_sound_stream`, `_watch_output_device`,
`_on_output_device_changed`, `_duck_audio`, `_restore_audio`.

- outbound method calls: **0**
- module globals needed: `logger`, `_get_default_render_id` (candidate 2 -- extract that first)
- inbound: 76 references over 6 names, but **68 of the 76 are `play_sound`** -- one method. The
  other five are `stop_sound_stream` (2), `_duck_audio` (2), `_restore_audio` (2), `_setup_sounds`
  (1), `_start_sound_stream` (1).
- Pass in: `config` (read), `audio_coordinator`, `sounds_dir`. **8 of the 11 attributes are
  region-private** and become state of the extracted object: `_sound_cache`, `_sound_stream`,
  `_sound_stream_sr`, `_playback_buffer`, `_buffer_lock`, `_warned_sound_misses`, `sound_files`,
  `_output_watcher_stop`. Two attributes leak outward and would need to stay or be published back:
  `output_device`, `output_device_name` (also written by `switch_output_device` 4584).
- Also fixes 4.2's `_sound_stream` / `sound_files` cross-thread reads by making them private to one
  object.

### 5. VAD / contiguous-speech gates -- lines 8371-8557, 187 lines

`_vad_probabilities`, `_vad_is_speech`, `_vad_reset`, `_buffer_has_contiguous_speech`,
`_zcr_energy_contiguous_speech`.

- outbound method calls: **0**
- `self.*` attributes read: **4** -- `_vad_model`, `_vad_lock`, `_vad_available`, `capture_rate`
- `self.*` attributes written: **0**
- module globals needed: `logger`, `resample_audio` (candidate 1), `_GATE_MIN_CONTIG_MS` (732),
  `_GATE_VAD_PROB` (733)
- inbound: **5** references over 2 names (`_vad_reset` 3, `_buffer_has_contiguous_speech` 2) from
  inside `dictation.py`, plus 4 external (`wake_consumer.py:665` `app._vad_is_speech`,
  `wake_consumer.py` x5 `app._vad_reset`, `tests/test_onnx_vad.py:15,77`) -- so the extracted
  object must be reachable as an attribute for `WakeConsumer` to keep working, or `wake_consumer.py`
  must be updated in the same change.
- Pass in: `(model, lock, available_flag, default_rate)` -- one constructor call. Extending the
  region to include `_emit_wake_trace` (8574) and `calibrate_wake_mic` (8585) raises inbound from 5
  to 28 (23 of them `_emit_wake_trace`), so stop at 8557.

### Rejected candidates, for the record

- **Config load/save/migrate (3200-4400, 1,201 lines)**: only 9 attributes read and 23 inbound
  references, but 5 outbound method calls into unrelated subsystems (`_detect_capture_rate`,
  `apply_mode`, `exit_ava_command_session`, `set_gesture_enabled`, `set_wake_word_enabled`) --
  `load_config` re-enters mode and wake-word machinery, so extraction requires 5 injected callbacks.
- **Hands-free ducking (7703-8000, 298 lines)**: inbound is only 7, but it needs 14 attributes read
  and 11 written, including `is_speaking` and `silence_start` which belong to the wake state
  machine, and it is entangled with `_wake_gate_freeze_until` which `_wake_audio_is_below_gate`
  (8681) depends on.
- **Snooze (11664-11772, 109 lines)**: inbound **0**, the best score in the file, but 8 outbound
  calls (`start_continuous_mode`, `stop_continuous_mode`, `start_wake_word_mode`,
  `stop_wake_word_mode`, `stop_recording`, `play_sound`, `_schedule_ui`, `_get_mode_display`) --
  it is a thin orchestrator over the very subsystems it would leave behind.
- **Icon painting (11408-11497 + 11895-11921, 117 lines)**: inbound 9, outbound 2, but it needs
  `tray_icon` read/write and the `_WHEEL_*` palette constants, and `_schedule_ui` (11388) sits in
  the middle of the band and is referenced 61 times from the whole file, so the band cannot be cut
  contiguously.

---

## REPORT

- [x] **branch before/after** -- before: `feature/v0.22`; after: `feature/v0.22`. No checkout,
  switch, merge, rebase, stash or commit. Only file written under the repo:
  `docs/reviews/dictation_structure_audit.md`.
- [x] **longest five functions** -- `DictationApp.__init__` 2059-2942 (884);
  `DictationApp._stop_recording_impl` 10572-11129 (558); `DictationApp.load_config` 3200-3708 (509);
  `DictationApp._stop_recording_impl.transcribe` 10672-11127 (456);
  `DictationApp.process_wake_word_buffer` 8705-9133 (429).
- [x] **dead code count** -- 8 definitions never referenced anywhere + 1 module constant never read
  + 5 config keys whose value never influences a branch. Separately: 4 definitions reachable only
  through the `commands.json` string dispatch table (possibly dynamic, not counted as dead).
- [x] **duplicate pairs count** -- 10.
- [x] **extract first list** -- 1) decode-quality gates 655-1470 (816 lines, 0 attributes);
  2) single-instance/console/preview 72-351+1472-1497+1955-2048 (400 lines, 0 attributes);
  3) `open_*` window openers 11547-11663 (117 lines, `_schedule_ui` + 5 window handles);
  4) sound output 9994-10402 (409 lines, `config`/`audio_coordinator`/`sounds_dir`);
  5) VAD gates 8371-8557 (187 lines, `_vad_model`/`_vad_lock`/`_vad_available`/`capture_rate`).
- [x] **not done** --
  - `dictation.py` was **not imported**; import cost is measured via a 65-dependency shim in
    dictation.py's own order, not by timing `import dictation`. The module body's own work
    (logging setup, `LOG_DIR.mkdir`, the self-check write, the `threading.Thread` patch) is
    enumerated but not timed. Reason: a second `RotatingFileHandler` on the live `samsara.log` while
    the app is running.
  - No tests were run (no suite, per task).
  - Thread attribution is a **lower bound**. It resolves 34 of 36 `thread_registry` call sites in
    `dictation.py` (2 are lambdas: 8334, and the `_restart_wake_session_timer` target) and does not
    model the `"wake-consumer"` / `"cmd-utt-queue"` / per-utterance daemon threads in
    `samsara/audio_engine/wake_consumer.py`, the `samsara/streaming.py` threads, or Qt's main
    thread. Methods called only from those threads (for example
    `_handle_command_mode_utterance` 7131, `_handle_ava_command_utterance` 6847,
    `_wake_audio_is_below_gate` 8647) are labelled "main" in section 4.2 although they do not run
    on the main thread. Section 4.1 documents that surface separately.
  - Section 4.2 lists 17 of the 95 flagged cross-thread attributes -- the ones where writer and
    reader are in different subsystems. The other 78 were classified mechanically and not read
    individually.
  - Dead-code cross-reference excludes `build/`, `dist/`, `release/`, `release_staging/`,
    `website/`, `__pycache__/`, `tmp_samsara_home/`, `perf_artifacts/`, `.pytest*`, and all
    `*.bak*` files (including `dictation.py.bak`, `dictation.py.bak-2026-06-02`,
    `dictation.py.bak-2026-04-15-trayswitch`). A name used only in one of those would be reported
    as dead here. 10 `.pytest_tmp_*` directories were unreadable (permission denied) and were
    skipped.
  - Config-key branch analysis covers `self.config.get('literal')` only. Keys read via
    `self.config['x']`, via a subscript, via a variable key, or from a sub-dict held in a local
    (for example `audio_config.get('adaptive_gate', True)` at 8663) are outside the 141 leaf sites
    and were not classified.
  - Section 3 pairs were found by targeted search over the categories named in the task
    (resampling, RMS/dBFS, VAD framing, timestamp formatting, path resolution, clipboard, retry
    loops) plus the decode preambles and gate splits. It is not an exhaustive clone-detection pass;
    no token-level clone detector was run.
  - Section 6 measures cross-reference cost for the 10 candidate regions listed (5 ranked, 4
    rejected with reasons, plus two boundary variants). Other cut lines exist and were not scored.
