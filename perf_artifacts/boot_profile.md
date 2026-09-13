# Samsara cold-start profile -- 2026-09-12

Measurement only. No startup code changed. Sources:

- **Live log**: `C:\Users\Morne\.samsara\logs\samsara.log`, boot of 2026-09-11 23:36:51 (cold), cross-checked
  against boots 15:51, 16:55 (cold-ish) and 2026-09-12 09:37, 19:04 (warm page cache).
- **`python -X importtime`** (`perf_artifacts/boot_importtime.txt`, 2,794 modules, one run, warm-ish cache,
  **without** `samsara.torch_guard` -- so it shows the full stack, not exactly what the app pays; see the
  guarded re-run below).
- **`tools/boot_profile.py --gpu-load`**: separate interpreter, `SAMSARA_HOME_DIR` = temp copy of
  `~/.samsara` (config.json only), warm disk cache, live Samsara left running. The script prints its raw
  numbers as JSON on stdout; every number used below is reproduced in the tables.

Timestamps below are wall-clock from the 23:36 boot; process start is the `[TORCH-GUARD] enabled` line at
23:36:48.983 (first log line of the new process). "Total 26,390 ms" in the log is measured from `__init__`
entry (51.250) to "Ready" (17.645); from process start it is 28.7 s.

## 1. Stage table (boot of 2026-09-11 23:36:51)

"Boot thread" = the main thread constructing `DictationApp` (the splash runs on the separate Qt thread and
keeps animating; what the boot thread delays is the tray + main window and everything after it).
"Model thread" = `load_model_async()`'s worker.

| # | stage | ms | thread | blocks the UI? | evidence |
|---|---|---:|---|---|---|
| 1 | Python + `dictation.py` module imports up to `__main__` (incl. `sounddevice` **1,513**, `samsara.audio_engine`/scipy) | 2,086 | boot | yes (nothing shown yet -- splash not up) | log 48.983 -> 51.069; `[BOOT-DIAG] __main__: entry (since sounddevice import: 1513ms)`; warm `import sounddevice` = 210 ms (boot_profile) |
| 2 | instance lock + early UI scale | 4 | boot | yes | 51.069 -> 51.073 |
| 3 | splash init (Qt runtime + SplashScreenQt) | 175 | boot (+Qt) | yes | `[BOOT-DIAG] splash init: 175ms`; splash visible from ~51.25 |
| 4 | `__init__` entry -> `[INIT] Loading config` (no marker covers this) | 502 | boot | yes | 51.250 -> 51.752; nothing logged in between |
| 5 | `load_config` incl. the three MIGRATE steps and their three `save_config()` writes | 32 | boot | yes | `load_config: entry` 51.754 -> `done` 51.784. **The log's "config load: 531ms" is stage 4 + 5** (its timer starts at `__init__` entry) |
| 6 | audio device enumeration (`sd.query_devices` + hostapis) | 234 | boot | yes | `[BOOT-DIAG] get_available_microphones: 233ms`; warm `query_devices()` = 0 ms after PortAudio init |
| 7 | detect_capture_rate | 0 | boot | -- | log |
| 8 | mic calibration: open InputStream + **1.5 s ambient recording** | 1,774 | boot | yes | `[BOOT-DIAG] mic calibration (sd.InputStream open+1.5s record): 1774ms`; `measure_ambient_rms` measured 1,518 ms (`CALIBRATION_DURATION = 1.5`) |
| 9 | sound setup | 125 | boot | yes | log |
| 10 | plugin discovery + command executor (36 files, 198 commands) | 693 | boot | yes | log; two silent gaps: 325 ms before `music.py`, 127 ms before `smart_actions.py` (their imports) |
| 11 | history / SQLite init | 110 | boot | yes | log |
| 12 | TTS engine init (`EdgeTTSEngine` = `import edge_tts`; `AudioCoordinator`) | 1,630 | **boot** | yes | 54.735 -> 56.346; warm: `import edge_tts` 256-400 ms, ctor 0 ms; warm boot 19:04 logged 266 ms |
| 13 | smart actions, keyboard/mouse listener | 12 | boot | yes | log |
| 14 | model load kicked off (thread start) | 6 | boot | -- | 56.361 |
| 15 | **`_start_ace_engine` -> `AudioCaptureEngine.start()`** | **13,371** | boot | yes | 56.366 `start() called` -> 09.729 `[ACE] Opening stream` -> 09.738 returned. The stream open itself is 9 ms. See section 3. |
| 16 | post-ACE wiring, config watcher, main-window post | 512 | boot | yes | 09.738 -> 10.250 (`MainWindowQt._init_window` posted) -- boot thread done at **+21.3 s** from process start |
| A1 | `cuda_detect` + cold `import faster_whisper` (ctranslate2 + CUDA DLLs, av, huggingface_hub) | 4,046 | model | no (delays "Ready") | 56.366 -> 00.412 `[CONFIG] Model: medium, cuda, float16`; warm: 130 ms + 92 ms (boot_profile) |
| A2 | `WhisperModel('medium', cuda, float16)` | 8,280 | model | no | 00.412 -> 08.692 `[OK] Model loaded in 8.3s`; warm 2,450 ms (boot_profile), 2.5-2.6 s on warm boots |
| A3 | Silero VAD: `faster_whisper.vad` import + bundled ONNX load | 212 | model | no | 08.693 -> 08.905; ONNX load 190 ms (`[BOOT-DIAG] ... returned: 190ms`); warm 280 + 143 ms. **The log's "Silero VAD load: 12547ms" = A1 + A2 + A3** (shared stage timer, see section 5) |
| A4 | OpenWakeWord: `import openwakeword` + `Model(hey_jarvis, onnx)` + wake profiles | 8,726 | model | no | 08.916 -> 17.642 `[OWW] Loaded 'hey_jarvis'`; warm 1,394 + 287 ms (boot_profile), 625-656 ms on warm boots. Runs although `wake_word_enabled` is **false** in the live config |
| A5 | wake word + stream start, "Startup complete", last_known_good | 13 | model | no | 17.645 -> 17.655 ("Ready" at **+28.7 s** from process start) |

