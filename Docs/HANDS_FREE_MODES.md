# Hands-free modes: the three phrases

While a hands-free session is running, three phrases move you between lanes.
Each one must be the **whole utterance** — say it alone, with nothing before or
after it.

| Say this | Lane | What it is for |
|---|---|---|
| **"command mode"** | COMMAND | Voice commands only. Nothing you say gets typed. |
| **"dictate mode"** | DICTATE | Everything you say is staged and typed. |
| **"ava mode"** | AVA | Everything you say goes to the assistant. |

Also accepted: `"dictation mode"` and a bare `"dictate"` for DICTATE, and a
hyphenated form of any of them (`"ava-mode"`).

## Whole utterance only

A mode phrase buried in a sentence is **not** a switch — it is just words:

- "ava mode" → switches to AVA.
- "we should use ava mode later" → stays dictation, types normally.

This is deliberate. An earlier build let `"Ava <anything> Mode"` switch lanes as
a prefix, and ordinary dictation containing those words hijacked the session
mid-sentence (2026-07-18). Bare `"ava"` is likewise **not** a switch word — it
is too ordinary a word to spend on a lane change.

## The Ava invocations are separate, and configurable

Besides `"ava mode"`, Ava can be reached by any phrase in the
`ava_invocations` config list. Both routes land in exactly the same place.

The defaults are `"hey ava"`, `"so ava"` and `"oracle"`. To change them, edit
`ava_invocations` in your config file (there is no settings-UI widget for it
yet). Keep `"ava mode"` **out** of that list — it is a switch word now, and
listing it in both places would give one phrase two different entry paths.

> **Quick Reference never hardcodes these.** The in-app Quick Reference window
> reads the switch words from `samsara/session_modes.py`'s switch table and the
> invocations from your live config on every open, so whatever you configure is
> what it shows.

## What happens to text you had staged

Switching away from DICTATE while a thought is staged **commits that thought**
(it is typed into your target window) before the lane changes. This is the same
for all three phrases — `"ava mode"` behaves exactly like `"command mode"`.

If you did not want it delivered, say `"scratch that"` before switching.

## If Ava will not start

If Ava cannot answer — the plugin is switched off, or the backend is not
reachable — the switch is **refused**: you get the error earcon, the reason is
written to the log at WARNING, and you stay in the lane you were already in.
A refused switch never leaves you in a lane that cannot respond.
