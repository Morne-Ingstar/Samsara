# Full boot: after (3 runs, calibration cache on, overrides none, HEAD eeebf68)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.305 | 0.278 | 0.307 |
| main_entry | 0.855 | 0.839 | 0.875 |
| splash_shown | 0.888 | 0.875 | 0.911 |
| init_entry | 0.891 | 0.875 | 0.911 |
| model_kickoff | 1.373 | 1.359 | 1.5 |
| config_watcher | 1.435 | 1.422 | 1.556 |
| shell_ready | 1.435 | 1.422 | 1.556 |
| tray_created | 1.455 | 1.442 | 1.574 |
| tts_ready | 2.076 | 2.036 | 2.158 |
| whisper_ready | 3.96 | 3.945 | 4.093 |
| silero_ready | 4.014 | 3.999 | 4.15 |
| ready_for_dictation | 4.014 | 3.999 | 4.15 |
| startup_complete | 4.02 | 4.007 | 4.156 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 93 | 79 | 141 |
| audio device enumeration | 15 | 0 | 16 |
| mic calibration | 0 | 0 | 0 |
| sound setup | 31 | 16 | 32 |
| plugin discovery + command executor | 234 | 234 | 234 |
| history / SQLite init | 94 | 78 | 109 |
| TTS engine init (deferred) | 31 | 16 | 32 |
| smart actions init | 0 | 0 | 0 |
| keyboard/mouse listener setup | 15 | 0 | 15 |
| ACE audio engine start | 16 | 16 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 62 | 62 | 63 |
| tray icon created | 578 | 563 | 672 |
| async: Whisper model load (hotkey dictation ready) | 2594 | 2578 | 2594 |
| async: Silero VAD load | 62 | 47 | 62 |
| async: wake word + audio stream start | 0 | 0 | 15 |
| async: startup complete | 0 | 0 | 0 |
