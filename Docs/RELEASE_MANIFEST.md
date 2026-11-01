# Release manifest (schema version 1)

From 0.40.4, the fixed `addons` GitHub release (title **Samsara add-ons**,
normal release, never Latest) holds packs, their checksums, and
`manifest-<version>.json` plus `manifest-latest.json`. Each version release
holds only `SamsaraSetup-<version>.exe`, `Samsara-Windows-v<version>.zip`,
and their `.exe.sha256` / `.zip.sha256` sidecars for older updaters.
Keep every asset on releases through 0.40.3 unchanged for older clients.
`manifest.json` remains the local build/installer input and cache filename;
it is no longer uploaded to version releases.

Components and installer fetch mode try
`https://github.com/Morne-Ingstar/Samsara/releases/download/addons/manifest-<own-version>.json`.
Only HTTP 404 falls back to `manifest-latest.json` at the same location.
Components downloads the exact pack URL in the manifest, including a pack
originally built for a prior version. The updater requests the offered
version's catalog and additionally requires its `app_version`, `release_tag`,
asset URL and size to match the offered update. A fallback catalog for another
version is refused. Invalid manifests and other HTTP errors fail closed.

Producer: `tools/gen_release_manifest.py`, run by `release-build.yml` after
the build, from the actual built artifacts. Consumer: `samsara/components.py`
(fetch, validate, list, download one component with verification). Both are
covered by `tests/test_release_manifest.py` and `tests/test_component_fetch.py`.

## Top level

| field | type | meaning |
|---|---|---|
| `schema_version` | integer | always `1` for this document. A consumer refuses any other value. |
| `app_version` | string | `samsara.__version__` of the build, e.g. `0.23.0-beta.1`. |
| `release_tag` | string | the application's version tag, e.g. `v0.40.4`; core and installer use this release, optional packs use `addons`. |
| `build_date` | string | ISO 8601 UTC. The committer date of `git_sha`, not the wall clock, so two runs of the same build agree. |
| `git_sha` | string | full 40-hex commit the build came from. |
| `components` | array | the components below, sorted by `id`. |
| `release_assets` | optional array | final signed installer metadata: `filename`, `url`, `size_bytes`, `sha256`. Required for installer updates from 0.40.4 onward; the portable ZIP uses the `core` component's metadata. |

## Component