Critical path to "Ready": stages 1-14 (**7.4 s** on the boot thread before the model thread even starts)
+ A1 + A2 + A3 + A4 (**21.3 s** sequential on the model thread). The boot thread's own 13.4 s ACE stall
(15) runs in parallel with A1-A2 and does not extend "Ready", but it holds back the tray/main window by
~12 s and is the reason the app looks frozen after the splash.

## 2. Import cost

### 2a. `-X importtime`, full stack, no torch_guard (top-30 by cumulative, `boot_importtime.txt`)

| cum ms | self ms | module |
|---:|---:|---|
| 30,921 | 26 | faster_whisper |
| 29,441 | 41 | faster_whisper.transcribe |
| 29,237 | 66 | ctranslate2 |
| 29,154 | 3 | ctranslate2.converters |
| 29,111 | 2 | ctranslate2.converters.transformers |
| 29,108 | **19,285** | transformers |
| 13,651 | 48 | openwakeword |
| 13,313 | 20 | openwakeword.custom_verifier_model |
| 13,154 | 201 | sklearn.linear_model |
| 11,174 | **9,425** | torch |
| 11,054 | 47 | sklearn |
| 10,909 | 34 | sklearn.base |
| 10,865 | 0 | sklearn.utils._metadata_requests |
| 10,865 | 96 | sklearn.utils |
| 10,608 | 13 | sklearn.utils._chunking |
| 10,595 | 18 | sklearn.utils._param_validation |
| 9,822 | 25 | transformers.dependency_versions_check |
| 9,796 | 1 | transformers.utils.versions |
| 9,795 | 63 | transformers.utils |
| 9,090 | 23 | sklearn.utils.validation |
| 8,264 | 24 | sklearn.utils._array_api |
| 7,863 | 22 | sklearn.utils.fixes |
| 7,249 | 84 | transformers.utils.auto_docstring |
| 7,006 | 44 | transformers.utils.generic |
| 4,260 | 81 | pandas |
| 4,031 | 23 | transformers.utils.logging |
| 4,006 | 381 | huggingface_hub.utils |
| 3,117 | 11 | huggingface_hub.errors |
| 3,106 | 92 | httpx |
| 2,996 | 229 | scipy.stats |

