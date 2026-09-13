# Wake Words — Hands-Free Activation

Wake-word listening runs alongside Samsara's recording modes. It is **off by
default**. When enabled, Samsara listens locally for the default wake phrase,
**"Jarvis"**, then accepts a voice command without requiring a keyboard or
mouse.

## Enable It

1. Open **Settings → Modes**.
2. Turn on **Wake word listener**.
3. Leave the displayed wake phrase as **Jarvis**, or configure an advanced
   wake profile as described below.

The default runtime hotkey is **Ctrl+Alt+W**. It toggles the wake listener
without changing Hold, Continuous, or HANDS FREE mode. The hotkey is
configurable in **Settings → Modes → Toggle wake word**.

## Give a Command

Either say the wake phrase and command together:

```text
"Jarvis, new tab"
"Jarvis, scroll down"
"Jarvis, show numbers"
```

Or say them separately:

```text
You: "Jarvis"
Samsara: [ready sound]
You: "open Chrome"
```

By default, Samsara waits up to five seconds for the second utterance. Change
that window under **Settings → Modes → Wake command timeout**. Say
**"nevermind"** as a complete utterance to cancel a pending Jarvis command.

Wake-word commands use the same enabled command packs as other command entry
points. Samsara currently ships with hundreds of built-in commands plus plugin
commands; the searchable list in **Settings → Commands** is authoritative for
your installation and enabled packs.

## Wake Words and Recording Modes

| Feature | Activation | Behavior |
|---|---|---|
| **Hold-to-dictate** | Hold the configured dictation hotkey | Transcribes and pastes when released |
| **Continuous** | Toggle the configured Continuous hotkey | Transcribes speech segments until stopped |
| **HANDS FREE** | Toggle the configured voice-control button | Buffers ordinary speech until the complete utterance "end"; exact commands run between thoughts |
| **Wake listener** | Say "Jarvis" while the listener is enabled | Runs a command without changing the active recording mode |

The wake listener is an independent activation layer, not a fourth dictation
mode. HANDS FREE is the long-running combined command-and-dictation lane.

## Wake phrase opens the hands-free session

By default a wake phrase opens a short one-command window. To have it open
the latched **HANDS FREE** session instead -- armed once, open until you say
"go to sleep" -- set, in `%USERPROFILE%\.samsara\config.json` (close Samsara
first):

```json
"wake_word_config": { "opens_session": true },
"command_mode": { "enabled": true, "mode": "toggle" }
```

The wake phrase then does exactly what a tap of the voice-control toggle
does: same lane (dictate), earcon, indicator badge and inactivity timeout.
Words spoken after the wake phrase in the same breath are not run; start
with the next utterance. The flag needs `command_mode.mode` "toggle" -- with
"hold" there is no latched session, so the flag is ignored (a warning is
logged) and the one-command window is used.

Ending the session: "go to sleep", "samsara sleep" or "sleep now", spoken as
the whole utterance (a sentence that merely contains them is dictated), or
the older "stop listening" / "exit hands free" / "exit command mode". A
sleep phrase keeps any staged, uncommitted dictation and restores it the
next time the session opens. Add your own sleep phrases with
`command_mode.abort_phrases` (a list).

### A custom wake phrase such as "wake up samsara"

Any phrase in `wake_word_config.phrase` is matched against Whisper's
transcript today, so a new phrase works without new software. But only
"jarvis" / "hey jarvis" have a bundled openWakeWord model. Every other phrase
runs without the cheap pre-filter, which means Whisper decodes all room
audio while the listener is on. A dedicated model for a new phrase means
training one with openWakeWord's own training pipeline (not part of this
repository; see `samsara/wake_models/README.txt`). That takes a GPU for a
few hours, synthetic positive clips and negative audio. Loading the
resulting `.onnx` for the main wake phrase also needs a small code change.
Only wake profiles (`wake_profiles[].oww_model`) load a custom model file
today, and `_load_oww_model` takes the main phrase's model from
openWakeWord's bundled set (`samsara/wake_detector.py` `PHRASE_TO_MODEL`).
Wake profiles do not pass through `opens_session`.

## App-Specific Wake Profiles

Advanced wake profiles can focus a particular app and start a targeted
dictation session. The bundled defaults are **"activate Claude"** and
**"activate Hermes"**. Each profile has its own target process, behavior, and
send word:

- `focus_dictate` focuses the target, types the accumulated text, and can send
  it with the profile's send word.
- `stage_send` keeps the text staged and does not press Enter automatically.

Profiles are stored in the `wake_profiles` section of
`%USERPROFILE%\.samsara\config.json`. Close Samsara before manually editing
that file. Keep a backup and do not share API keys or private paths from it.

## Tuning and Troubleshooting

Run **Settings → General → Run Mic Setup Guide** after changing microphones or
moving one. The guide calibrates Samsara to the current device and room.

If the phrase is detected too rarely or too often:

1. Confirm the correct microphone in **Settings → General**.
2. Run the microphone setup guide.
3. Adjust **Settings → Modes → Wake word sensitivity**. Lower values are more
   sensitive; higher values are stricter.
4. Use **tray menu → Wake Word Debug** for the detailed detection trace.

If Jarvis is heard but the following command does not run, check the searchable
command list, its command pack, and the wake-command timeout. Commands are
matched as complete utterances; speak the command itself concisely.

## Privacy and Network Use

Audio capture, wake detection, Whisper transcription, and ordinary command
matching run locally. A command can still invoke an explicitly configured
network feature—for example cloud Ava, Edge TTS, a webhook, or a web search.
Enabling wake-word listening does not itself send audio to a Samsara server.
