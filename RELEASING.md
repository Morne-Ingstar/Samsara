# Releasing Samsara

## CPU build: CI-built and CI-verifiable

- Before tagging, run `python tools\probes\inject_matrix.py --gate` on the machine that will produce the build.

Pushing a version tag (`v*`) triggers `.github/workflows/release-build.yml` on a
`windows-latest` GitHub-hosted runner:

1. Checkout, Python 3.11, `requirements.txt` (including the pinned CPU ONNX
   Runtime used by Silero VAD), and pinned PyInstaller.
2. `python -m PyInstaller --clean --noconfirm scripts\samsara.spec` with
   `INCLUDE_CUDA` unset — CPU-only configuration.
3. A trimmed, blocking smoke check (`tools\ci_smoke.py`) launches the frozen
   EXE and requires the explicit startup-complete marker with no unexplained crash. See
   "Why the smoke check is trimmed" below.
4. `dist\Samsara\*` is zipped as `Samsara-Windows-<tag>.zip`, for example
   `Samsara-Windows-v0.40.0.zip`.
5. A matching `Samsara-Windows-<version>.zip.sha256` sidecar is generated from
   the final archive. The ZIP and sidecar are uploaded as workflow artifacts
   and, on a tag push, attached to a draft GitHub Release alongside the
   installer, optional components and manifest.

After that build succeeds, `.github/workflows/release.yml` consumes its verified
artifacts through `workflow_run`; it does not build a second application.
Publication remains a separate release decision.

This is the CI-traceability piece SignPath Foundation's code-signing program
asks for: a build a third party can point at and confirm came from this
repo's source at this commit, not from a laptop.

### Triggering a release

```
git tag v0.40.0
git push origin v0.40.0
```

Or run the workflow manually (Actions tab -> Release Build (CPU, SignPath-ready) -> Run
workflow) to sanity-check the build without cutting a tag — this uploads
the artifact but does not create a GitHub Release.

### Where artifacts land

- Every run: a `Samsara-Windows-<version>.zip` workflow artifact and matching
  `.zip.sha256` sidecar (Actions run page -> Artifacts), retained 30 days.
- Every run: a `smoke-log-<version>` artifact with the smoke check's
  `samsara.log`, retained 14 days — read this first if a run looks off.
- Tag pushes only: the ZIP and mandatory SHA-256 sidecar are also attached to
  the draft GitHub Release for that tag.

For v0.40.0, the draft asset list is `SamsaraSetup-0.40.0.exe`,
`Samsara-Windows-v0.40.0.zip`, `Samsara-GestureControl-v0.40.0.zip`,
`Samsara-WakeWord-Models-v0.40.0.zip`, their matching `.sha256` sidecars,
and `manifest.json`. See [installation](Docs/INSTALL.md) for the newcomer path.

## Updater release gate

v0.22.1 is the updater bootstrap release. Because v0.22.0 has no updater,
v0.22.1 remains a manual download and extraction. Packaged Windows builds from
v0.22.1 onward may install a later release in-app; source launches and explicit
`SAMSARA_HOME_DIR` isolation profiles do not update the application.

Before publishing every updater-compatible release:

1. For portable copies, produce the exact `Samsara-Windows-<tag>.zip` name and its mandatory
   `.zip.sha256` sidecar from the final CI archive. Verify a fresh download
   against that sidecar before publishing.
   Installed per-user copies require `SamsaraSetup-<version>.exe` and its own
   `.exe.sha256` sidecar instead; never replace an installed folder with the
   portable ZIP. All-users installs require a manual installer update.
2. Prove the release is **in-place compatible** with the current portable-folder
   updater. The exact ZIP plus sidecar names are the updater's compatibility
   signal. If a release requires a clean install, change the update protocol
   and asset naming before publishing it; never expose an incompatible archive
   under the names the existing updater accepts.
3. Exercise the update from the oldest supported updater build, including
   clean shutdown, replacement at the same installation path, relaunch, and
   rollback after a forced startup failure.
4. Confirm the per-user profile/configuration is unchanged, custom commands and
   drop-in command plugins were backed up/migrated, and only the ten allowlisted
   CUDA DLLs were preserved from the old runtime tree.
5. Confirm an opted-out packaged app and every source launch make no update
   request. When enabled, checks go directly to GitHub Releases and disclose
   only ordinary request metadata; no Samsara profile content is uploaded.

The SHA-256 sidecar detects a damaged or mismatched archive. It is not code
signing and does not protect against compromise of the release-publishing
account. Do not describe it as providing either guarantee.

## CUDA add-on

The application release remains the reproducible CI-built CPU zip. v0.22 reuses
the existing `Samsara-CUDA-Pack-v0.20.0.zip` because its ten DLLs are
byte-for-byte identical to the current tested CUDA environment. Its published
SHA-256 is
`5dc752c89ca4e6ad777b545907a7e654471ce3dfe10a3d96bbfc705386db335d`.
Installation and verification steps live in `Docs/CUDA.md`.

Do not copy or package an installed Samsara directory: a user's extracted CUDA
archive or other local files may be nested inside it. `build_release.bat` is a
CPU build-and-smoke helper; `scripts\build_cuda.bat` remains a developer helper,
not a release artifact producer. Any future CUDA runtime change needs a new
archive, hashes, clean extraction test, and frozen-app smoke before publication.

## Why the smoke check is trimmed

The local pre-release gate, `tools\build_and_smoke.cmd` ->
`tools\frozen_smoke.py`, runs 11 checks across two scenarios (normal boot +
liveness, first-run wizard path). Most of it assumes things a stock CI
runner does not reliably have:

