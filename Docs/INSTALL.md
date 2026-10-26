# Installing Samsara

Two routes. Both end with the same application; pick by your situation.

Open the [latest published release](https://github.com/Morne-Ingstar/Samsara/releases/latest).
v0.40.0 is currently an unpublished draft. When it is published, its assets are:

| File | Purpose |
| --- | --- |
| `SamsaraSetup-0.40.0.exe` | Installer for a normal Windows installation. |
| `Samsara-Windows-v0.40.0.zip` | Portable app; extract before running. |
| `Samsara-WakeWord-Models-v0.40.0.zip` | Optional wake-word models. |
| `Samsara-GestureControl-v0.40.0.zip` | Optional webcam gesture control. |
| `manifest.json` | Component sizes and SHA-256 hashes. |

Each EXE/ZIP above also has a matching `.sha256` file. GitHub's source-code
archives are not the Windows application. The optional CUDA pack is an older,
separate download; it is not a v0.40.0 asset.

## 1. The installer (most people)

Download `SamsaraSetup-0.40.0.exe` from the release page and run it.

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
  button. Cancelling, or having no network, keeps the core app installed: the
  final page says so, and the components can be added any time later from
  the first-run wizard's Optional Components page or from Settings.

The installer is not code-signed yet, so Windows SmartScreen shows "Windows
protected your PC" on first run. Click "More info", then "Run anyway", after
checking the file's SHA-256 against the `.sha256` beside it on the release
page. [Installer documentation](../installer/README.md) explains what signing will take.

## 2. The ZIPs (corporate proxies, no network on the target machine)

Every component is also a plain ZIP on the release page, and `manifest.json`
beside them lists each one's size and SHA-256.

1. Download `Samsara-Windows-v0.40.0.zip` and unpack it anywhere; run
   `Samsara.exe` from that folder.
2. Wake-word models: unpack `Samsara-WakeWord-Models-v0.40.0.zip` into
   `Samsara\_internal\openwakeword\resources\models`.
3. GPU acceleration: unpack `Samsara-CUDA-Pack-v0.20.0.zip` into
   `Samsara\_internal\ctranslate2` (all ten DLLs; see [CUDA guide](CUDA.md)).

The component ZIPs do not include the transcription model. A fresh machine
still needs an internet connection for the speech-model download on first use.

Or let the installer do the unpacking from a folder you carried over: put
the ZIPs and their matching `manifest.json` in one folder and run

```
SamsaraSetup-0.40.0.exe /ComponentsDir="D:\samsara-components"
```

The installer then verifies and installs the selected components from that
folder instead of the network. `/NoFetch` skips components entirely.

## Verifying a download

```
certutil -hashfile SamsaraSetup-0.40.0.exe SHA256
```

Compare with the `.sha256` file on the release page, or with `manifest.json`
for the component ZIPs.

## First start

1. Select your microphone in setup and speak to test it. If it is missing,
   connect it and press **Refresh**. If access is denied, enable microphone
   access in Windows privacy settings, then press **Refresh** again.
2. Choose a transcription model. A CPU is enough; NVIDIA acceleration is
   optional. Samsara downloads an uncached model on first use, so that first
   start can take longer. Later starts use the cached model locally.
3. In a blank note, hold **Ctrl+Shift**, speak, and release to paste your words.
   Setup can change that binding; check **Quick Reference** in the tray menu
   for your actual shortcuts.
4. Open **Settings** in the main window to change your setup. **Settings → Help**
   contains setup and support tools; **Voice help** is also in the main navigation.

Closing setup before finishing leaves it incomplete; it returns on the next
launch. Optional components can be added later from setup or Settings.
