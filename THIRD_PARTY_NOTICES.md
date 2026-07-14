# Third-Party Notices

This file records attribution for third-party code and ideas incorporated
into Samsara, beyond what's already covered by `requirements.txt`'s own
package licenses.

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
