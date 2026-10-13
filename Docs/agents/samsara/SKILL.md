---
name: samsara
description: Use Samsara's MCP tools to check public status, speak a short reply, or type approved text for a Windows user who controls their PC by voice.
compatibility: Windows; Samsara running with the agents pack enabled; local HTTP MCP client.
---

# Samsara

Samsara is a Windows voice-control and dictation app for people who find typing
or fine mouse work painful. The user may not be looking at the screen. This skill
is optional and Samsara never installs it into another app's folders.

<!-- STAND-IN COPY (564): all prose in this document awaits the owner's wording. -->

## Connect on this PC

In Samsara, open Settings > Tools > Command library > Command packs and turn on
**Let AI apps use Samsara**.
**Copy connection details** copies the URL and Authorization header together.
The default URL is `http://127.0.0.1:47832/mcp`; use the copied port if customized.
**New token** invalidates the old token immediately; update your client afterward.
Keep the copied token private; do not include it in replies, logs or reports.
Copying details asks Windows to keep them out of clipboard history and cloud
sync; native exclusion is unverified. They are cleared after 60 seconds if
Samsara is still running and the clipboard still contains that copy.

Claude Code, using the copied token in place of `<token>`:

```powershell
claude mcp add --transport http samsara http://127.0.0.1:47832/mcp --header "Authorization: Bearer <token>"
```

After **Copy connection details**, this PowerShell block connects Claude Code
without manually inserting the token:

```powershell
$samsaraDetails = (Get-Clipboard -Raw).Trim() -split '\r?\n'
if ($samsaraDetails.Count -ne 2 -or $samsaraDetails[0] -notmatch '^http://127\.0\.0\.1:[0-9]+/mcp$' -or $samsaraDetails[1] -notmatch '^Authorization: Bearer [A-Za-z0-9_-]{40,128}$') { throw 'Copy connection details from Samsara first.' }
claude mcp add --transport http samsara $samsaraDetails[0] --header $samsaraDetails[1]
```

Codex CLI/IDE: set `SAMSARA_AGENT_TOKEN` in the environment of the process that
launches Codex, then add this to `~/.codex/config.toml`:

```toml
[mcp_servers.samsara]
url = "http://127.0.0.1:47832/mcp"
bearer_token_env_var = "SAMSARA_AGENT_TOKEN"
default_tools_approval_mode = "writes"
```

For a PowerShell launch, the environment assignment is:

```powershell
$env:SAMSARA_AGENT_TOKEN = '<token>'
```

Hermes, in `~/.hermes/config.yaml`, then `/reload-mcp`:

```yaml
mcp_servers:
  samsara:
    url: "http://127.0.0.1:47832/mcp"
    headers: {Authorization: "Bearer <token>"}
```

The endpoint requires a Bearer token, rejects browsers' Origin headers, and
accepts only a loopback Host header. It is off by default and closes when the
switch is off or Samsara quits.

<!-- # STAND-IN COPY (565): typing prose awaits the owner's wording. -->
**Let AI apps type for me** is a second switch under the agents pack, off by
default. Turning the pack off clears this switch. While off, `type_text` is
absent from `tools/list` and calls to it are refused.

## Tools

- `get_status()` returns only `app_version`, `listening`, `current_mode`,
  `feedback_level`, `speech_muted`, and `ava_route` (`off`, `local` or `cloud`).
  Listening means microphone capture or a continuous/wake listener is active;
  it does not report whether a connected agent is ready. No text, titles or keys.
- `speak(text)` accepts 1–400 characters of plain text, without markup or control
  characters. Brief and Full speak through Samsara's coordinator. Silent returns
  `suppressed` / `silent`, with no speech; mute returns `suppressed` / `muted`.
  Completed speech returns `spoken` / `completed`; accepted pending speech returns
  `queued` / `coordinator`. Missing voice or UI, coordinator suppression and rate
  limits return a reason. A queued result is acceptance, not proof of playback.
  At most one accepted utterance every two seconds and three pending utterances.
  Pending slots expire after 60 seconds even if interrupted speech never reports
  completion; switching the pack off resets the slots. During a typing question
  and for three seconds afterward, `speak` returns `busy` and speaks nothing.