Top-level requested: faster_whisper 30,921 · openwakeword 13,651 · torch 11,174 · onnxruntime 235 ·
sounddevice 182 · PySide6.QtWidgets 49 ms. Sum of self time 56.2 s (single run; includes some cold pages).

### 2b. What the app actually pays (`samsara.torch_guard.install()` first, warm, `-X importtime`)

`import faster_whisper` chain = **297 ms** total (ctranslate2 127, av 58, huggingface_hub present,
**transformers NOT imported**). torch_guard prevents ctranslate2's converters from dragging in
`transformers` (19.3 s self in 2a). So the 4.0 s of A1 on a cold boot is not the transformers cost; it is
DLL/page loading of ctranslate2 + CUDA runtime + av + huggingface_hub, which warm costs 0.3 s.

### 2c. Isolated per-module cost, warm cache (`boot_profile.py`, one interpreter each)

| module | ms | | module | ms |
|---|---:|---|---|---:|
| numpy | 59 | | openwakeword | **1,383** |
| sounddevice | 210 | | openwakeword.model | 1,373 |
| scipy.signal | 854 | | sklearn (pulled in by openwakeword) | 1,159 |
| onnxruntime | 116 | | edge_tts | 400 |
| ctranslate2 | 190 | | samsara.tts | 89 |
| faster_whisper | 283 | | PySide6.QtWidgets | 56 |
| faster_whisper.vad | 294 | | samsara.audio_engine | 67 |

Cold vs warm ratio from the logs: sounddevice 1,513 vs 210 ms; TTS 1,630 vs 266 ms; OWW 8,726 vs 656 ms;
Whisper load 8,280 vs 2,450 ms. Cold start is dominated by first-touch disk reads, not CPU.

## 3. What the "12.3 s before the 190 ms Silero load" is

Refuted: it is **not** Silero, not device enumeration, not a lock wait. Two separate things were mislabelled:

**(a) The model thread's "Silero VAD load: 12,547 ms" stage.** `_boot_log("async: Silero VAD load")` is
the first `_boot_log` call on the model thread, and the shared stage timer's "last" timestamp is the boot
thread's `model load kicked off` at 56.358. So that number is the whole model thread up to that point:

| | ms | evidence |
|---|---:|---|
| `cuda_detect` + cold `import faster_whisper` (ctranslate2 + CUDA DLLs, av, huggingface_hub) | 4,046 | 56.366 -> 00.412 |
| `WhisperModel('medium', device=cuda, float16)` | 8,280 | `[OK] Model loaded in 8.3s` |
| `faster_whisper.vad` import + `SileroVADModel` ONNX load | 212 | `ONNX load returned: 190ms` |
| **sum** | **12,538** | matches 12,547 |

**(b) The boot thread's `ACE engine.start()` = 13,371 ms.** The `[ACE] Opening stream` log line is
written *after* the resample-filter design and *before* `sd.InputStream(...)`; `returned` follows it by
9 ms. So the whole stall is inside `_open_stream` before the stream is even opened -- i.e. in
`_design_polyphase_filter(160, 441)` (scipy `firwin`, pure numpy/scipy compute on the main thread; no
device call, no lock: `_capture_rate` is passed explicitly so `query_devices` is skipped).

Reproduction, warm cache (`boot_profile.py`, main thread runs the same steps 250 ms after starting a
worker that does exactly what `load_model_async` does, incl. the real CUDA load):

| main-thread step | with worker | no worker |
|---|---:|---:|
| `_design_polyphase_filter(160, 441)` | 909 ms | 751 ms |
| `sd.InputStream(44100).start()` | 15 ms | 14 ms |
| **ACE start equivalent** | **924 ms** | 765 ms |
| worker: cuda_detect 130 ms, import faster_whisper 92 ms, WhisperModel 2,448 ms; 10-ms GIL ticker pauses > 60 ms | **0** | -- |

