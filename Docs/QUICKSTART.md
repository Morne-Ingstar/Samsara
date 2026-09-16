# Quick Start

## Download and run on Windows

You need Windows 10 or 11, a microphone, and internet access for the initial
application and model downloads. The packaged app includes Python. A
compatible NVIDIA GPU is optional; transcription speed and memory needs
depend on the model and your hardware.

1. Open the [latest public release](https://github.com/Morne-Ingstar/Samsara/releases/latest).
2. Download **`Samsara-Windows-<version>.zip`**, not GitHub's automatically
   generated source-code archives.
3. Extract the ZIP and run **Samsara.exe** from the extracted folder.
4. Follow the setup wizard to select your microphone and download a
   transcription model.

The current public release is
[v0.22.1](https://github.com/Morne-Ingstar/Samsara/releases/tag/v0.22.1).
[v0.23.0-beta.1](https://github.com/Morne-Ingstar/Samsara/releases/tag/v0.23.0-beta.1)
is a separate tester download. Both publish an application ZIP and a SHA-256
checksum file; choose the release that matches the behavior you want to test.

The standard application ZIP supports CPU transcription. For a compatible
NVIDIA GPU, follow the [CUDA add-on instructions](CUDA.md).

## Your first dictation

Open a blank note and put the cursor in its text area. With the default
binding, **hold Ctrl+Shift, speak, then release**. Your transcription is
inserted into the focused text box.

If holding a key is uncomfortable, configure **Hands Free** or wake-word
listening in Settings. Quick Reference shows your current hotkeys and spoken
controls; the setup wizard and your configuration can change them.

In the public v0.22 hands-free workflow, speech can be buffered across pauses.
Say **“end”** as a whole utterance to paste the current thought and keep the
session open. Check your version's in-app guidance and
[release notes](../CHANGELOG.md) for beta changes to session controls.

## Try a command

Enable wake words, then say your configured wake phrase followed by a command.
The default wake phrase is **“Jarvis”**.

| Try saying | What it does |
| --- | --- |
| “Jarvis, scroll down” | Scrolls the active window. |
| “Jarvis, show numbers” | Labels available interactive controls for voice targeting. |
| “Jarvis, new tab” | Sends the new-tab shortcut to the focused app; try it in a browser. |
| “Jarvis, volume up” | Raises system volume. |

Command packs, foreground apps and optional integrations affect availability.
Explore the in-app command reference and enable the packs you need in
**Settings → Commands**. For more examples, see the
[voice command reference](VOICE_COMMANDS.md).

Samsara also remains available in the system tray. Closing its main window
hides it to the tray; double-click the tray icon to reopen it.

## Optional Ava and spoken replies

Ava can use an Ollama model on your computer. Install and configure the local
model separately; it is not required for basic Whisper dictation.

Windows speech voices work locally. Edge TTS uses an online speech service.
Cloud Ava requires a supported provider and your API key.
See [privacy and network features](../PRIVACY.md) before enabling online options.

## Updates

Automatic update checks are **off by default**. In a packaged build, use
**Check for Updates**, say **“check for updates”**, or explicitly enable
periodic checks. Source launches do not use this updater.

A check contacts GitHub Releases; there is no always-connected update channel.
See [what the request sends](../PRIVACY.md#what-can-use-the-network).

The in-app installer verifies the release ZIP against its published SHA-256,
closes Samsara, and replaces application files. Your user profile stays
outside the application folder. The updater preserves the supported optional
CUDA DLLs and backs up/migrates installation-local custom commands and
drop-in command plugins.

v0.22.1 introduced the updater. Users of v0.22.0 need to download and extract
v0.22.1 manually first. SHA-256 checks verify file integrity, not the identity
of a code signer.

## From source

For development on Windows, use **Python 3.11**, the version used by the
project's CI and release build. Run these commands in PowerShell:

```powershell
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dictation.py
```

The development checkout may differ from a packaged release. Do not launch a
second copy against a profile already in use by Samsara. Read
[AGENTS.md](../AGENTS.md) before using a coding agent, and
[the architecture guide](ARCHITECTURE.md) for the code layout.

For source-installation GPU libraries, see
[CUDA setup for source environments](CUDA.md#source-environments).

## App-specific wake profiles

Advanced wake-profile settings live in your user
`%USERPROFILE%\.samsara\config.json`. Close Samsara before editing that file.
This fragment illustrates two targets; merge it with your configuration
rather than replacing the whole file:

```json
{
  "wake_profiles": [
    {
      "phrase": "activate claude",
      "target_process": "claude.exe",
      "mode": "focus_dictate",
      "send_word": "over"
    },
    {
      "phrase": "activate hermes",
      "target_process": "Hermes.exe",
      "mode": "stage_send",
      "send_word": "send"
    }
  ]
}
```

Use process names for applications installed on your machine.

- **focus_dictate** types into the target and presses Enter on that profile's
  send word.
- **stage_send** buffers speech until its send word, then inserts the text
  without pressing Enter.

Give each profile its own distinct send word. A target that should leave text
staged must not share the submit phrase of one that sends automatically.

## Next steps

- [Voice commands](VOICE_COMMANDS.md) and [hands-free modes](HANDS_FREE_MODES.md)
- [Custom commands and Python plugins](CUSTOM_COMMANDS.md)
- [Privacy and local storage](../PRIVACY.md)
- [Report a bug or share feedback](https://github.com/Morne-Ingstar/Samsara/issues/new/choose)
