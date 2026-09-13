# Release Build CI (draft)

`.github/workflows/release-build.yml` is a draft companion to the existing
`.github/workflows/release.yml`. It targets the same CPU build described in
[RELEASING.md](../RELEASING.md), but adds two things `release.yml` doesn't
have yet:

1. Every `uses:` action pinned to a full commit SHA instead of a mutable
   major-version tag (`release.yml`'s own doc comment calls this out as "a
   future hardening option, not done here" -- see RELEASING.md's assumptions
   list).
2. An explicit, disabled stub for where SignPath Foundation code signing
   would slot in once an account exists, plus a couple of extra headless-safe
   smoke assertions (below).

It has not replaced `release.yml` and has not been run. This document exists
so a maintainer can review, then either fold it into `release.yml` or run it
standalone via a real tag/`workflow_dispatch`.

## What the workflow does

On a push of a tag matching `v*.*.*`, or a manual `workflow_dispatch`:

1. Checks out the repo, sets up Python 3.11 with pip caching keyed on
   `requirements.txt`.
2. Runs `tools/check_release_version.py` (fails the build if
   `samsara.__version__`, `samsara.smart_actions_bridge.SAMSARA_VERSION`, and
   the pushed tag disagree).
3. Installs `requirements.txt` plus pinned PyInstaller `6.20.0`.
4. Builds via `python -m PyInstaller --clean --noconfirm scripts\samsara.spec`
   with `INCLUDE_CUDA` unset -- the same CPU-only configuration
   `tools/build_and_smoke.cmd` produces locally.
5. Runs the headless-safe smoke checks (see below), blocking on failure.
6. Zips `dist\Samsara\*` as `Samsara-Windows-<version>.zip` and generates its
   `.sha256` sidecar with PowerShell's `Get-FileHash` (see the manifest note
   below).
7. Uploads the zip + sidecar, and the smoke log, as workflow artifacts.
8. On a tag push only: creates a **draft** GitHub Release with the zip and
   sidecar attached (`draft: true`, always -- this workflow never publishes a
   release). `release.yml` then runs via `workflow_run`, only if this
   workflow succeeded, downloads this run's artifact and attaches that same
   zip (it no longer builds its own).
9. Has a disabled `if: false` SignPath signing step, commented with the exact
   inputs it would need, sitting between packaging and the draft-release
   step.

### Note on `tools/release_manifest.py`

That module provides `tracked_tree_datas()`, which `scripts/samsara.spec`
uses to build its PyInstaller `datas` list from Git's tracked-file manifest.
It does not generate a SHA-256 sidecar or any other release manifest -- there
is no such helper in the file. The `.zip.sha256` sidecar is produced inline
with PowerShell's `Get-FileHash`, matching what `release.yml` already does.

## Where the wake-word models come from

The nine OpenWakeWord files the build bundles (`alexa_v0.1.onnx`,
`embedding_model.onnx`, `hey_jarvis_v0.1.onnx`, `hey_mycroft_v0.1.onnx`,
`hey_rhasspy_v0.1.onnx`, `melspectrogram.onnx`, `silero_vad.onnx`,
`timer_v0.1.onnx`, `weather_v0.1.onnx`) are **not in the `openwakeword`
wheel** and not in this repository. The package downloads them into
`site-packages/openwakeword/resources/models` the first time the app runs
(`openwakeword.utils.download_models()`), which is why a developer machine
has them and a fresh CI runner never does -- `scripts/samsara.spec`'s
`collect_data_files(...)` then quietly collects nothing, and that is how the
v0.23.0-beta.1 CI ZIP (run 34774339039) shipped 0 of 9 and failed the
workflow's own OWW check. Since 2026-09-13 `tools/release_preflight.py`
pins the nine files to the upstream `dscripka/openWakeWord` **v0.5.1**
release assets by SHA-256 (`OWW_MODELS`); `release-build.yml` runs
`python tools\release_preflight.py --fetch-oww-models` before PyInstaller
(download only what is missing, verify every byte, never a silent skip),
the spec itself now raises if any of the nine is absent, the local preflight
(`build_release.bat` -> `release_preflight.py`) verifies presence + hashes,
and `release.yml` -- now triggered by `workflow_run` on this workflow and
gated on its success -- re-opens the downloaded ZIP and checks the nine
files again before attaching. What fails if they are missing: the fetch step
(bad hash or unreachable URL), else the spec (`SystemExit` naming the files),
else check 7 below on the frozen output, else `release.yml`'s gate; at no
point does a ZIP without wake-word models reach a GitHub Release.

