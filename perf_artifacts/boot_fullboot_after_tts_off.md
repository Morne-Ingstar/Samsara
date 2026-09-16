# Full boot: after_tts_off (3 runs, calibration cache on, overrides ['tts.enabled=false'], HEAD eeebf68)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.293 | 0.283 | 0.525 |
| main_entry | 0.861 | 0.834 | 1.306 |
| splash_shown | 0.898 | 0.871 | 1.387 |
| init_entry | 0.898 | 0.872 | 1.39 |
| model_kickoff | 1.437 | 1.284 | 1.835 |
| config_watcher | 1.497 | 1.342 | 1.899 |
| shell_ready | 1.497 | 1.343 | 1.899 |
| tray_created | 1.524 | 1.37 | 1.917 |
| whisper_ready | 4.06 | 3.92 | 4.449 |
| silero_ready | 4.124 | 3.98 | 4.505 |
| ready_for_dictation | 4.124 | 3.98 | 4.505 |
| startup_complete | 4.128 | 3.986 | 4.511 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 78 | 62 | 125 |
| audio device enumeration | 15 | 0 | 15 |
| mic calibration | 0 | 0 | 0 |
| sound setup | 32 | 32 | 32 |
| plugin discovery + command executor | 234 | 218 | 234 |
| history / SQLite init | 31 | 31 | 94 |
| TTS engine init (deferred) | 16 | 16 | 31 |
| smart actions init | 0 | 0 | 15 |
| keyboard/mouse listener setup | 15 | 0 | 16 |
| ACE audio engine start | 16 | 16 | 32 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 62 | 63 |
| tray icon created | 531 | 500 | 625 |
| async: Whisper model load (hotkey dictation ready) | 2625 | 2609 | 2641 |
| async: Silero VAD load | 62 | 62 | 63 |
| async: wake word + audio stream start | 0 | 0 | 0 |
| async: startup complete | 0 | 0 | 0 |
