---
name: samsara
description: Use Samsara's MCP tools to check its public status or speak a short reply to a Windows user who controls their PC by voice.
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
switch is off or Samsara quits. This version has no typing tool.

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
  completion; switching the pack off resets the slots.

## Etiquette

Check `get_status` first. If Samsara is unavailable, answer in text and stop.
Speak only a finished result, a blocker, or a question the user wants to hear.
Use one or two short sentences. Keep code, paths, tables, URLs and long text in
the written reply. Never read private text aloud unless the user requested it.
Respect Silent, mute and refusals; do not retry or rephrase around them. Do not
loop speech or narrate progress. The user's stop command can interrupt speech.
Ask for information in one short spoken question; v1 cannot listen for an answer.
Agent speech uses Ava's voice: never impersonate the user's own messages or
system warnings.
Dictated names can be misheard; clarify ambiguous or destructive targets.

## Protocol and sources

Dual-era MCP: current **2026-07-28** HTTP requests use per-request metadata,
`MCP-Protocol-Version`, `Mcp-Method`, and (for calls) `Mcp-Name`; `server/discover`
advertises capabilities. Older clients use `initialize`, which returns
**2025-11-25**, followed by `notifications/initialized`. Supporting both lets
handshake clients use the two tools while newer clients use stateless discovery.
Responses are plain JSON. There are no SSE streams or protocol sessions.
Each inbound request has a two-second deadline, including headers and body.
Requests run one at a time: several slow connections from a local program can
delay agent speech. The deadline bounds each accepted connection, not queue time.

Verified 13 October 2026:
[MCP versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[legacy lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[Claude Code](https://code.claude.com/docs/en/mcp),
[Codex](https://learn.chatgpt.com/docs/extend/mcp),
[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp).
