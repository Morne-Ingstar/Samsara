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

## Code signing policy and Windows metadata

The [Code signing policy](../CODE_SIGNING_POLICY.md) publishes the intended
attribution, author/reviewer/approver roles, MFA/manual approval requirements,
and [privacy policy](../PRIVACY.md). The README and release-notes template link
it. The website needs the same section before application.

`scripts/version_info.py` reads the package version with AST, without importing
the app, and generates resources for `Samsara.exe` and `samsara-mcp.exe`. These
are the only first-party PE outputs in the current spec. No separate compiled
updater, own DLL or own `.pyd` target exists. Third-party DLLs/extensions and
component ZIPs must not be submitted under Samsara's identity.

ProductName/CompanyName are `Samsara`; FileDescription is `Samsara voice control
and dictation`; copyright includes AGPL-3.0. ProductVersion is the full package
version. Numeric FileVersion/fixed product version is major.minor.patch.counter,
with the prerelease counter or zero for stable versions: `0.30.0-beta.9` becomes
`0.30.0.9`. Numeric resource fields do not define update ordering.

Inno uses `GetVersionNumbersString` on the built app for numeric fields and
AppVersion for its product text version; it has no SignTool. CI checks the two
application PE resources before smoke/packaging. The checker reads every
translated string and fixed version through Windows version.dll without
executing the files.

## Signing status and intended two-stage process

**Integration is blocked pending an authoritative full commit SHA for the v3
SignPath action and validation of the artifact configuration schema.** This
change was prepared offline. The checked-in workflow retains its disabled
signing stub. Adding a token alone does not yet activate signing; current
artifacts must not be described as signed.

The intended process, all on GitHub-hosted `windows-latest` runners:

1. Build and smoke the tagged source. Stage A uploads only `Samsara.exe` and
   `samsara-mcp.exe` using upload-artifact id `upload-unsigned-artifact`.
2. Submit its artifact-id to `signpath/github-action-submit-signing-request`,
   configuration `stage-a`, policy `release-signing`. Wait for manual approval,
   download the result, validate both Authenticode signatures and PE identities,
   and replace only these files in dist. Then build the portable ZIP and installer.
3. Stage B uploads `SamsaraSetup-<version>.exe`, submits its artifact-id with
   configuration `stage-b` and the same policy, waits for a second manual approval,
   validates the result and replaces the installer.
4. Compute SHA-256 sidecars from the final ZIP/installer, then upload those same
   final files as artifacts and draft-release assets. Signing failures must fail
   the job; they must never fall back silently to unsigned output.

`signpath/stage-a.xml` and `stage-b.xml` have ZIP roots because upload-artifact
wraps even a single installer in a ZIP. Their allowlist excludes third-party PE
files. XML well-formedness can be checked offline; SignPath schema validation
is still required before activation.

The guard design computes `SIGNPATH_ENABLED` in job env from the token's presence
and guards every signing upload/request/restore step with that boolean. GitHub
does not allow secrets directly in a step's `if` expression. Without the token,
all signing steps skip and the existing packaging scripts continue. The token
is passed only to the two requests. Organization/project come from repository
variables `SIGNPATH_ORGANIZATION_ID` / `SIGNPATH_PROJECT_SLUG`; no extra enable
variable is needed.

## Foundation requirements and sources

These requirements come from the brief's 2026-10-22 reading of the
[Foundation terms](https://signpath.org/terms) and
[SignPath documentation](https://docs.signpath.io/). They were not re-fetched
in this offline implementation; recheck the primary sources during activation.

- OSI-approved license, active maintenance, prior releases, no proprietary
  components, and functionality described on the download page.
- Own binaries built from own source; Code signing policy on home/download/release
  pages; privacy disclosures; roles/permission links; GitHub and SignPath MFA;
  review of non-committer changes; manual approval of every signing request.
- ProductName `Samsara` and consistent ProductVersion. The certificate is provided
  by SignPath Foundation and the signing service by SignPath.io.
- SignPath GitHub App installed for trusted build-origin verification; all jobs
  before submission on GitHub-hosted runners.

No verified Foundation certificate reputation guidance was available in the
offline material. This change makes no claim about when SmartScreen warnings
disappear and does not assert that current docs are silent. Verify the current
Foundation guidance before promising an outcome.

## Remaining steps, in order

1. Resolve/pin the official v3 action commit, validate both XML configurations
   against SignPath's schema, install the reviewed two-stage workflow and rerun
   its focused gates. This is a code-verification blocker, not an owner account step.
2. The owner approved the policy wording/roles on 2026-10-23; add the same
   **Code signing policy** section to the VPS site's home and download pages.
3. Enable MFA for every GitHub team member and apply at
   [SignPath Foundation](https://signpath.org/) with the
   [repository](https://github.com/Morne-Ingstar/Samsara), policy and privacy links.
4. After approval, enable MFA for every SignPath member; create the project,
   `stage-a` / `stage-b` artifact configurations, a `release-signing` policy
   requiring manual approval, and an API token allowed to submit requests.
5. Install the SignPath GitHub App for this repository's origin verification.
6. Add repository secret `SIGNPATH_API_TOKEN` and variables
   `SIGNPATH_ORGANIZATION_ID` / `SIGNPATH_PROJECT_SLUG` using the account values.
7. Push the next real matching version tag; approve both requests, inspect the
   draft signatures/sidecars, and follow the normal release review/publish process.

The account/API, hosted build, actual signing and reputation behavior have not
been exercised here. Offline fixture packaging checks file routing only; it
cannot establish trusted origin, genuine signatures or real build equivalence.
