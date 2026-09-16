# Privacy and local storage

Samsara's core transcription runs locally. That does not mean every optional
feature is offline, or that no text is stored on your computer.

This page describes the current source and beta/development behavior.
Older packaged releases may not include every feature listed here.

## What stays on your computer

- Whisper transcription and wake-word processing run on your machine.
- Dictionary entries, corrections, configuration, and dictation history are
  stored locally.
- Ava can use an Ollama model running on your own computer. Windows speech
  voices provide local text-to-speech.
- The Task List plugin stores tasks locally. Its former external sync was
  removed in v0.21.1; see the [changelog](CHANGELOG.md). If Edge TTS is
  selected, spoken task confirmations use the same online speech service
  as other spoken text.

The normal user-data folder is **%USERPROFILE%\.samsara**. Development and
test profiles can override it with `SAMSARA_HOME_DIR`. Moving or replacing
the application folder does not, by itself, clear this profile.

History, application logs, saved recordings, and exports can contain
sensitive information. Review what you share in a bug report. Local storage
is not a promise that every file is encrypted or automatically deleted.

## What can use the network

| Feature | Information involved |
| --- | --- |
| **Initial setup and model downloads** | Downloads application components or model files from their hosts. The host receives normal connection metadata. |
| **Cloud Ava** | Sends the prompt and relevant conversation context to the provider you configure, using your API key. |
| **Optional Ava web search** | Sends the question and request context to the configured supported cloud search service when enabled. |
| **Smart Corrections cloud backend or fallback** | Sends text for correction when the cloud path is configured and enabled. Cloud fallback is off by default. |
| **Edge TTS** | Sends the text selected for speech to Microsoft's online speech service. It needs internet but no personal API key. Choose Windows voices for local speech. |
| **Smart Actions bridge** | Sends the request text, session context and observations to the endpoint you configure. That endpoint can be local or remote. |
| **Web searches, links and integrations** | Contact the website, service or network device involved in the requested action. Their own data policies apply. |
| **Packaged-app update checks** | Contact GitHub Releases. GitHub receives ordinary connection metadata, including IP address, request time and user agent. |

An Ollama endpoint configured on another machine is a network service too;
“local model” assumes the endpoint is on your own computer. API providers may
charge for usage independently of Samsara.

Update checks do not send dictation, audio, history, configuration, API keys,
or device names. Automatic checks are off by default; a manual check or an
explicitly enabled periodic check makes an outbound request. Source launches
do not use the packaged-app updater. See [updating Samsara](Docs/QUICKSTART.md#updates).

## The local intent-observation log

In current beta/development builds, `intent.shadow_enabled` defaults to
`true`. After a hands-free DICTATE utterance has been handled as text, an
observer records what the experimental intent resolver would have decided.
This observer does not execute the proposed command.

The default location is:

```text
%USERPROFILE%\.samsara\shadow\intent-YYYY-MM-DD.jsonl
```

Each readable JSONL entry includes the dictated text, proposed interpretation,
timing information, and the focused application's process name. This log does
not contain audio, an audio path, or window titles. Samsara does not upload it
or include it in its diagnostics bundle.

To stop new entries, set `shadow_enabled` to `false` in the existing
`intent` object in your user `config.json`. Preserve the other settings:

```json
{
  "intent": {
    "shadow_enabled": false
  }
}
```

This is a configuration fragment, not a replacement for the whole file.
Close Samsara before editing the file, then reopen it. Disabling the observer
does not delete existing logs. To remove them, close Samsara and delete the
`shadow` folder in your user-data directory. A custom `intent.shadow_dir`
setting changes that location.

For developers, [shadow.py](samsara/intent/shadow.py) defines the recorded
fields, and [shadow_report.py](tools/shadow_report.py) summarizes the logs
locally.

## Plugins and shared material

Python plugins run with Samsara's permissions and can access files or the
network. Only install plugins you trust. A plugin's own behavior is not
changed by selecting local transcription.

Before posting an issue, screenshot, history excerpt or diagnostic file,
check it for private words, personal paths, account details and keys.
