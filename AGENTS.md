# AGENTS.md — Samsara-dev (read by Codex/Code before any task)

Samsara is a Windows voice-control and dictation app for people who cannot easily type (PySide6, Python). Owner: Morne, solo developer, chronic finger-joint pain. Accessibility is a hard constraint: minimise typing, favour voice and large tap targets.

## Non-negotiables
- Branch is `feature/v0.22` (verify with `git rev-parse --abbrev-ref HEAD` before and after). Never checkout/switch/merge/rebase/stash/reset. Do not commit unless the prompt says so; commits use `git commit -F <tempfile>`.
- The app is usually RUNNING while you work. Never close it. Never edit `C:\Users\Morne\.samsara\config.json`. Never `import dictation` in a live process or at module level in tests — it attaches a second log handler to the live log. Tests that need the app use `SAMSARA_HOME_DIR` pointed at a temp copy.
- Python: `F:\envs\sami\python.exe`. One pytest process at a time. Never run the full suite unless the prompt's GATE is the full suite; targeted files only (`-q -p no:cacheprovider`).
- Touch only the files the prompt's ALLOWED line names. Another session may be editing other files in this tree at the same time; do not "helpfully" stage, format or fix them, and do not stage anything you did not change.
- If the repository or a referenced file does not match what the prompt describes, STOP and report exactly what differs. Never create, stub or rewrite files to make a gate pass.
- No new dependencies. Qt windows go through `samsara/ui/qt_runtime.py` (post(), hide-on-close, no own QApplication); run `tools/check_qt_discipline.py` after UI changes. Theme tokens come from `samsara/ui/theme.py`.
- Live log for symptoms: `C:\Users\Morne\.samsara\logs\samsara.log` (never project-root .log files).

## Where things are
- `dictation.py` — the app (large; extraction in progress). `samsara/session_modes.py` — hands-free session lanes, switch/exit words, DispatchOutcome. `samsara/commands.py` + `command_registry.py` + `plugin_commands.py` — registry, matcher, executor, DispatchResult. `samsara/execution_policy.py` — the one choke point before side effects. `plugins/commands/*.py` — the hands. `samsara/audio_engine/` — capture, VAD, wake, device recovery. `samsara/ui/` — Qt.
- Docs: `Docs/` (capital D) — ARCHITECTURE.md, EXECUTION_POLICY.md, DEMO.md, VOICE_COMMANDS.md, HANDS_FREE_*.md. Product direction: `C:\Users\Morne\Documents\Claude\SAMSARA_VISION.md` and `SAMSARA_MAP.md`. Demo rehearsal: `tools/demo_rehearsal.py` → `perf_artifacts/demo_rehearsal.md`.

## Report format
Echo every REPORT label from the prompt verbatim, including `branch before/after` and `not done`. Reports are reconciled against the prompt as a checklist and against `git status --porcelain`; omissions are treated as failures.
