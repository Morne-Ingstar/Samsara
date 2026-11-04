# Releasing Samsara

## Asset layout from 0.40.4 (1003)

The version release contains exactly `SamsaraSetup-<version>.exe`,
`Samsara-Windows-v<version>.zip`, `SamsaraSetup-<version>.exe.sha256`, and
`Samsara-Windows-v<version>.zip.sha256`. The two sidecars keep automatic updates
working for 0.40.3 and older clients. Upload all optional packs, their `.sha256`
files, and `manifest-<version>.json` / `manifest-latest.json` to one fixed
release tagged `addons`, titled **Samsara add-ons**, normal release and never
Latest. Do not remove or rename any asset on 0.40.3 or older releases.
The layout below supersedes the historical 0.40.3 asset list in this document.
1002 owns the CI upload guards; tag-triggered CI must not replace signed local
assets. Its deliberate CI upload path must follow this two-place allowlist
before being used for 0.40.4+; this task does not change workflows.

1. Create `addons` once, explicitly excluding it from Latest:
   `gh release create addons --title "Samsara add-ons" --notes "Optional component downloads." --latest=false`.
   Keep existing version releases untouched. Copy previously verified packs
   to `addons` once, under their original filenames, with their checksum files.
2. Build and sign the app and installer; assemble the final portable ZIP.
   Keep all final files in one artifacts directory. Generate the catalog
   **after** signing; installer bytes must not change after their hash is taken.
   A provisional local `manifest.json` can supply installer component sizes
   before signing. Re-run the generator afterward for publication.
3. Run the following, substituting the final directory, version and commit.
   Omit `--reuse-manifest` on the first build with entirely new packs.
   A prior manifest explicitly approves its available optional packs for reuse;
   verify runtime compatibility before supplying it. Absent CUDA stays unavailable
   unless a local matching CUDA ZIP exists or the approved manifest supplies it.

   ```powershell
   python tools/gen_release_manifest.py --artifacts-dir release/final --tag v0.41.0 --app-version 0.41.0 --git-sha <full-commit> --reuse-manifest release/previous-manifest.json --output release/final/manifest.json --publish-plan release/final/upload-plan.json
   ```

4. Read `upload-plan.json`. Upload only missing pack/checksum names from
   `addons_release.files` to `addons`. Compare existing names by size/hash;
   never overwrite an immutable pack with different bytes. Changed packs get
   a new versioned filename. The generator writes sidecars for local packs;
   reused remote packs retain their already-uploaded sidecars. Stage and verify
   all referenced packs before uploading `manifest-0.41.0.json`. Refresh
   `manifest-latest.json` only after that catalog is complete. Only the latest
   alias is routinely replaced; keep versioned catalogs stable.
5. Upload exactly the four `version_release.files` to the version draft:
   installer, portable ZIP, and their two generated `.sha256` sidecars. Do not
   upload manifests, `upload-plan.json`, pack ZIPs or pack sidecars there.
   Verify the resulting list matches the plan exactly.
6. Verify a fresh download of each app artifact against its manifest `sha256`
   and `size_bytes`, plus Components downloads from `addons`, before publication.
   Keep the existing explicit pre-release/Latest promotion decisions below.

No upload is performed by the generator. The release runner follows the plan.
The generator writes both app sidecars from the same final hashes recorded in
the manifest. At tag v0.40.3, `samsara/updater.py` requests the asset selected by
`samsara/update_installer.py:asset_name` plus `.sha256`: the `.exe.sha256` for a
per-user installed copy, or `.zip.sha256` for a portable copy. Older clients can
therefore keep updating automatically. Clients from 0.40.4 verify the manifest
hash without fetching sidecars. Historical component downloads remain valid
because their release assets are preserved.

## CPU build: signed locally, verified by CI

Release files are built and code-signed locally. CI must not touch GitHub
Release assets automatically. A tag push runs verification and stores build
outputs as workflow artifacts only; it does not create or update a Release.
Both release workflows require an explicit `workflow_dispatch` input
`upload_to_release=true` before uploading, and stop if the target Release
already has a `SamsaraSetup-*.exe` asset. Never replace the locally signed
installer or its accompanying release files with CI outputs.

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
   `Samsara-Windows-v0.41.0.zip`.
5. A matching `Samsara-Windows-<version>.zip.sha256` sidecar is generated from
   the final archive. Build outputs are uploaded as workflow artifacts only.

The `.github/workflows/release.yml` workflow has no automatic trigger. A
maintainer must manually dispatch it with the successful build run ID, release
tag, and `upload_to_release=true` to attach verified CI files. Both workflows
check for an existing installer before upload and keep any created release as a
draft with `make_latest: false`. Publication remains a separate release
decision.