## Smoke checks: 8 of 13 run on CI

`tools/frozen_smoke.py` (called by `tools/build_and_smoke.cmd`) is
RELEASING.md's stated "11 checks across two scenarios" local gate, and
`build_and_smoke.cmd` layers 2 more of its own (OWW model-file presence,
OWW model-load-failure log scan) on top -- 13 checks in total. None of that
code was modified; this workflow reuses `tools/ci_smoke.py` (already written
to be the CI-safe subset) as the blocking gate, then adds a few more of the
13 as simple log-text/file-existence assertions layered on top of the same
log `ci_smoke.py` already captures.

| # | Check | Runs on CI here? | Why |
|---|-------|-------------------|-----|
| 1 | Boot marker (`[INIT] Startup complete.`) reached within timeout | Yes | `tools/ci_smoke.py`'s core check |
| 2 | No unexplained crash (Traceback/CRITICAL), with mic/GPU-absent tracebacks allowlisted | Yes | `tools/ci_smoke.py`'s `KNOWN_BENIGN_MARKERS` exists specifically for this |
| 3 | Bootloader-level failure caught (e.g. missing frozen dependency) | Yes | `tools/ci_smoke.py` captures and scans stderr for this |
| 4 | Process still alive right after the boot marker | Yes | implied by `ci_smoke.py`'s `"boot"` outcome |
| 5 | `[INIT] Startup complete.` logged exactly once (duplicate-handler regression) | Yes | new step: count-match on the copied log |
| 6 | Bundled Silero ONNX VAD loaded, not the RMS fallback | Yes | new step: marker match on the copied log |
| 7 | Bundled OWW wake-word model files present in `dist\Samsara` | Yes | new step: `Test-Path` on the frozen output, no process launch needed |
| 8 | No `[OWW] Failed to load model` in the log | Yes | new step: text match on the copied log |
| 9 | Log line count stays bounded after boot (no runaway logging loop) | No | needs `frozen_smoke.py`'s specific windowed live-tail algorithm; not reproduced here to avoid an unreviewed re-implementation diverging from it |
| 10 | Exactly one `Samsara.exe` process (no self-respawn) | No | needs `psutil` child-process walking identical to `frozen_smoke.py`'s checker; `frozen_smoke.py` itself isn't CI-safe to run verbatim (see below) |
| 11 | Clean shutdown within a bounded timeout | No | `tools/ci_smoke.py` does call `terminate()` and logs the outcome, but doesn't gate the build on shutdown latency the way `frozen_smoke.py` does |
| 12 | No orphaned `Samsara.exe` processes after exit | No | same `psutil` reasoning as #10 |
| 13 | First-run wizard path survives without crashing | No | same mic/GPU false-Traceback risk as the normal boot path applies to the wizard scenario too, and `frozen_smoke.py`'s wizard checker has no benign-marker allowlist the way `ci_smoke.py` does for the normal path |

**Why not just run `tools/frozen_smoke.py` for the missing 5?** Its
`wait_for_boot()` treats *any* line containing `"Traceback"` or `"CRITICAL"`
as a hard failure, with no allowance for the caught-and-logged
`PortAudioError` (no mic) / GPU-detect (no NVIDIA driver) tracebacks a stock
`windows-latest` runner produces on every single boot -- this is exactly the
false-negative problem RELEASING.md's "Why the smoke check is trimmed"
section describes, and exactly why `tools/ci_smoke.py` exists as a separate,
narrower script in the first place. Reimplementing the missing 5 checks with
`frozen_smoke.py`'s own allowlist-free logic would reintroduce that false
failure; reimplementing them with a *new* allowlist outside that reviewed
script wasn't attempted here, to keep this draft's diff to workflow/doc
files only.

