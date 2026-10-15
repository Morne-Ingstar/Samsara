# Security policy

## Supported versions

Security fixes target the current **0.30.0 beta line**. Use its latest beta;
older beta and stable lines are not maintained for security fixes.
The source version is defined in [samsara/__init__.py](samsara/__init__.py).

## Report a vulnerability privately

**STAND-IN destination: <owner to choose: email or GitHub private advisory>.**
The private reporting route is pending; do not post vulnerabilities, credentials,
recordings or transcripts in public issues. Once the route is chosen, send the
affected version, impact and minimal reproduction with synthetic data.

## Local data and network boundaries

Configuration, corrections, vocabulary and dictation history live in the local
user profile; logs and optional recordings/exports can also contain private data.
See [profile routing](samsara/paths.py), [history storage](samsara/history_store.py)
and [logging](samsara/log.py). Local files are not guaranteed encrypted.

The [intent observer](samsara/intent/shadow.py) stores dictated text locally and
never uploads its log or includes it in diagnostics. Core speech recognition
runs locally. Optional cloud Ava, cloud corrections, remote Ollama, Edge TTS
and integrations can send relevant text/context to their configured services;
see [the privacy policy](PRIVACY.md) and [cloud requests](samsara/cloud_llm.py).
Do not interpret local storage as a promise that optional features never send text.

## Repository guard

Gitleaks checks commits for credentials and private export filenames. Ignore rules
help prevent new files being added; they do not remove tracked files or history.
Existing historical findings need separate review and credential rotation where
applicable. Never include real speech, chat exports or device codes in fixtures.
