# The five-minute take

SAMSARA_VISION.md section 7: the product is shipped when one five-minute
take works end to end with no hands. This page is that take as a checklist
a human can follow, plus the rehearsal that checks it against the app itself.

The current rehearsal table lives in `perf_artifacts/demo_rehearsal.md`.
Re-generate it after every change; it is the gap between today and the demo.

## The take (fixed script -- do not paraphrase it away)

Preconditions: Samsara running, hands-free (command-mode toggle) armed,
Spotify, Warp, Claude and Obsidian installed, two monitors.

| # | say | expect |
|---|-----|--------|
| 1 | "wake up samsara" | hands-free opens (earcon, indicator shows the session) |
| 2 | "play something from my alternative rock playlist" | media starts -- note which path played it (music.py / media_keys / stremio) |
| 3 | "put warp on the left screen and claude on the right" | two windows placed on two monitors |
| 4 | "tell claude the mode switch fix landed and ask what's next" | text dictated into the Claude app and sent |
| 5 | a 40-word paragraph, into Obsidian (the rehearsal's `PARAGRAPH`) | appears verbatim |
| 6 | "correct that" + fix one word by voice | the correction applies on screen |
| 7 | "scratch that" | last action undone |
| 8 | "go to sleep" | hands-free closes |

What the rehearsal knows about each line today is in the table; the short
version of the workarounds, for a human doing the take by hand:

* Hands-free enters in the **dictate** lane. Say "command mode" before
  lines 2-4, and "dictate" (or start line 5 with the word "dictate") before
  the paragraph; finish the paragraph with "end" to commit it. "focus
  obsidian" works inside either lane.
* Line 8 works as soon as "go to sleep" is in `command_mode.abort_phrases`
  in config.json (the built-in exit phrases are "stop listening", "exit
  hands free", "exit command mode").
* Lines 1, 3, 4 and 6 have no owner yet -- the rehearsal names the plugin
  that would own each.

## Running the rehearsal

Dry run (default; nothing is executed, Samsara may stay running):

```
F:\envs\sami\python.exe tools\demo_rehearsal.py
```

Writes `perf_artifacts/demo_rehearsal.md` and prints it. Every line of the
take is resolved through the real layers: `samsara.session_modes` control
words (abort phrases, "scratch that", switch words, Ava invocations, the
hands-free reserved-command probe), the command registry
(`samsara.command_registry.CommandMatcher` built from the live registry --
the same rows `tools/dump_command_metadata.py` prints), and the argument
grammar the resolved plugin actually implements. Status per line:

* **WORKS** -- resolves to a command whose arguments the plugin can honour,
  in the lane the session would actually be in.
* **PARTIAL** -- resolves, but needs a mode switch, an extra utterance, or a
  fallback path that is not quite the intent (a Spotify search instead of a
  playlist, SMTC "play" instead of choosing music, "end" to commit).
* **MISSING** -- no command, grammar or capability owns it; the table names
  the plugin or module that would.

Options: `--catalog dump.json` rehearses against a saved
`tools/dump_command_metadata.py` output (what the tests do); `--config
path` reads another config.json (wake phrases, abort phrases, packs,
music_library); `--out path` changes the report path.

Live run (owner only):

```
F:\envs\sami\python.exe tools\demo_rehearsal.py --live
```

Refuses unless Samsara is running and the console is interactive. Then, one
step at a time with a 3 s pause: the resolvable command lines (status WORKS
or PARTIAL with a command) are executed through the real `CommandExecutor`
and the execution policy (a destructive command still asks first), and the
outcome chip the app would show is printed with PASS/FAIL; the lines that
belong to the app's ears (wake, the paragraph, the correction, sleep) are
announced for you to speak to the running Samsara. Do not run it from a
script or CI.

Tests: `tests/test_demo_rehearsal.py` (a fixture catalog covering WORKS /
PARTIAL / MISSING, the lane model, the grammars, determinism, the --live
refusal, and the rule that the tool never imports dictation.py).
