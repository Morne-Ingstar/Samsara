# Full boot: 152_source_cached (3 runs, calibration cache on, overrides ['wake_word_enabled=true'], HEAD 763e509)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.359 | 0.345 | 0.368 |
| main_entry | 0.951 | 0.93 | 0.974 |
| splash_shown | 1.016 | 0.985 | 1.036 |
| init_entry | 1.016 | 0.986 | 1.037 |
| model_kickoff | 1.695 | 1.678 | 1.835 |
| config_watcher | 1.755 | 1.74 | 1.899 |
| shell_ready | 1.755 | 1.74 | 1.899 |
| tray_created | 1.782 | 1.771 | 1.933 |
| tts_ready | 2.551 | 2.394 | 2.592 |
| whisper_ready | 4.555 | 4.513 | 4.601 |
| silero_ready | 4.662 | 4.616 | 4.757 |
| ready_for_dictation | 4.662 | 4.616 | 4.759 |
| startup_complete | 4.67 | 4.623 | 4.877 |
| wake_models_ready | 6.047 | 5.91 | 6.482 |
| wake_listener_active | 6.351 | 6.216 | 6.793 |
| ava_readiness_resolved | 1.458 | 1.445 | 1.467 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 125 | 0 | 172 |
| audio device enumeration | 0 | 0 | 94 |
| mic calibration | 0 | 0 | 15 |
| sound setup | 31 | 31 | 46 |
| plugin discovery + command executor | 250 | 235 | 266 |
| history / SQLite init | 219 | 172 | 344 |
| TTS engine init (deferred) | 31 | 31 | 31 |
| smart actions init | 0 | 0 | 0 |
| keyboard/mouse listener setup | 0 | 0 | 16 |
| ACE audio engine start | 15 | 15 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 63 | 62 | 79 |
| tray icon created | 765 | 734 | 953 |
| async: Whisper model load (hotkey dictation ready) | 2828 | 2735 | 2922 |
| async: Silero VAD load | 63 | 47 | 234 |
| async: wake word + audio stream start | 15 | 0 | 125 |
| async: startup complete | 0 | 0 | 0 |
