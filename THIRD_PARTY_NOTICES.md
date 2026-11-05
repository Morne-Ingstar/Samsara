# Third-Party Notices

This file records attribution for third-party code and ideas incorporated
into Samsara, beyond what's already covered by `requirements.txt`'s own
package licenses.

## Custom wake classifiers (hey Samsara / hey Ava)

The `samsara/wake_models/hey_samsara.onnx` and `hey_ava.onnx` classifier
weights were trained on 2026-11-03 and are distributed under CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). The application source licence
is separate. Attribution, transformations and training-tool notices are in
[`samsara/wake_models/ATTRIBUTION.md`](samsara/wake_models/ATTRIBUTION.md),
with copied LibriSpeech, LibriTTS/Piper voice and openWakeWord notices alongside.
LibriSpeech: Vassil Panayotov (2014), prepared with Daniel Povey, CC BY 4.0.
LibriTTS: Heiga Zen and Google Speech / Google Brain contributors (2019),
CC BY 4.0. The selected Piper en_US-libritts-high voice repository declares
MIT; openWakeWord code/frozen preprocessing uses Apache-2.0. Piper 1.4.2
GPL-3.0 was a training tool only; its code and voice weights are not bundled.

## Rango (architectural acknowledgement, no code copied)

- Project: Rango -- https://github.com/david-tejada/rango
- License: MIT
- Copyright: (c) 2023 David Martinez Tejada
- Reference commit at time of review: `de798d0db94581fe71c2e13572ca85dcedd3fd26`

The concept of numbering interactive page elements with in-page hint labels
for keyboard/voice selection -- used by Samsara's `browser_extension/` DOM
Show Numbers path (see `plugins/commands/show_numbers.py` and
`samsara/browser_bridge.py`) -- is inspired by Rango's general approach.

**No code from Rango is copied or adapted.** Samsara's implementation was
written independently: a smaller, single-file-per-concern vanilla-JS content
script rather than Rango's larger TypeScript hint-generation system, and a
different transport (a local, token-authenticated loopback WebSocket server
hosted inside the already-running Samsara process) rather than Rango's
Talon integration (`rango-talon`), which relays commands via a
clipboard-write + simulated-hotkey + clipboard-poll mechanism that doesn't
fit Samsara's authentication/schema-validation requirements and was
deliberately not adapted.
