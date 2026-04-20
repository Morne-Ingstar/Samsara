# Samsara Architecture

## System Overview

Samsara is a voice-controlled computing tool for Windows. It captures audio
from the microphone, detects speech using Silero VAD, transcribes via Whisper,
cleans the text through a phonetic wash layer, matches commands via a unified
token-based registry, and dispatches actions.

## Audio → Action Pipeline

```
Microphone (sounddevice, capture at device native rate)
  │
  ├─ [Hold/Toggle mode] ──► Direct to Whisper on release/silence
  │
  └─ [Wake word mode] ──► Silero VAD (real-time, per-chunk)
                              │
                              ├─ No speech → discard (fans, ambient ignored)
                              │
                              └─ Speech detected → buffer until 0.8s silence
                                    │
                                    ▼
                              Resample to 16kHz
                                    │
                                    ▼
                              Whisper transcription (faster-whisper, CUDA)
                                    │
                                    ▼
                              Wake corrections (charvis → jarvis)
                                    │
                                    ▼
                              Wake phrase matching (token-aware boundaries)
                                    │
                                    ▼
                              Echo stripping (duplicate wake words)
                                    │
                                    ▼
                              Phonetic wash (fine tab → find tab,
                                  punctuation stripping, symbol→word)
                                    │
                                    ▼
                              CommandMatcher (token-based, longest-match)
                                    │
                                    ├─ Built-in → hotkey/launch/macro/text/method
                                    ├─ Plugin → handler(app, remainder)
                                    └─ No match → paste as dictated text
```

## State Machine (4 States)

```
         wake word          "dictate"/"type"
ASLEEP ──────────► COMMAND ──────────────────► LONG DICTATION
                   WINDOW ──────────────────► QUICK DICTATION
                     │         hotkey
                     │
                     └─► command executed → ASLEEP
                     └─► 3s timeout → ASLEEP
```

| State | Trigger | Behavior |
|-------|---------|----------|
| Asleep | Default | VAD + wake word listener active |
| Command Window | Wake word | 3s window for commands |
| Quick Dictation | Hotkey or "type" | 0.8s silence auto-finishes |
| Long Dictation | "dictate" | No timeout, say "over" to finish |

## Key Modules

### Core
| File | Purpose |
|------|---------|
| `dictation.py` (~3,800 lines) | DictationApp engine + in-app CommandExecutor |
| `commands.json` | 104 built-in voice command definitions |
| `voice_training.py` | Mic calibration, vocabulary, corrections |

### Command System
| File | Purpose |
|------|---------|
| `samsara/command_registry.py` | Unified CommandMatcher — token-based longest-match |
| `samsara/commands.py` | Modular CommandExecutor (used by tests) |
| `samsara/plugin_commands.py` | Plugin registry, @command decorator, load_plugins() |
| `samsara/phonetic_wash.py` | Fixes Whisper misrecognitions before matching |

### Audio
| File | Purpose |
|------|---------|
| `samsara/calibration.py` | Auto-threshold via IQR outlier rejection |
| `samsara/echo_cancel.py` | Frequency-domain AEC (FFT + WASAPI loopback) |
| `samsara/constants.py` | Sample rates, thresholds, timing values |

### Wake Word
| File | Purpose |
|------|---------|
| `samsara/wake_word_matcher.py` | Token-aware wake phrase matching |
| `samsara/wake_corrections.py` | Whisper misrecognition map for wake phrases |
| `samsara/command_parser.py` | Intent routing (command vs dictation vs mode) |

### UI (all in samsara/ui/)
| File | Purpose |
|------|---------|
| `settings_window.py` | Settings UI (lazy tabs, staged building) |
| `listening_indicator.py` | Always-on-top overlay (pulses teal when active) |
| `wake_word_debug.py` | Debug window with trace pipeline |
| `first_run_wizard.py` | Setup wizard |
| `history_window.py` | Dictation history viewer |
| `splash.py` | Splash screen |

### Plugins (plugins/commands/)
| File | Purpose |
|------|---------|
| `audio_switch.py` | "switch to speakers" — NirCmd device switching |
| `web_shortcuts.py` | "go to youtube" — config-driven URL bookmarks |
| `tab_finder.py` | "where is github" — browser tab search |
| `macros.py` | "going dark" — multi-step workflows |
| `quick_ask.py` | "ask claude" — IPC to ARC for AI queries |

## Command Matching

The CommandMatcher (`samsara/command_registry.py`) uses token-based
longest-match semantics. At startup it loads all built-in commands and
plugins, tokenizes every phrase, sorts by token count descending, and
freezes the registry.

Example: user says "find tab github"
- Tokenized: ["find", "tab", "github"]
- Matcher checks 3-token phrases first, then 2-token, then 1-token
- "find tab" (2 tokens, plugin) matches before "find" (1 token, built-in)
- Remainder: "github"

This eliminates all prefix collisions by design.

## Phonetic Wash

Between Whisper output and the CommandMatcher sits a correction layer
(`samsara/phonetic_wash.py`) that fixes known misrecognitions:

- Phrase corrections: "fine tab" → "find tab", "get hub" → "github"
- Word corrections: "mike" → "mic" (per-token, whole-word only)
- Punctuation stripping: "refresh page." → "refresh page"
- Symbol-to-word: "." → "period" (when entire text is a symbol)

All corrections use word boundaries to prevent substring corruption.

## Speech Detection

Samsara uses Silero VAD (Voice Activity Detection) for real-time speech
detection in wake word mode. Unlike RMS-based volume thresholds, VAD
distinguishes human speech from fan noise, ambient hum, and other
non-vocal sounds regardless of volume level.

Falls back to RMS-based detection if torch is not available.

## Threading Model

- Audio callback: sounddevice callback thread
- Transcription: new daemon thread per utterance
- Command dispatch: inline on transcription thread
- UI updates: marshalled via root.after(0, ...)
- Whisper calls serialized by model_lock