- **A real audio input device.** `dictation.py`'s `__init__` unconditionally
  opens a PortAudio/WASAPI input stream. If a runner has no mic, the
  exception is caught and logged (`logger.exception("[ACE] Engine failed to
  start: ...")`) and the app keeps booting — but `logger.exception()` always
  writes a literal `Traceback (most recent call last):` line regardless.
  `frozen_smoke.py` treats *any* `"Traceback"` substring as a hard failure,
  so on a mic-less runner it would report FAIL for a run that's actually
  fine. `tools\ci_smoke.py` knows about this one specific caught-exception
  pattern and treats it as a warning, not a failure — everything else
  matching `Traceback`/`CRITICAL` still fails the check.
- **A warm Hugging Face model cache.** The model factory resolves cached
  snapshots locally first; an absent or unusable speech model downloads from
  Hugging Face Hub. The seed `model_size` is `base`; setup can recommend a
  different size. Loading happens *before* `"[INIT] Startup complete."` is logged. On a fresh CI
  runner this is a real network call of unknown duration on the very first
  runs. The release gate still requires the startup marker; a timeout is a
  failure. Increase the bounded workflow timeout if model-host latency proves
  consistently insufficient rather than accepting a build that never boots.
- **A GUI session.** Windows Actions runners do run with an interactive
  session (unlike Linux runners needing Xvfb), so `customtkinter`/Qt window
  creation is expected to work — but this is unverified on this specific
  app until a real CI run confirms it. See the risk list below.

The `release-build.yml` smoke step is blocking. `tools/ci_smoke.py` allowlists only
known, caught hardware-availability tracebacks; an unexplained traceback,
critical error, or early process exit fails the release job. The smoke log is
uploaded on every run for inspection.

`tools\frozen_smoke.py` and `tools\build_and_smoke.cmd` remain the full local
gate to run before every tagged release. `build_release.bat` first refuses to
run if either a frozen `Samsara.exe` or this checkout's source `dictation.py`
is active, if tracked/staged source differs from the commit, or if a required
runtime/release-tool file is still untracked; it never force-kills the user's
running app. Unrelated untracked user artifacts are preserved and tolerated.
The PyInstaller spec derives project data from Git's tracked-file manifest so
ignored or untracked demo assets, downloads, and bytecode cannot leak into the
package.

The local wrapper uses `SAMSARA_PYTHON` when set, otherwise the known
development environment when present, then a `python`/`py -3.11` executable
from `PATH`. Every candidate is executed and validated before any cleanup or
build begins.

## Assumptions a first real CI run should confirm

- **GUI/window creation succeeds headless.** Unverified: whether
  `customtkinter`/Tk and the Qt-based overlays (`PySide6`, pulled in via
  `samsara.ui.numbers_overlay_qt` / `workflow_capture_qt`) initialize
  cleanly on a `windows-latest` runner's session. If not, `ci_smoke.py`
  will report a genuine (not benign-pattern) crash — check the uploaded
  `smoke-log-*` artifact first.
- **The bundled VAD asset and ONNX Runtime both reach the frozen app.** A
  successful boot must log the local Silero ONNX load rather than the RMS
  fallback. The spec explicitly collects `faster_whisper/assets`; its
  `onnxruntime` hidden import activates PyInstaller's runtime hook.
- **The defensive torch/torchaudio exclude remains useful.** They are no
  longer runtime dependencies, but a local CUDA-pack build may have torch
  installed as a DLL source. `build_release.bat` now relies on the spec's
  excludes and no longer performs post-build deletion. `release.yml` keeps
  an additional CI-only cleanup and logs a warning if it removes anything;
  if that warning fires on a real run, the spec's exclude list deserves a
  closer look.
- **Build size has grown well past the v0.20.0 baseline.** The
  `Samsara-Windows`/`~292MB` figure from v0.20.0 no longer reflects the
  current tree — a local build of the current `master` produces a
  `dist\Samsara` around **698MB** uncompressed (PySide6, pyarrow, mediapipe,
  scipy, onnxruntime, etc. are now much larger contributors). Not a CI
  problem (GitHub's runner disk comfortably fits it), just don't be
  surprised the CI-built zip is much bigger than the old release notes
  imply — update `docs`/release notes size figures once a real CI zip size
  is known.
- **Version identity is enforced before build.** The release tag,
  `samsara.__version__`, and `samsara.smart_actions_bridge.SAMSARA_VERSION`
  must agree or `tools/check_release_version.py` fails the workflow.
- **`softprops/action-gh-release` is pinned to an exact commit SHA** in both
  release workflows. Several `actions/*` dependencies still use major-version tags.
- **Publisher concurrency group** (`release-${{ github.event.workflow_run.head_branch }}`)
  queues runs for the same tag (`cancel-in-progress: false`).

## Code signing policy and release notes

Include this section in every release's notes (use an absolute URL on GitHub):

```markdown
### Code signing policy

See the [Code signing policy](https://github.com/Morne-Ingstar/Samsara/blob/master/CODE_SIGNING_POLICY.md)
for signing status, SignPath.io and SignPath Foundation attribution, team roles,
and the privacy policy. Check the individual assets' signatures; older unsigned
releases remain unsigned.
```

The package version generates Windows PE resources for `Samsara.exe` and
`samsara-mcp.exe`. The installer reads its numeric version from the built app and
its full product text version from AppVersion. CI checks both application PE
resources before smoke or packaging. Company/publisher is `Samsara`.

Signing integration remains pending verification of the SignPath action's full
commit SHA and artifact schema. The checked-in workflow retains its disabled
signing stub; do not describe its outputs as signed. See [Release CI](Docs/RELEASE_CI.md)
for the intended two-stage process and activation steps.
