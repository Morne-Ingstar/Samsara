# Contributing to Samsara

Samsara is a Windows voice-control and dictation app. Keep accessibility central:
minimize typing and precise mouse work, and make controls usable by voice.

## Set up a checkout

Use Windows, Git, and 64-bit Python 3.11, matching the CI and release workflows.
Run these commands in PowerShell. The explicit virtual-environment interpreter
avoids PowerShell activation-policy changes.

```powershell
git clone https://github.com/Morne-Ingstar/samsara.git
cd samsara
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-test.txt
```

Optional integrations may need additional services or credentials; they are not
needed for every contribution. Never add personal configuration or credentials
to a change.
For help building integrations, packs, or plugins, ask in [#integrations on Discord](https://discord.gg/tPYvf369hQ).

## Run focused tests

Run only the test files or individual tests relevant to your change. Do not run
the whole suite by default. From the repository root, this example checks the
packaged changelog and Home's changelog reader:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_changelog_ships_402.py tests/test_home_qt.py -k changelog -q -p no:cacheprovider
```

Replace the file paths and `-k` expression for your feature, or select one test
with `tests/test_feature.py::test_behavior`. Run one pytest process at a time.
For tests that measure Qt layouts, also set
`$env:QT_QPA_FONTDIR = "$env:WINDIR\Fonts"`; remove it afterwards with
`Remove-Item Env:QT_QPA_FONTDIR -ErrorAction SilentlyContinue`. Ordinary tests
do not need the font override.

`tests/conftest.py` assigns a temporary `SAMSARA_HOME_DIR` before collection and
mocks audio output by default. Keep tests isolated from personal data, real audio
hardware, and external services. Do not import `dictation` at test-module level:
it has application startup side effects. Prefer testing the extracted modules in
`samsara/` with fixtures and mocks.

Put Python tests under `tests/`, in files named `test_<feature>.py`, with
`test_<behavior>` functions. Name new tests by feature rather than work-queue
number. Cover observable behavior and regressions rather than copying the
implementation. Use `tests/fixtures/` for reusable synthetic input.

## Run from source

Use a microphone and an interactive Windows desktop. Clear the test-only Qt
overrides, then launch the entry point with a separate development profile:

```powershell
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
Remove-Item Env:QT_QPA_FONTDIR -ErrorAction SilentlyContinue
$env:SAMSARA_HOME_DIR = Join-Path ([System.IO.Path]::GetTempPath()) 'samsara-source-profile'
.\.venv\Scripts\python.exe dictation.py
```

This profile persists between launches until the temporary folder is removed;
it keeps development settings and logs separate from your normal profile.
Initial setup may need to download a speech model. Optional AI features have
their own setup. Use the explicit interpreter above rather than workstation
launcher scripts.

## Submit a change

Keep changes focused, preserve unrelated work, and describe the behavior that
changed and the exact focused tests you ran. Include screenshots for UI changes.
Use theme tokens from `samsara/ui/theme.py` and route Qt windows through
`samsara/ui/qt_runtime.py`. After a UI change, run:

```powershell
.\.venv\Scripts\python.exe tools/check_qt_discipline.py
```

For future commits, use a short imperative subject of at most 72 characters,
such as `Fix changelog lookup in packaged builds`. Put the reason, details, and
validation in the body when needed. See [CHANGELOG.md](CHANGELOG.md) for release
history; preserve existing release wording.
