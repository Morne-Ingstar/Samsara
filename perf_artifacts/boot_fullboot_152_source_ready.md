# Full boot: 152_source_ready (3 runs, calibration cache on, overrides ['wake_word_enabled=true'], HEAD 763e509)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.353 | 0.338 | 0.363 |
| main_entry | 0.945 | 0.929 | 0.952 |
| splash_shown | 1.019 | 0.998 | 1.027 |
| init_entry | 1.019 | 1.004 | 1.028 |
| model_kickoff | 1.752 | 1.66 | 1.786 |
| config_watcher | 1.828 | 1.72 | 1.843 |
| shell_ready | 1.829 | 1.72 | 1.844 |
| tray_created | 1.857 | 1.748 | 1.882 |
| tts_ready | 2.442 | 2.319 | 2.487 |
| whisper_ready | 4.429 | 4.235 | 4.44 |
| silero_ready | 4.491 | 4.297 | 4.499 |
| ready_for_dictation | 4.491 | 4.297 | 4.499 |
| startup_complete | 4.5 | 4.303 | 4.51 |
| wake_models_ready | 5.744 | 5.516 | 5.754 |
| wake_listener_active | 6.057 | 5.816 | 6.065 |
| ava_readiness_resolved | 1.572 | 1.458 | 1.576 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 157 | 0 | 235 |
| audio device enumeration | 15 | 15 | 234 |
| mic calibration | 0 | 0 | 0 |
| sound setup | 32 | 31 | 32 |
| plugin discovery + command executor | 250 | 218 | 250 |
| history / SQLite init | 156 | 141 | 172 |
| TTS engine init (deferred) | 31 | 31 | 47 |
| smart actions init | 0 | 0 | 0 |
| keyboard/mouse listener setup | 16 | 15 | 16 |
| ACE audio engine start | 16 | 16 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 63 | 62 | 78 |
| tray icon created | 828 | 750 | 860 |
| async: Whisper model load (hotkey dictation ready) | 2656 | 2578 | 2671 |
| async: Silero VAD load | 62 | 47 | 63 |
| async: wake word + audio stream start | 16 | 0 | 16 |
| async: startup complete | 0 | 0 | 0 |
