# Releasing Samsara

## CPU build: CI-built and CI-verifiable

Pushing a version tag (`v*`) triggers `.github/workflows/release.yml` on a
`windows-latest` GitHub-hosted runner:

1. Checkout, Python 3.11, CPU-only `torch`/`torchaudio` (from
   `download.pytorch.org/whl/cpu` — never the CUDA index), `requirements.txt`,
   pinned PyInstaller.
2. `python -m PyInstaller --clean --noconfirm scripts\samsara.spec` with
   `INCLUDE_CUDA` unset — CPU-only configuration.
3. A trimmed, non-blocking smoke check (`tools\ci_smoke.py`) launches the
   frozen EXE and watches for a clean boot or an unexplained crash. See
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

## CUDA pack: still a local, manual step

`Samsara-CUDA-Pack-<version>.zip` is **not** built on CI this pass. It's
built locally via `build_release.bat`, which sets `INCLUDE_CUDA=1` so
`scripts\samsara.spec` copies cuDNN/cuBLAS/cuDART DLLs out of the local CUDA
torch install (`F:\envs\sami\...\torch\lib`). A GitHub-hosted runner has no
GPU and no reason to carry a CUDA torch install, so there's nothing to
harvest those DLLs from without a second, much heavier job. See the
commented-out section at the bottom of `release.yml` for the two options
when this becomes worth doing (pull a CUDA torch wheel on a dedicated CI job
vs. keep it a manual local step forever). For now: build the CUDA pack
locally as usual and attach it to the tag's GitHub Release by hand after CI
creates it.

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
  runs; `ci_smoke.py` treats "still alive, no crash, just hasn't reached the
  boot marker yet within the timeout" as a pass rather than a failure.
- **A GUI session.** Windows Actions runners do run with an interactive
  session (unlike Linux runners needing Xvfb), so `customtkinter`/Qt window
  creation is expected to work — but this is unverified on this specific
  app until a real CI run confirms it. See the risk list below.

The `release.yml` smoke step is blocking. `tools/ci_smoke.py` allowlists only
known, caught hardware-availability tracebacks; an unexplained traceback,
critical error, or early process exit fails the release job. The smoke log is
uploaded on every run for inspection.

`tools\frozen_smoke.py` and `tools\build_and_smoke.cmd` are **unchanged** —
they remain the full local gate to run before every tagged release.

## Assumptions a first real CI run should confirm

- **GUI/window creation succeeds headless.** Unverified: whether
  `customtkinter`/Tk and the Qt-based overlays (`PySide6`, pulled in via
  `samsara.ui.numbers_overlay_qt` / `workflow_capture_qt`) initialize
  cleanly on a `windows-latest` runner's session. If not, `ci_smoke.py`
  will report a genuine (not benign-pattern) crash — check the uploaded
  `ci-smoke-log-*` artifact first.
- **`torch`/`torchaudio` CPU-index resolution.** The workflow force-installs
  from `download.pytorch.org/whl/cpu` before `requirements.txt`, relying on
  `requirements.txt`'s bare (unpinned) `torchaudio` line to accept the
  already-installed CPU build rather than pip re-resolving a different
  version. If a future `requirements.txt` edit adds a version pin to
  `torch`/`torchaudio`, confirm a matching CPU wheel exists at that pin.
- **PyInstaller torch/torchaudio exclude isn't airtight.** `scripts\samsara.spec`
  excludes `torch`/`torchaudio` from `Analysis`, but `build_release.bat`
  (the existing local packaging script) still defensively deletes
  `_internal\torch(audio)` post-build — implying PyInstaller has, at least
  once, bundled them anyway despite the exclude. `release.yml`'s "Verify
  build output" step mirrors that same defensive cleanup and logs a warning
  if it actually had to remove anything; if that warning fires on a real
  run, the exclude list in the spec deserves a closer look.
- **Build size has grown well past the v0.20.0 baseline.** The
  `Samsara-Windows`/`~292MB` figure from v0.20.0 no longer reflects the
  current tree — a local build of the current `master` produces a
  `dist\Samsara` around **698MB** uncompressed (PySide6, pyarrow, mediapipe,
  scipy, onnxruntime, etc. are now much larger contributors). Not a CI
  problem (GitHub's runner disk comfortably fits it), just don't be
  surprised the CI-built zip is much bigger than the old release notes
  imply — update `docs`/release notes size figures once a real CI zip size
  is known.
- **Version identity must agree in three places.** Keep the release tag,
  `samsara.__version__`, and `samsara.smart_actions_bridge.SAMSARA_VERSION`
  on the same value before tagging; the workflow names artifacts from the tag.
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
