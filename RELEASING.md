# Releasing Samsara

## CPU build: CI-built and CI-verifiable

Pushing a version tag (`v*`) triggers `.github/workflows/release.yml` on a
`windows-latest` GitHub-hosted runner:

1. Checkout, Python 3.11, `requirements.txt` (including the pinned CPU ONNX
   Runtime used by Silero VAD), and pinned PyInstaller.
2. `python -m PyInstaller --clean --noconfirm scripts\samsara.spec` with
   `INCLUDE_CUDA` unset — CPU-only configuration.
3. A trimmed, blocking smoke check (`tools\ci_smoke.py`) launches the frozen
   EXE and requires the explicit startup-complete marker with no unexplained crash. See
   "Why the smoke check is trimmed" below.
4. `dist\Samsara\*` is zipped as `Samsara-Windows-<version>.zip` (same
   naming convention v0.20.0 shipped:
   `release\release_staging\Samsara-Windows-v0.20.0.zip`).
5. The zip is uploaded as a workflow artifact (every run, including manual
   `workflow_dispatch` runs) and, on a tag push, attached to a GitHub
   Release for that tag via `softprops/action-gh-release`.

This is the CI-traceability piece SignPath Foundation's code-signing program
asks for: a build a third party can point at and confirm came from this
repo's source at this commit, not from a laptop.

### Triggering a release

```
git tag v0.22.0
git push origin v0.22.0
```

Or run the workflow manually (Actions tab -> Release (CPU build) -> Run
workflow) to sanity-check the build without cutting a tag — this uploads
the artifact but does not create a GitHub Release.

### Where artifacts land

- Every run: a `Samsara-Windows-<version>.zip` workflow artifact (Actions
  run page -> Artifacts), retained 30 days.
- Every run: a `ci-smoke-log-<version>` artifact with the smoke check's
  `samsara.log`, retained 14 days — read this first if a run looks off.
- Tag pushes only: the zip is also attached to the GitHub Release for that
  tag.

## CUDA packaging

v0.22 publishes only the verified CPU zip. `build_release.bat` is also a CPU
build-and-smoke helper; it creates a local `.7z`, not a CUDA pack and not the
canonical GitHub artifact. `scripts\build_cuda.bat` remains a developer helper
for experimenting with `INCLUDE_CUDA=1`, but it is not a release pipeline and
its output must not be advertised or attached until it has an equivalent
clean-checkout build, smoke, archive, and artifact-verification path.

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
- **A warm Hugging Face model cache.** `WhisperModel(...)` loads (and, on a
  fresh machine, downloads from Hugging Face Hub — the default `model_size`
  is `base`) *before* `"[INIT] Startup complete."` is logged. On a fresh CI
  runner this is a real network call of unknown duration on the very first
  runs. The release gate still requires the startup marker; a timeout is a
  failure. Increase the bounded workflow timeout if model-host latency proves
  consistently insufficient rather than accepting a build that never boots.
- **A GUI session.** Windows Actions runners do run with an interactive
  session (unlike Linux runners needing Xvfb), so `customtkinter`/Qt window
  creation is expected to work — but this is unverified on this specific
  app until a real CI run confirms it. See the risk list below.

The `release.yml` smoke step is blocking. `tools/ci_smoke.py` allowlists only
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
  `ci-smoke-log-*` artifact first.
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
- **`softprops/action-gh-release` and `actions/*` are pinned to major-version
  tags** (`@v5`, `@v6`, `@v2`), not exact commit SHAs. Standard practice, but
  stricter supply-chain pinning (exact SHA) is a future hardening option,
  not done here.
- **Concurrency group** (`release-${{ github.ref }}`) cancels an in-flight
  run if the same tag/ref is pushed again — intentional, but means a retry
  push while a build is running kills the first one rather than queuing
  behind it.

## Future: SignPath signing hook point

Once the CPU build has a few clean CI runs behind it, SignPath code signing
slots in after the "Package CPU build" step and before "Attach to GitHub
Release": upload the unsigned zip (or just `Samsara.exe`) to SignPath's
API, wait for the signed artifact, and use *that* in the Release/artifact
upload steps instead. Not wired up yet — this workflow is the
prerequisite (verifiable, CI-built binary) SignPath's process needs before
that conversation can start.
