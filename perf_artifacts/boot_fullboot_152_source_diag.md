# Full boot: 152_source_diag (3 runs, calibration cache on, overrides ['wake_word_enabled=true'], HEAD 763e509)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.342 | 0.325 | 0.358 |
| main_entry | 0.923 | 0.897 | 0.953 |
| splash_shown | 0.985 | 0.959 | 1.004 |
| init_entry | 0.986 | 0.965 | 1.008 |
| model_kickoff | 1.638 | 1.526 | 1.677 |
| config_watcher | 1.701 | 1.585 | 1.738 |
| shell_ready | 1.701 | 1.585 | 1.739 |
| tray_created | 1.731 | 1.606 | 1.766 |
| tts_ready | 2.293 | 2.152 | 2.335 |
| whisper_ready | 4.243 | 4.089 | 4.292 |
| silero_ready | 4.299 | 4.151 | 4.35 |
| ready_for_dictation | 4.299 | 4.151 | 4.35 |
| startup_complete | 4.306 | 4.16 | 4.357 |
| wake_models_ready | 5.572 | 5.404 | 5.661 |
| wake_listener_active | 5.881 | 5.702 | 5.967 |
| ava_readiness_resolved | 1.451 | 1.365 | 1.459 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 156 | 125 | 172 |
| audio device enumeration | 0 | 0 | 16 |
| mic calibration | 0 | 0 | 16 |
| sound setup | 31 | 31 | 31 |
| plugin discovery + command executor | 234 | 219 | 234 |
| history / SQLite init | 141 | 125 | 156 |
| TTS engine init (deferred) | 31 | 31 | 32 |
| smart actions init | 0 | 0 | 15 |
| keyboard/mouse listener setup | 16 | 16 | 16 |
| ACE audio engine start | 16 | 15 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 62 | 63 |
| tray icon created | 750 | 641 | 750 |
| async: Whisper model load (hotkey dictation ready) | 2562 | 2562 | 2657 |
| async: Silero VAD load | 63 | 62 | 63 |
| async: wake word + audio stream start | 0 | 0 | 15 |
| async: startup complete | 0 | 0 | 0 |
