# Full boot: 152_source_nocal (3 runs, calibration cache off, overrides ['wake_word_enabled=true'], HEAD 763e509)

Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).

| milestone | median s | min | max |
|---|---:|---:|---:|
| first_log | 0.351 | 0.344 | 0.352 |
| main_entry | 0.938 | 0.923 | 0.965 |
| splash_shown | 1.001 | 0.977 | 1.033 |
| init_entry | 1.006 | 0.983 | 1.039 |
| model_kickoff | 3.176 | 3.146 | 3.283 |
| config_watcher | 3.252 | 3.202 | 3.345 |
| shell_ready | 3.252 | 3.203 | 3.345 |
| tray_created | 3.302 | 3.23 | 3.377 |
| tts_ready | 3.873 | 3.78 | 3.956 |
| whisper_ready | 5.872 | 5.767 | 5.991 |
| silero_ready | 5.927 | 5.83 | 6.053 |
| ready_for_dictation | 5.927 | 5.831 | 6.053 |
| startup_complete | 5.933 | 5.839 | 6.061 |
| wake_models_ready | 7.161 | 7.128 | 7.368 |
| wake_listener_active | 7.478 | 7.436 | 7.672 |
| ava_readiness_resolved | 2.981 | 2.956 | 3.044 |

| [BOOT] stage | median ms | min | max |
|---|---:|---:|---:|
| config load | 141 | 110 | 156 |
| audio device enumeration | 16 | 15 | 16 |
| mic calibration | 1532 | 1531 | 1562 |
| sound setup | 31 | 31 | 32 |
| plugin discovery + command executor | 235 | 234 | 250 |
| history / SQLite init | 172 | 156 | 203 |
| TTS engine init (deferred) | 31 | 31 | 31 |
| smart actions init | 0 | 0 | 0 |
| keyboard/mouse listener setup | 16 | 0 | 16 |
| ACE audio engine start | 15 | 0 | 16 |
| model load kicked off (async) | 0 | 0 | 0 |
| shell ready (tray + main window scheduled) | 63 | 63 | 78 |
| tray icon created | 2313 | 2235 | 2344 |
| async: Whisper model load (hotkey dictation ready) | 2703 | 2625 | 2703 |
| async: Silero VAD load | 63 | 47 | 63 |
| async: wake word + audio stream start | 0 | 0 | 15 |
| async: startup complete | 15 | 0 | 16 |
