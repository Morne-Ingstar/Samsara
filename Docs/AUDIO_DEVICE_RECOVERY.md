# Microphone loss and recovery

When the input device disappears mid-session, the audio engine rebuilds the
stream on its own. This note records how it resolves the device, because
getting that wrong is what broke recovery on 2026-09-12.

## What went wrong

The engine detected the loss correctly and entered recovery immediately, but
every attempt failed the same way:

```
[ACE] Input stream died unexpectedly -- entering recovery
[ACE] Opening stream: device='Analogue 1 + 2 (Focusrite USB Audio)' ...
[ACE] Recovery attempt failed, retrying in 2s: Multiple input devices found for
      'Analogue 1 + 2 (Focusrite USB Audio)': [.. MME] / [.. Windows DirectSound]
... 30 identical attempts ...
[ACE] Recovery gave up after 60s -- device never reappeared
```

The device had never gone anywhere. Recovery passed sounddevice a **bare
device name**, and that name existed under three host APIs (MME, DirectSound,
WASAPI) — an ambiguous name is not resolvable, so sounddevice raised rather
than picking one. Boot never hit this because it opens the **index** that
`samsara.audio_devices.list_microphones()` reconciled (`device=30` in the log).

After give-up the engine stopped trying entirely, so every later hold recorded
`frames=0` and the user got a failure earcon with no explanation.

## How it resolves now

`samsara/audio_engine/device_resolver.py` owns the rules:

1. **Identity is captured at open.** Every successful open records
   `(device_name, hostapi_name)` — never just the name.
2. **Recovery resolves that pair back to a live index** and opens the index.
3. **If the recorded host API is gone** but the name exists under another one,
   it takes the best available and logs the substitution:
   `[ACE] Recovered on a different backend -- host API changed Windows WASAPI -> MME`.
   USB re-enumeration moves devices between backends; that is normal.
4. **Preference order is WASAPI first**, then MME, DirectSound, WDM-KS, ASIO —
   the same preference `audio_devices.list_microphones()` applies when it builds
   the list you pick from, so recovery lands on the backend boot chose.
5. **Nothing is ever opened by bare name.** With no recorded identity it falls
   back to the configured index (or the system default), never a name.

## Retry policy

| Phase | Interval | Duration |
|---|---|---|
| Fast recovery | every 2 s | 60 s |
| Background probe | every 10 s | indefinitely |

After the 60 s window the engine sets **`device_lost`** and keeps probing
forever with a cheap `query_devices()` enumeration — it never opens a stream
until the name is actually back. A device that returns five minutes later
reconnects on its own; before this, only a restart or re-picking the mic from
the tray would recover it.

`engine.request_reconnect_now()` forces an immediate attempt (this is what a
"Reconnect microphone" tray action calls) without waiting out the interval.

All of this runs on the `ace-recovery` daemon thread. The detection callback
returns immediately, so neither boot nor the Qt thread ever blocks on it.

## `device_lost`

`AudioCaptureEngine.device_lost` is True from give-up until a successful
reopen. It exists so the app layer can do the three things the engine must not
do itself:

- show a persistent "mic lost" state on the listening indicator,
- play the failure earcon **once** per loss rather than once per attempt,
- refuse hold/wake capture with an explanation instead of silently recording
  `frames=0`.

It is cleared automatically by any successful reopen, including one from the
background probe.

## Extending it

Host API preference lives in `HOSTAPI_PREFERENCE` at the top of
`device_resolver.py`. Add or reorder entries there; matching is a
case-insensitive substring test against the PortAudio host API name, and an
unrecognised backend still works — it just sorts last.