So with a warm cache the stall is ~0.9 s and is the filter design itself (that is what the warm boots
show: 1,442 / 1,318 ms). The 13.4 s only happens on cold boots and its length tracks the model thread's
cold phase boot-by-boot (stall / model-thread import+load: 13.4/12.3 s, 14.1/16.0 s, 3.6/4.4 s,
1.4/2.8 s) and ends *during* the load, not at its end. The only mechanism consistent with all of that is
the main thread being starved while the model thread performs cold extension-module loads
(`LoadLibrary` of ctranslate2 / cuDNN / cuBLAS / av / onnxruntime inside `_imp.create_dynamic`, which
CPython runs **with the GIL held**) and cold page-ins of the 1.5 GB model -- the filter design is a
GIL-bound numpy/scipy loop and gets almost no scheduling until those finish. This could not be reproduced
here because the disk cache cannot be evicted without a reboot; it is the best-supported explanation, not
a measured one. Either way, the fix does not depend on the mechanism: the filter design is deterministic
for (160, 441) and can be cached, and the ACE engine can start before the model thread is kicked off.

**OpenWakeWord's 7.9 s** (8,726 ms wall on the model thread): warm it is `import openwakeword`
1,394 ms (of which sklearn ~1,160 ms via `openwakeword.custom_verifier_model`, which Samsara never uses)
+ `Model(hey_jarvis, onnx)` 287 ms + first predict 1 ms. Cold it is the same imports plus onnxruntime
DLLs and the three ONNX files paged in from disk. It runs on every boot even though the live config has
`wake_word_enabled: false`, and `_load_wake_profile_models()` runs right after it.

## 4. Confirm / refute

| claim | verdict | evidence |
|---|---|---|
| The three MIGRATE steps run on every boot | **Confirmed** -- and why | 66 `[MIGRATE]` lines over the last 22 diag-era boots (3 per boot, every boot: 15:51, 16:55, 23:36, 09:37, 19:04 all show the same three). Cause: each migration deletes its legacy key from `self.config` and calls `save_config()`, but `save_config()` writes `_three_way_merge(last_snap, self.config, on_disk)` and the rule "new key in disk only -> include from disk" (the disk snapshot is only taken *after* `load_config`, so `base` lacks the key) puts `wake_targets`, `command_mode_enabled` and `ai_command_mode` straight back. Every saved copy is the same 10,771 bytes and all still contain the three keys (`config.json`, `config.json.bak`, the 16 `config_backups/*.json`). Cost is small (32 ms incl. three writes, three backup rotations per boot) -- the real cost is that the migration can never finish and the backup ring fills with identical files. |
| The 1.5 s calibration recording runs on every boot | **Confirmed** | `_run_calibration_if_auto()` is unconditional in `__init__` when `threshold_mode == 'auto'` (live config: `auto`); `measure_ambient_rms` sleeps `CALIBRATION_DURATION = 1.5` s with an open stream (measured 1,518 ms); every diag-era boot logs 1,538-1,774 ms. The result is written into the in-memory config and persisted only by the next `save_config`; nothing reads a previous calibration back. |
| TTS init is on the boot thread | **Confirmed** | `logger.info("[INIT] Initializing TTS...")` at `dictation.py:2744` sits in `DictationApp.__init__` between the hints setup and `load_model_async()`; the model thread does not exist yet. 1,630 ms cold (`import edge_tts` + `samsara.tts`), 266 ms warm. |
| "config load 531 ms" | **Mislabelled** | `load_config` is 32 ms; the other ~500 ms is `__init__` work before `[INIT] Loading config` with no marker. |
| `[BOOT]` stage timers are trustworthy | **No** | `_boot()`/`_boot_log()` share one "last timestamp" across threads: `ACE audio engine start: 828ms` (real 13,371), `Silero VAD load: 12547ms` (real 212), `OpenWakeWord: 7906ms` (real 8,726). Only the `[BOOT-DIAG]` lines with explicit `perf_counter` deltas are reliable. |

