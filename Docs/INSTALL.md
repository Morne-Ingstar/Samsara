# Installing Samsara

Two routes. Both end with the same application; pick by your situation.

## 1. The installer (most people)

Download `SamsaraSetup-<version>.exe` from the release page and run it.

- It installs for your user account only, no administrator rights needed.
  Choose "all users" in the first dialog if you want that instead.
- The Components page lists what else is available, with sizes:
  - **Samsara** (required).
  - **Hands-free: wake-word models**, preselected. Say the wake word instead
    of pressing a key.
  - **GPU acceleration: NVIDIA CUDA pack**, preselected when an NVIDIA GPU is
    detected. Shown greyed with "No NVIDIA GPU detected" otherwise. If the
    installer says it could not detect a GPU, choose it only if you have an
    NVIDIA card.
  - **Command model**, greyed with "Coming soon" until it is released.
- Pressing Enter on every page gives a working install.
- After the files are copied, the components you chose are downloaded
  (verified by SHA-256, resumable) with a progress window and a Cancel
  button. Cancelling, or having no network, keeps a working Samsara: the
  final page says so, and the components can be added any time later from
  the first-run wizard's Optional Components page or from Settings.

The installer is not code-signed yet, so Windows SmartScreen shows "Windows
protected your PC" on first run. Click "More info", then "Run anyway", after
checking the file's SHA-256 against the `.sha256` beside it on the release
page. `installer/README.md` explains what signing will take.

## 2. The ZIPs (corporate proxies, no network on the target machine)

Every component is also a plain ZIP on the release page, and `manifest.json`
beside them lists each one's size and SHA-256.

1. Download `Samsara-Windows-<version>.zip` and unpack it anywhere; run
   `Samsara.exe` from that folder.
2. Wake-word models: unpack `Samsara-WakeWord-Models-<version>.zip` into
   `Samsara\_internal\openwakeword\resources\models`.
3. GPU acceleration: unpack `Samsara-CUDA-Pack-v0.20.0.zip` into
   `Samsara\_internal\ctranslate2` (all ten DLLs; see `docs/CUDA.md`).

Or let the installer do the unpacking from a folder you carried over: put
the ZIPs in one folder and run

```
SamsaraSetup-<version>.exe /ComponentsDir="D:\samsara-components"
```

The installer then verifies and installs the selected components from that
folder instead of the network. `/NoFetch` skips components entirely.

## Verifying a download

```
certutil -hashfile SamsaraSetup-<version>.exe SHA256
```

Compare with the `.sha256` file on the release page, or with `manifest.json`
for the component ZIPs.