- `type_text(text)` accepts 1–2,000 characters of plain text. Internal newlines
  and carriage returns are refused everywhere, along with other control/invisible
  characters and markup; they are never stripped. Delivery uses only the
  clipboard, never typed injection or typed fallback; an unsafe/unavailable
  clipboard returns `refused_delivery`. It cannot press Enter or send. One request may await approval for
  at most 30 seconds; a second returns `busy` immediately and is never queued.
  Samsara asks who wants to type how many words into which app, without reading
  the text. The spoken question must finish and its half-second echo tail clear
  before the answer window opens; an unspoken question times out. Say yes using Samsara's Command-only key (default Ctrl+Alt+C).
  One approval permits one paste into the same window and process; a focus or
  process change cancels it. Decline, timeout, disabled switches and unsafe
  targets paste nothing. Terminals, elevated windows, Samsara's own windows and
  lock/UAC desktops are refused before asking. UIA `IsPassword` fields are
  refused. Missing, failed or timed-out UIA verification returns
  `refused_unverified`; a provider incorrectly reporting a password as ordinary
  text still cannot be identified. UIA is checked on a worker with a 350 ms limit. Unknown integrity/desktop state is refused too.
  The result contains `status` (`delivered` or `refused`) and a closed reason:
  `delivered`, `refused_terminal`, `refused_elevated`, `refused_own_window`,
  `refused_password`, `refused_unverified`, `refused_delivery`, `user_declined`, `timeout`, `window_changed`, `too_long`,
  `invalid_text`, `disabled`, or `busy`. `delivered` means injection accepted;
  it does not prove the editor displayed the text. Logs contain only the tool,
  outcome, reason and length bucket, never the text or window title.

## Etiquette

Check `get_status` first. If Samsara is unavailable, answer in text and stop.
Speak only a finished result, a blocker, or a question the user wants to hear.
Use one or two short sentences. Keep code, paths, tables, URLs and long text in
the written reply. Never read private text aloud unless the user requested it.
Respect Silent, mute and refusals; do not retry or rephrase around them. Do not
loop speech or narrate progress. The user's stop command can interrupt speech.
Ask for information in one short spoken question; these tools do not return dictation.
Agent speech uses Ava's voice: never impersonate the user's own messages or
system warnings.
Dictated names can be misheard; clarify ambiguous or destructive targets.

<!-- # STAND-IN COPY (565) -->
Before `type_text`, ask the user in chat and name the destination. Call only
when the user has asked for that text to be placed there. Never use it to send
messages, run commands, or fill credentials. The user's spoken yes is required
every time, even after chat approval. A refusal is final; never retry around it.

## Protocol and sources

Dual-era MCP: current **2026-07-28** HTTP requests use per-request metadata,
`MCP-Protocol-Version`, `Mcp-Method`, and (for calls) `Mcp-Name`; `server/discover`
advertises capabilities. Older clients use `initialize`, which returns
**2025-11-25**, followed by `notifications/initialized`. Supporting both lets
handshake clients use the tools while newer clients use stateless discovery.
Responses are plain JSON; there are no SSE streams. Legacy `initialize` returns
an optional `Mcp-Session-Id` header binding its sanitized clientInfo name for one
hour (at most 64 identities); echo it on calls. Missing/expired identity is
"An AI app", never the last app that initialized. Modern requests carry their
own clientInfo metadata. Client-supplied names are labels, not verified identity.
Each inbound request has a two-second deadline, including headers and body.
At most eight connections run concurrently; a full connection budget is closed
without queuing another worker. A local program can exhaust that budget and
delay agent access. A typing call can wait 30 seconds for its spoken answer.

Verified 13 October 2026:
[MCP versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[legacy lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[Claude Code](https://code.claude.com/docs/en/mcp),
[Codex](https://learn.chatgpt.com/docs/extend/mcp),
[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp).
