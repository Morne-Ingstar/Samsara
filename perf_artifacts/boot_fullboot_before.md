# Full boot: before (3 runs, calibration cache on, HEAD eeebf68)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.295 | 0.285 | 0.308 |
| main_entry | 0.87 | 0.862 | 0.872 |
| splash_shown | 0.9 | 0.896 | 0.906 |
| init_entry | 0.904 | 0.9 | 0.906 |
| model_kickoff | 1.484 | 1.345 | 1.51 |
| config_watcher | 1.545 | 1.405 | 1.567 |
| shell_ready | 1.545 | 1.405 | 1.568 |
| tray_created | 1.565 | 1.424 | 1.586 |
| tts_ready | 2.189 | 2.052 | 2.197 |
| whisper_ready | 4.106 | 3.998 | 4.118 |
| silero_ready | 4.159 | 4.054 | 4.173 |
| ready_for_dictation | 4.159 | 4.054 | 4.174 |
| startup_complete | 8.169 | 8.059 | 8.185 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 79 | 47 | 172 |
| audio device enumeration | 15 | 0 | 16 |
| mic calibration | 0 | 0 | 0 |
| sound setup | 31 | 31 | 32 |
| plugin discovery + command executor | 234 | 218 | 235 |
| history / SQLite init | 93 | 47 | 219 |
| TTS engine init (deferred) | 16 | 16 | 16 |
| smart actions init | 0 | 0 | 16 |
| keyboard/mouse listener setup | 15 | 0 | 15 |
| ACE audio engine start | 16 | 15 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 62 | 63 |
| tray icon created | 656 | 532 | 672 |
| async: Whisper model load (hotkey dictation ready) | 2625 | 2610 | 2656 |
| async: Silero VAD load | 62 | 47 | 63 |
| async: wake word + audio stream start | 0 | 0 | 0 |
| async: startup complete | 4015 | 4000 | 4016 |
