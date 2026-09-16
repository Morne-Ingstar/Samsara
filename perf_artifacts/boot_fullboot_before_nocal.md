# Full boot: before_nocal (3 runs, calibration cache off, HEAD eeebf68)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.291 | 0.281 | 0.295 |
| main_entry | 0.85 | 0.841 | 0.871 |
| splash_shown | 0.881 | 0.876 | 0.905 |
| init_entry | 0.884 | 0.877 | 0.908 |
| model_kickoff | 2.969 | 2.872 | 3.001 |
| config_watcher | 3.028 | 2.934 | 3.059 |
| shell_ready | 3.028 | 2.934 | 3.059 |
| tray_created | 3.05 | 2.95 | 3.081 |
| tts_ready | 3.595 | 3.53 | 3.611 |
| whisper_ready | 5.52 | 5.449 | 5.556 |
| silero_ready | 5.579 | 5.507 | 5.608 |
| ready_for_dictation | 5.579 | 5.507 | 5.609 |
| startup_complete | 9.587 | 9.519 | 9.617 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 156 | 109 | 188 |
| audio device enumeration | 0 | 0 | 0 |
| mic calibration | 1532 | 1531 | 1532 |
| sound setup | 31 | 31 | 31 |
| plugin discovery + command executor | 219 | 219 | 234 |
| history / SQLite init | 93 | 62 | 94 |
| TTS engine init (deferred) | 16 | 16 | 16 |
| smart actions init | 15 | 0 | 15 |
| keyboard/mouse listener setup | 0 | 0 | 16 |
| ACE audio engine start | 16 | 0 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 47 | 63 |
| tray icon created | 2141 | 2062 | 2203 |
| async: Whisper model load (hotkey dictation ready) | 2562 | 2516 | 2593 |
| async: Silero VAD load | 62 | 47 | 63 |
| async: wake word + audio stream start | 0 | 0 | 0 |
| async: startup complete | 4016 | 4016 | 4016 |