## 5. Ranked fixes

| rank | fix | saves (measured basis) | risk |
|---|---|---|---|
| 1 | **Do not load OpenWakeWord when no wake mode is enabled**; load on first `start_wake_word_mode()` / profile enable, on its own thread (also skips the sklearn import that `openwakeword.custom_verifier_model` drags in). | "Ready" **8.7 s** earlier cold, 0.6 s warm (stage A4; live config has wake word off). | low -- first wake enable pays 1.7 s warm; needs a "wake ready" state for the tray/earcon. |
| 2 | **Unblock the boot thread from the model thread**: start the ACE engine *before* `load_model_async()` (two-line reorder), and cache the (160, 441) polyphase filter (`_design_polyphase_filter` is deterministic; compute once, keep on disk or module level). | tray/main window **~12-13 s** earlier on cold boots (stage 15 -> ~1 s); 0.75-0.9 s on every boot from the cache. "Ready" unchanged. | low -- reorder delays the model kick-off by ~1 s warm unless the filter is cached; cache must key on (up, down). |
| 3 | **TTS off the boot path**: construct `EdgeTTSEngine`/`AudioCoordinator` lazily on first `speak()` or on a worker after the tray is up. | 1,630 ms cold / 266 ms warm off the boot thread (stage 12). | low-medium -- startup status announcements must wait for the engine; keep the `tts_engine is None` guards. |
| 4 | **Persist calibration** (`speech_threshold` + device id + timestamp); skip the 1.5 s recording when the same device calibrated < 24 h ago, recalibrate in the background after boot. | 1,774 ms off the boot thread every boot (stage 8). | medium -- room noise changes; keep the manual recalibrate and the background refresh. |
| 5 | **Parallel model loads with per-capability ready**: Whisper on one thread, Silero + OWW on another, each publishing its own ready flag (hold-to-dictate needs Whisper only). | "Ready for dictation" = max(A1+A2, A3+A4) instead of the sum: **8.9 s** earlier cold, 0.8 s warm; with fix 1 the second thread is 0.2 s. | medium -- cold-disk contention between the two loads; the tray must show per-capability state; `_vad_lock` already exists. |
| 6 | **Fix the migration persistence bug** (take `_config_last_disk_snapshot` *before* running migrations, or have migration saves bypass the three-way merge); optionally a `config_version` short-circuit. | ~30 ms + 3 writes + 3 backup rotations per boot; stops the backup ring filling with identical copies; the three `[MIGRATE]` lines disappear. | low -- one snapshot ordering change; verify with the temp-home run that the keys stay gone after a second load. |
| 7 | **Lazy imports on the boot path**: `sounddevice` (1.5 s cold, needed only from stage 6 on -- import it after the splash is posted so the splash appears ~1.5 s sooner), `scipy.signal` (854 ms warm, only needed by the filter design / fix 2), `music.py` and `smart_actions.py` plugin imports (325 + 127 ms). | splash **~1.5 s** earlier cold; ~0.5 s boot thread (plugins); scipy only if fix 2's cache lands. | low -- `_PRE_SD_T`/`_POST_SD_T` diagnostics move with it. |
| 8 | **Per-thread `[BOOT]` stage timers** (or drop `_boot_log` on the model thread). | 0 s -- but the map's 26.4 s breakdown was read from corrupted numbers; every future measurement needs this. | none. |

Expected after 1-4 (cold): boot thread ≈ 3 s to tray/window (from 21.3 s), "Ready" ≈ 12.5 s (from 28.7 s),
gated by the Whisper load; with 5, "Ready" ≈ 12.5 s cold / ≈ 5 s warm.

## 6. Not measured

- Cold-cache numbers were taken from the live log only; the profile script ran warm (no way to evict the
  page cache without a reboot). The GIL-starvation explanation for the 13.4 s ACE stall is inferred from
  the log correlation, not reproduced.
- Stage 4's 502 ms (`__init__` before `load_config`) has no marker and was not attributed.
- `AudioCoordinator` construction was not timed separately from `EdgeTTSEngine` (both inside stage 12).
