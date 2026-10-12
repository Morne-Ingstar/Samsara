---
name: samsara-voice
description: Respond accessibly to a Hermes user speaking through Samsara voice dictation on Windows.
---

# Samsara voice

This is stand-in wording for the owner's review. This skill is optional: install it
using your own Hermes skill installation workflow. Samsara never installs files
into Hermes's folders for you.

Samsara is a Windows voice-control and dictation application for people who find
typing or fine mouse work painful. Its optional Hermes pack lets a user speak to
their own configured Hermes from the application they are working in, and keep
dictating while Hermes thinks. Hermes keeps its own tools and approval settings.

The user is speaking through voice dictation. Names, punctuation and instructions
may be misheard. If a name or a destructive instruction is ambiguous, ask a short
clarifying question before acting. Do not guess the target of a destructive act.

Your reply may be read aloud. Lead with the answer in one or two short sentences.
Use plain spoken language. Put tables, code blocks, long paths and detailed lists
after the first paragraph. Keep details available: Samsara can read the reply in
pages. Never ask the user to type or click; ask them to tell you the information or
use a voice command. Do not claim that every reply is spoken: Silent shows a chip,
Brief adds a short sound, and Full reads the first page aloud after capture ends.

The Command-only key (default Ctrl+Alt+C) gives these commands to Samsara:

- `ask Hermes <message>` or `tell Hermes <message>` sends one background turn.
- `new Hermes session` or `new conversation with Hermes` starts fresh.
- `new Hermes session with high effort` selects a listed effort for the new session.
  The levels are none, minimal, low, medium, high, xhigh, max and ultra.
- `stop Hermes` stops the running turn and keeps the conversation.
- `read the rest` pages the last reply; `repeat that` repeats the last page.

Only one Hermes turn runs at a time. A busy submission is refused, not queued.
There is no Samsara session list, session switching or staged-draft forwarding.
Hermes sessions created here use source `samsara`, so the user can find them later
in Hermes. The pack is off by default and requires explicit notice acceptance.
