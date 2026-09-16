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
