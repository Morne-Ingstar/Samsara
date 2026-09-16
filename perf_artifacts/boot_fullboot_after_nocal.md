# Full boot: after_nocal (3 runs, calibration cache off, overrides none, HEAD eeebf68)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.3 | 0.293 | 0.323 |
| main_entry | 0.889 | 0.876 | 0.917 |
| splash_shown | 0.926 | 0.921 | 0.957 |
| init_entry | 0.926 | 0.921 | 0.957 |
| model_kickoff | 2.914 | 2.913 | 3.016 |
| config_watcher | 2.976 | 2.972 | 3.075 |
| shell_ready | 2.976 | 2.972 | 3.075 |
| tray_created | 2.992 | 2.992 | 3.092 |
| tts_ready | 3.604 | 3.574 | 3.658 |
| whisper_ready | 5.527 | 5.479 | 5.617 |
| silero_ready | 5.58 | 5.532 | 5.677 |
| ready_for_dictation | 5.58 | 5.532 | 5.677 |
| startup_complete | 5.588 | 5.538 | 5.684 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 109 | 63 | 125 |
| audio device enumeration | 15 | 0 | 16 |
| mic calibration | 1532 | 1531 | 1547 |
| sound setup | 16 | 15 | 31 |
| plugin discovery + command executor | 219 | 219 | 235 |
| history / SQLite init | 93 | 31 | 109 |
| TTS engine init (deferred) | 31 | 16 | 32 |
| smart actions init | 0 | 0 | 0 |
| keyboard/mouse listener setup | 16 | 15 | 16 |
| ACE audio engine start | 0 | 0 | 0 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 62 | 63 |
| tray icon created | 2063 | 2062 | 2141 |
| async: Whisper model load (hotkey dictation ready) | 2609 | 2578 | 2625 |
| async: Silero VAD load | 47 | 46 | 47 |
| async: wake word + audio stream start | 0 | 0 | 16 |
| async: startup complete | 0 | 0 | 16 |