| field | type | meaning |
|---|---|---|
| `id` | string | stable identifier, `[a-z0-9-]+`. Consumers key on it. |
| `name` | string | human name for a picker. |
| `description` | string | one line. |
| `kind` | string | one of `core`, `acceleration`, `model`, `voice`, `feature`. A `feature` is a downloadable app capability rather than a model or hardware acceleration. |
| `available` | boolean | `false` for a component that is declared but not yet released. Its `url`, `sha256`, `size_bytes` and `min_disk_bytes` are `null`, and a consumer lists it as `unavailable` and never downloads it. |
| `filename` | string | the asset's file name on the Release; also the local file name. |
| `url` | string or null | `https://` only. The Release asset. |
| `size_bytes` | integer or null | exact size of the asset, from the file. |
| `sha256` | string or null | lowercase 64-hex SHA-256 of the asset, computed from the file. Never a placeholder: the generator refuses to emit one. |
| `min_disk_bytes` | integer or null | free space needed to download and unpack: at least `size_bytes`. |
| `install_dir` | string | where the asset's contents belong, relative to the application folder (`.` for the core app). |
| `requires` | array of strings | hardware or software preconditions, e.g. `["nvidia_gpu"]`. Empty when none. |
| `default` | boolean | preselected in a picker. |
| `source` | string | `built` (hashed from this build's artifact) or `pinned` (an asset from an earlier release, pinned by URL, size and hash in the generator; a local copy, when present, must match the pin). |

All fields are present on every component; optional information is `null`
or `[]`, never absent. That is what lets the command model land later
without a schema change: its entry flips `available` to `true` and gains real
`url`, `size_bytes`, `sha256` and `min_disk_bytes`.

## Components in this release

| id | kind | source | what it is |
|---|---|---|---|
| `core` | core | built | `Samsara-Windows-<tag>.zip`, the verified CPU build. `default: true`, `install_dir: .`. |
| `cuda-pack` | acceleration | built or pinned | Verified NVIDIA runtime DLLs for `_internal/ctranslate2` (Docs/CUDA.md). `requires: ["nvidia_gpu"]`. Available when a matching local pack exists or an explicitly approved prior manifest supplies a verified pack. Otherwise unavailable. |
| `wake-word-models` | model | built | `Samsara-WakeWord-Models-<tag>.zip`, the nine OpenWakeWord ONNX files as one archive, packaged from the frozen output. The generator checks every member against `tools/release_preflight.OWW_MODELS` before it will emit the entry. `install_dir: _internal/openwakeword/resources/models`. |
| `command-model` | model | pinned | declared, `available: false`, until the hands-free command model is released. |
| `gesture-control` | feature | built | `Samsara-GestureControl-<tag>.zip`, the MediaPipe/cv2 webcam hand-pose runtime. It unpacks into `_internal`, which is the frozen onedir interpreter's import root. |

## Guarantees of the generator

- A `built` artifact that is missing from the artifacts directory is a hard
  failure with the file name in the message.
- No entry is ever emitted with an empty or placeholder hash; a `pinned`
  entry must carry a full 64-hex pin, and a local copy of a pinned asset that
  does not match its pin is a hard failure.
- Output is `json.dumps(..., sort_keys=True, indent=2)` plus a trailing
  newline; components are sorted by `id`; `build_date` comes from Git. Two
  runs over the same build are byte-identical.
- `--reuse-manifest <local-json>` explicitly approves available optional
  packs for the new app. Their filenames, sizes and hashes stay pinned;
  their URL is under `addons`. Installation metadata must match. Any local
  copy must match the pin. Core and installer are never reused. Without this
  argument, built packs are required as before; an absent CUDA pack stays
  unavailable. New wake-word archives still undergo the nine model checks.
- Run `--publish-plan <path>` after signing the installer and building the
  final ZIP. It requires the installer, writes versioned/latest manifest
  copies beside `--output`, writes checksums for both app assets and local optional packs, and
  emits two explicit upload lists. The version list contains exactly the
  installer, portable ZIP, and their two `.sha256` sidecars. Reused packs already present on `addons` need
  no upload. See [RELEASING.md](../RELEASING.md) for publication ordering.
- Updates targeting 0.40.4+ verify downloaded bytes with manifest SHA-256,
  without a checksum sidecar request. Earlier update targets retain legacy
  sidecar handling. Old installed clients keep updating through the two app
  sidecars on each new version release: `SamsaraSetup-<version>.exe.sha256`
  for per-user installed copies and `Samsara-Windows-v<version>.zip.sha256`
  for portable copies. Sidecars contain the same hashes as the manifest.
  Preserving old release files keeps their existing component URLs working.

## Guarantees of the consumer (`samsara/components.py`)

- No network access at import time and no automatic downloads.
- `load_manifest()` accepts a local path or an `https://` URL and validates
  before returning; an invalid manifest raises `ManifestError` listing every
  problem.
- `component_state()` reports `present` (file on disk, size matches, and the
  verified-hash sidecar equals the manifest hash), `missing`, or
  `unavailable`.
- `download_component()` streams into `<filename>.part`, resumes a truncated
  part with an HTTP `Range` request (restarting when the server ignores it),
  reports `(done_bytes, total_bytes)` to a progress callback, verifies the
  SHA-256 over the finished file, and only then renames it into place and
  writes the sidecar. A hash mismatch deletes the part file and raises
  `HashMismatch`; a half-written component is never left on disk.
