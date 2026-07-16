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
                                    ├─ Built-in → registered JSON command handler
                                    ├─ Plugin → handler(app, remainder)
                                    └─ No match → return to sleep without pasting
```

## State Machine (4 States)

The current entry paths share one audio engine but have distinct ownership and
delivery rules:

| Entry path | Trigger | Behavior |
|-------|---------|----------|
| Hold-to-dictate | Hold configured dictation hotkey | Captures until release plus a bounded speech-aware tail, then transcribes and pastes |
| Continuous | Toggle configured Continuous hotkey | Transcribes speech segments until toggled off |
| HANDS FREE | Toggle configured voice-control button | Buffers ordinary speech across pauses; exact commands run in place; sole-utterance "end" pastes the complete thought and remains active |
| Wake command | "Jarvis" while the listener is enabled | Opens a bounded command window without changing the active recording mode |
| Target wake profile | Configured phrase such as "activate Claude" | Focuses the target and starts that profile's staged or send-on-word workflow |

## Key Modules

### Core
| File | Purpose |
|------|---------|
| `dictation.py` | DictationApp orchestration and entry-point coordination |
| `commands.json` | Built-in JSON voice command definitions |
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
| `main_window_qt.py` | Main hub for History, Dictionary, and Settings |
| `settings_qt.py` | Settings window and command-library UI |
| `listening_indicator.py` | Always-on-top listening-state overlay |
| `wake_word_debug_qt.py` | Wake detection trace and diagnostics |
| `first_run_wizard_qt.py` | Setup wizard |
| `history_qt.py` | Dictation history viewer |
| `splash_qt.py` | Startup splash screen |

### Plugins (plugins/commands/)
| File | Purpose |
|------|---------|
| `audio_switch.py` | "switch to speakers" — device switching (requires NirCmd in `tools/` or on `PATH`) |
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

Samsara uses the Silero VAD (Voice Activity Detection) ONNX model bundled
with faster-whisper for real-time speech detection in wake word mode. It is
loaded locally through ONNX Runtime: startup never downloads VAD code or
model files. Unlike RMS-based volume thresholds, VAD distinguishes human
speech from fan noise, ambient hum, and other non-vocal sounds regardless of
volume level.

A dedicated model instance is serialized across Samsara's audio threads.
Loading or inference failure falls back to RMS-based detection.

## Threading Model

- Audio capture: PortAudio's callback writes frames to the shared ring and
  does no transcription or command work.
- Audio consumers: dedicated daemon poll threads read independent ring cursors;
  completed utterances are handed to tracked worker threads for transcription.
- Command dispatch: runs inline on the utterance-processing worker so spoken
  actions retain their order.
- UI: one persistent `samsara-qt` thread owns `QApplication` and every Qt
  widget. Other threads marshal UI work through `qt_runtime.post()` and
  `QTimer.singleShot`.
- Whisper calls are serialized by `model_lock`.