`tools/release_preflight.py` (refuses a *local* build if Samsara is already
running or the checkout is dirty) was not wired in: a fresh Actions runner
checkout is always clean and never has Samsara running, so the check would
always trivially pass and adds no signal on CI.

## SignPath Foundation requirements

Per <https://signpath.org/terms.html> (SignPath Foundation's published
conditions for open-source projects), the three requirements that shaped
this workflow's design:

1. **"The binary is a valid, automated build resulting from the source
   code"** at the reviewed repository, verified technically for each
   release. This is why the workflow builds from a tag push in a
   GitHub-hosted runner with no local/manual build step in the path -- it's
   the "verifiable CI pipeline from the public repo" SignPath's process
   needs to point at.
2. **Every release needs manual approval for signing** (SignPath's own
   review/approval flow, separate from this workflow). This is why the
   signing step is a disabled stub between packaging and the draft release,
   not an automatic call: signing itself happens as a distinct,
   human-gated request once a project is registered, not as an unattended
   CI step.
3. **Source code, including build scripts and CI configuration, is what
   reviewers examine** to grant and keep eligibility. This is why the build
   stays a single straightforward PyInstaller-via-`samsara.spec` path with
   one smoke gate, rather than something reviewers would have to untangle.

(Other eligibility conditions -- OSI-approved license, no proprietary
components, active maintenance, no malware/exploit tooling, MFA for all team
members, published code-signing policy, etc. -- are project/organizational
facts, not workflow design choices, so they aren't reflected in the YAML
itself.)

Once signed, the certificate is issued to **SignPath Foundation**, not to
this project -- Windows SmartScreen and install dialogs will show "SignPath
Foundation" as the publisher, not "Samsara".

## What still has to happen manually

1. **Apply to SignPath Foundation.** Nothing here does this automatically.
   Application review and eligibility (OSI license, no proprietary
   components, published code-signing policy, team roles, MFA) happen
   outside CI, at <https://signpath.org/>. Only after acceptance does an
   `organization-id` / `project-slug` / signing policy exist to fill into the
   commented-out stub step.
2. **Wire up the SignPath action for real** once approved: uncomment the
   stub step, pin `signpath/github-action-submit-signing-request` to an
   exact commit SHA (not done here -- no account exists yet to test against),
   add `SIGNPATH_API_TOKEN` as a repository secret, and repoint the packaging
   step to zip the *signed* output instead of the unsigned one.
3. **Azure Trusted Signing (now "Azure Artifact Signing") as a paid
   fallback**, if SignPath Foundation's review is rejected, too slow, or its
   organizational requirements (MFA for all members, published policy, etc.)
   don't fit: Basic tier is **$9.99/month** for 5,000 signatures/month, one
   certificate profile of each type, $0.005/signature overage
   (<https://azure.microsoft.com/en-us/pricing/details/artifact-signing/>).
   Restricted to US/Canadian/EU/UK businesses as of general availability.
4. **A first real run on `windows-latest`** to confirm the assumptions
   RELEASING.md already flags as unverified for `release.yml` (GUI/window
   creation succeeding headless, the bundled VAD asset reaching the frozen
   app, build size) apply the same way here, since this workflow builds the
   same way.

## First-run steps

1. Review this file's diff against `release.yml` side by side; decide
   whether `release-build.yml` replaces it, runs alongside it, or gets
   discarded once reviewed.
2. Trigger it once via **Actions -> Release Build (CPU, SignPath-ready) ->
   Run workflow** (`workflow_dispatch`, blank version input) on this branch
   or a throwaway branch pushed to the remote -- this builds, smoke-checks,
   and uploads artifacts, but creates no Release (the draft-release step
   only runs `if: github.ref_type == 'tag'`).
3. Inspect the uploaded `smoke-log-*` artifact and the build-size/warning
   output in the "Verify build output" step, same as RELEASING.md's existing
   "Assumptions a first real CI run should confirm" list.
4. Only after a clean `workflow_dispatch` run, push a real tag (e.g.
   `v0.22.1-rc1`) to confirm the draft-release path -- **the release is
   created as a draft**, so nothing goes out until someone opens it on
   GitHub and clicks Publish.
5. Delete the draft release and the tag if it was only a test.