This is the CI-traceability piece SignPath Foundation's code-signing program
asks for: a build a third party can point at and confirm came from this
repo's source at this commit, not from a laptop.

### Triggering a release

Tagging v0.41.0 verifies a CI build; it does not alter a GitHub Release.

```
git tag v0.41.0
git push origin v0.41.0
```

After a deliberate asset upload and the release gates below pass, the runner
publishes the draft as a pre-release without selecting Latest:

```
gh release edit <tag> --draft=false --prerelease --latest=false
```

Only on the owner's explicit word, promote the published pre-release to a full
release and select Latest:

```
gh release edit <tag> --prerelease=false --latest
```

Replace `<tag>` with the release tag (for example, `v0.41.0`). These commands are
separate manual decisions and must not be added to either automated workflow.
See the [GitHub CLI release edit reference](https://cli.github.com/manual/gh_release_edit).

Or run the workflow manually (Actions tab -> Release Build (CPU, SignPath-ready) -> Run
workflow) to sanity-check a build without cutting a tag. Leave
`upload_to_release` unchecked; the run stores workflow artifacts only.

### Where artifacts land

- Every run: a `Samsara-Windows-<version>.zip` workflow artifact and matching
  `.zip.sha256` sidecar (Actions run page -> Artifacts), retained 30 days.
- Every run: a `smoke-log-<version>` artifact with the smoke check's
  `samsara.log`, retained 14 days — read this first if a run looks off.
- Tag pushes: build outputs stay in the Actions workflow artifacts; no Release
  asset is created or modified.

Historically, for v0.40.3, the draft asset list was `SamsaraSetup-0.40.3.exe`,
`Samsara-Windows-v0.40.3.zip`, `Samsara-GestureControl-v0.40.3.zip`,
`Samsara-WakeWord-Models-v0.40.3.zip`, their matching `.sha256` sidecars,
and `manifest.json`. See [installation](Docs/INSTALL.md) for the newcomer path.

## Updater release gate

v0.22.1 is the updater bootstrap release. Because v0.22.0 has no updater,
v0.22.1 remains a manual download and extraction. Packaged Windows builds from
v0.22.1 onward may install a later release in-app; source launches and explicit
`SAMSARA_HOME_DIR` isolation profiles do not update the application.

Before publishing every updater-compatible release:

1. Produce `Samsara-Windows-<tag>.zip` and `SamsaraSetup-<version>.exe`, then
   record their final sizes and SHA-256 in the add-ons manifest (core component
   and `release_assets`, respectively). Verify fresh downloads against those
   fields and the matching `.sha256` sidecars generated for legacy clients.
   Installed per-user copies use the installer; never replace an installed folder with the
   portable ZIP. All-users installs require a manual installer update.
2. Prove the release is **in-place compatible** with the current portable-folder
   updater. The exact app/sidecar names and bound manifest are the updater's compatibility
   signal. If a release requires a clean install, change the update protocol
   and asset naming before publishing it; never expose an incompatible archive
   under the names the existing updater accepts.
3. Exercise updates from 0.40.3 and the oldest supported updater build, as well
   as 0.40.4+ clients, using the applicable sidecar or manifest verification, including
   clean shutdown, replacement at the same installation path, relaunch, and
   rollback after a forced startup failure.
4. Confirm the per-user profile/configuration is unchanged, custom commands and
   drop-in command plugins were backed up/migrated, and only the ten allowlisted
   CUDA DLLs were preserved from the old runtime tree.
5. Confirm an opted-out packaged app and every source launch make no update
   request. When enabled, checks go directly to GitHub Releases and disclose
   only ordinary request metadata; no Samsara profile content is uploaded.

The manifest SHA-256 detects a damaged or mismatched archive. It is not code
signing and does not protect against compromise of the release-publishing
account. Do not describe it as providing either guarantee.

## CUDA add-on

The application release remains the CPU ZIP. CUDA packs live on `addons`.
Approve a compatible prior pack with `--reuse-manifest` rather than copying
1 GB to each version release. New built packs retain a versioned filename;
the generator checks app/tag identity before enabling a new CUDA pack.
The previously verified ten-DLL pack (ctranslate2 4.7.1) has SHA-256
`5dc752c89ca4e6ad777b545907a7e654471ce3dfe10a3d96bbfc705386db335d`
(1,128,045,243 bytes). Installation and verification steps live in
`Docs/CUDA.md`.

### Release checklist step: verify the CUDA catalog entry

Before announcing, confirm `cuda-pack.available` is true in the versioned
add-ons manifest and its URL points to the verified pack on `addons`. Check
size/hash and a clean extraction before approving a reused pack. If no pack is
verified for this runtime, leave it unavailable; do not hand-edit availability.

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
- **Publisher concurrency group** (`release-${{ inputs.version }}`)
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
