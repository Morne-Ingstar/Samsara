# Release manifest (`manifest.json`, schema version 1)

Every release declares what it contains. The declaration is one file,
`manifest.json`, attached to the GitHub Release beside the ZIPs. A future
installer reads it to offer the user only what their machine needs; Settings
reads it to show what is present. Nothing user-facing ships with the
manifest itself.

Producer: `tools/gen_release_manifest.py`, run by `release-build.yml` after
the build, from the actual built artifacts. Consumer: `samsara/components.py`
(fetch, validate, list, download one component with verification). Both are
covered by `tests/test_release_manifest.py` and `tests/test_component_fetch.py`.

## Top level

| field | type | meaning |
|---|---|---|
| `schema_version` | integer | always `1` for this document. A consumer refuses any other value. |
| `app_version` | string | `samsara.__version__` of the build, e.g. `0.23.0-beta.1`. |
| `release_tag` | string | the Git tag the assets live under, e.g. `v0.23.0-beta.1` (a `dev-<sha>` string for `workflow_dispatch` builds, which publish no Release). |
| `build_date` | string | ISO 8601 UTC. The committer date of `git_sha`, not the wall clock, so two runs of the same build agree. |
| `git_sha` | string | full 40-hex commit the build came from. |
| `components` | array | the components below, sorted by `id`. |

## Component

| field | type | meaning |
|---|---|---|
| `id` | string | stable identifier, `[a-z0-9-]+`. Consumers key on it. |
| `name` | string | human name for a picker. |
| `description` | string | one line. |
| `kind` | string | one of `core`, `acceleration`, `model`, `voice`. |
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
| `cuda-pack` | acceleration | pinned | `Samsara-CUDA-Pack-v0.20.0.zip`, the ten NVIDIA runtime DLLs for `_internal/ctranslate2` (docs/CUDA.md). `requires: ["nvidia_gpu"]`. Not built on CI (see release.yml's closing note), so pinned to the v0.20.0 asset by URL, size and SHA-256. |
| `wake-word-models` | model | built | `Samsara-WakeWord-Models-<tag>.zip`, the nine OpenWakeWord ONNX files as one archive, packaged from the frozen output. The generator checks every member against `tools/release_preflight.OWW_MODELS` before it will emit the entry. `install_dir: _internal/openwakeword/resources/models`. |
| `command-model` | model | pinned | declared, `available: false`, until the hands-free command model is released. |

## Guarantees of the generator

- A `built` artifact that is missing from the artifacts directory is a hard
  failure with the file name in the message.
- No entry is ever emitted with an empty or placeholder hash; a `pinned`
  entry must carry a full 64-hex pin, and a local copy of a pinned asset that
  does not match its pin is a hard failure.
- Output is `json.dumps(..., sort_keys=True, indent=2)` plus a trailing
  newline; components are sorted by `id`; `build_date` comes from Git. Two
  runs over the same build are byte-identical.

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
