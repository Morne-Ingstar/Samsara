# Boot fixes, queue 46 -- 2026-09-14

## What these numbers are
- **Where they come from:** `tools/boot_profile.py --full-boot 3` runs, reading real `dictation.py` boots in a temp profile.
- **Profile:** the live config (with hotkeys rebound to F13-F24), the calibration cache, hints, the app index and the polyphase filter cache.
- **Units:** seconds since `Popen`, as median (min-max) over 3 runs.
- **Cache state:** warm disk cache. A true cold boot needs a reboot, so the 26.4 s cold figure from queue 20 can't be reproduced here.
- **Conditions:** the live Samsara (PID 45868) was running alongside every measured boot, before and after.

Raw data:
- `boot_fullboot_{before,after,before_nocal,after_nocal,after_tts_off,final}.{json,md}`
- `config_migration_cost.py`

## Milestones
| milestone | before | after | before, no cal cache | after, no cal cache | after, TTS off | final tree |
|---|---|---|---|---|---|---|
| `__main__` entry | 0.87 (0.86-0.87) | 0.85 (0.84-0.88) | 0.85 (0.84-0.87) | 0.89 (0.88-0.92) | 0.86 (0.83-1.31) | 0.85 (0.85-0.89) |
| splash shown | 0.90 (0.90-0.91) | 0.89 (0.88-0.91) | 0.88 (0.88-0.91) | 0.93 (0.92-0.96) | 0.90 (0.87-1.39) | 0.89 (0.88-0.93) |
| model thread kicked off | 1.48 (1.34-1.51) | 1.37 (1.36-1.50) | 2.97 (2.87-3.00) | 2.91 (2.91-3.02) | 1.44 (1.28-1.83) | 1.46 (1.40-1.56) |
| shell ready (tray + window scheduled) | 1.54 (1.41-1.57) | 1.44 (1.42-1.56) | 3.03 (2.93-3.06) | 2.98 (2.97-3.08) | 1.50 (1.34-1.90) | 1.52 (1.49-1.62) |
| TTS ready | 2.19 (2.05-2.20) | 2.08 (2.04-2.16) | 3.60 (3.53-3.61) | 3.60 (3.57-3.66) | - | 2.16 (2.09-2.23) |
| Whisper loaded (hotkey dictation works) | 4.11 (4.00-4.12) | 3.96 (3.94-4.09) | 5.52 (5.45-5.56) | 5.53 (5.48-5.62) | 4.06 (3.92-4.45) | 4.10 (3.99-4.26) |
| Ready for dictation | 4.16 (4.05-4.17) | 4.01 (4.00-4.15) | 5.58 (5.51-5.61) | 5.58 (5.53-5.68) | 4.12 (3.98-4.50) | 4.17 (4.04-4.33) |
| **Startup complete** (splash closes) | **8.17 (8.06-8.19)** | **4.02 (4.01-4.16)** | **9.59 (9.52-9.62)** | **5.59 (5.54-5.68)** | 4.13 (3.99-4.51) | **4.17 (4.05-4.33)** |

## `[BOOT]` stages (ms)
| stage | before | after | before, no cal cache | after, no cal cache |
|---|---|---|---|---|
| config load | 79 (47-172) | 93 (79-141) | 156 (109-188) | 109 (63-125) |
| mic calibration | 0 (cached) | 0 (cached) | 1532 (1531-1532) | 1532 (1531-1547) |
| plugin discovery + command executor | 234 | 234 | 219 | 219 |
| TTS engine init (deferred) | 16 | 31 | 16 | 31 |
| ACE audio engine start | 16 | 16 | 16 | 0 |
| Whisper model load | 2625 (2610-2656) | 2594 (2578-2594) | 2562 | 2609 |
| Silero VAD load | 62 | 62 | 62 | 47 |
| **Silero -> Startup complete** | **4015 (4000-4016)** | **0** | **4016** | **0** |

No run logged a `[MIGRATE]` line.

## Per item
1. **Heavy runtimes on the boot path.**
   - **What queue 20 measured is already fixed.** The 13.4 s `_start_ace_engine` stall and the 12.5 s "Silero" stage were handled by 02d (`9b4c884`). ACE now takes 16 ms and Silero 62 ms.
   - **Lazy `sounddevice` was not done.**
     - The warm import is 166 ms.
     - `samsara/audio_devices.py`, `voice_training_qt.py` and `mic_setup_wizard_qt.py` import it at module load anyway.
     - A lazy `audio_engine` import once stalled `__init__` for 11 s; `a2b97a0` hoisted it on purpose.
   - **What was fixed instead:** the Smart Corrections Ollama warm-up. It made a synchronous backend HTTP probe (4.0 s whenever Ollama is down), ran even with the feature off, and blocked "Startup complete". It now runs on a registered thread, and only when Smart Corrections is on.
   - **Result:** "Startup complete" is 4.15 s earlier (8.17 -> 4.02 s). Hotkey dictation readiness is unchanged (4.16 -> 4.01 s, within noise).
2. **Calibration persistence** (02d). The measured recording costs 1532 ms on the boot thread. The cache saves it: shell ready 3.03 -> 1.54 s, dictation ready 5.58 -> 4.16 s.
   - **Added:** a threshold clamped at `CALIBRATION_CEILING` (0.15) is never persisted, never reused from the cache, and never applied by the background refresh. The live log had an "ambient" RMS of 0.1589 that was stored as 0.1500 and reused on the next boot.
   - **Forced recalibration:** tray > Tools > Recalibrate Mic.
3. **Migrations** (02d). On today's live config, `load_config` takes 1.46 ms with the migrations and 1.48 ms with them stubbed out. It makes 0 saves; the checks take 0.0004 ms. No `config_version` was added (see the report).
4. **TTS** (02d). The engine is built on a worker after shell ready and is ready 0.64 s later (warm). With TTS off, Whisper loads in 4.06 s versus 3.96 s with it on, so there's no contention. Lazy-on-first-speech would add about 0.6 s warm (about 2.7 s cold, from the live log's `tts_ready in 2689ms`) to the first spoken response, for no boot gain.

## Cold reference (live log, boot of 2026-09-14 17:42, before this change)
| stage | value |
|---|---|
| process start -> `__main__` | 4.2 s |
| `__init__` -> model kick-off | 1.84 s |
| config watcher (cold `watchdog` import) | 1.39 s |
| Whisper load | 10.8 s |
| Ollama probe gap | 4.0 s |
| "Startup complete" (from process start) | 21.2 s |

With this change, the expected cold "Startup complete" is about 17.2 s. That's the 4.0 s gap removed, inferred rather than measured cold.
