# Samsara installer (Inno Setup 6)

`samsara.iss` builds `SamsaraSetup-<version>.exe` from the finished
PyInstaller dist. It installs the core application only, per user by default
(no administrator rights; the privileges dialog offers an all-users install),
and shows a Components page driven by the release manifest
(`docs/RELEASE_MANIFEST.md`). Nothing large is bundled: after the files are
copied the installer runs the app's own fetcher for the selected components.

## Build

```
python -m PyInstaller --clean --noconfirm scripts\samsara.spec
python tools\gen_release_manifest.py --artifacts-dir . --tag v<version> --git-sha <sha> --output manifest.json
tools\build_installer.cmd manifest.json
```

The version comes from `samsara\__init__.py`; the component sizes and the
command model's availability come from `manifest.json` and are written to
`installer\manifest_defines.iss` (generated, not tracked). `release-build.yml`
runs the same command on CI and uploads the setup exe as an artifact. Inno
Setup 6 is preinstalled on GitHub's windows-latest runners; locally, install it
or point `SAMSARA_ISCC` at `ISCC.exe`.

## Components page

| Inno name | manifest id | default | shown when it cannot be chosen |
|---|---|---|---|
| core | core | fixed, always installed | never |
| wake_word_models | wake-word-models | selected | never |
| cuda_pack | cuda-pack | selected only when an NVIDIA GPU is detected | disabled with "(No NVIDIA GPU detected)"; when detection itself fails it stays selectable with "(Could not detect a GPU; choose it if you have an NVIDIA card)" |
| command_model | command-model | not selected | disabled with "(Coming soon)" while the manifest says `available: false`; lights up at the next build once the manifest says otherwise, no installer change |

GPU detection: `nvcuda.dll` in the system folder (it ships with the NVIDIA
driver), then a WMI `Win32_VideoController` query for "NVIDIA". Any error in
detection means "could not detect", never "no GPU".

Pressing Enter through every page yields a working install: core plus the
wake-word models, plus the CUDA pack on a machine with an NVIDIA GPU.

## After the files are copied

`[Code] CurStepChanged(ssPostInstall)` runs

```
Samsara.exe --fetch-components <ids> --manifest <url> --progress-window [--components-dir <dir>]
```

and waits. That mode lives in `samsara/ui/first_run_qt.py`
(`fetch_components_main`): it validates every id against the manifest before
touching disk, downloads through `samsara.components` (SHA-256 verified,
resumable), unpacks into each component's `install_dir`, and shows a small
progress window with a Cancel button. Exit codes: 0 done, 2 bad id or
manifest, 3 component not available, 4 no network, 5 cancelled, 6 a component
failed verification. The installer never fails on a non-zero code: the core
install is complete either way, and the Finished page says which case
happened. `/ComponentsDir="D:\folder"` serves the archives from a local folder
(no network); `/NoFetch` skips the step.

Dispatch (wired in 108): `dictation.py` routes `--fetch-components` through
`_dispatch_startup_argument`, at the top of the module.
It runs above every heavy import, and above the single-instance lock.
Both placements are load-bearing: above the imports so post-install costs
~200 ms instead of a full app boot, and above the lock so a fetch requested
while a session is already running still fetches instead of exiting 0 having
done nothing.

The wizard's Optional Components page offers the same components, forever,
for anything the installer did not fetch. It shares the download-and-unpack
core (`fetch_and_install`) with this mode but not the outer shell: the
installer needs argv and an exit code, the wizard needs per-component
buttons and cancel.

## Accessibility

Inno Setup's wizard is fully keyboard-driven: Tab and Shift+Tab move through
every control on every page in visual order, Space toggles a component,
Alt+N and Alt+B are Next and Back, Enter activates the default button, and
nothing depends on hovering. The wizard is DPI aware: at 150% Windows
scaling the pages, the component list and the buttons scale with the text
(`WizardSizePercent=120` gives the list room at every scale), and
`WizardResizable=yes` lets the user enlarge it further.

## Signing (note only, no work here)

An unsigned installer that then downloads and runs more code is the worst
SmartScreen case: every first user sees "Windows protected your PC". Signing
it needs a code-signing certificate held by a CA, which for a small open
project means SignPath Foundation's free OSS signing. Their prerequisite is
exactly what now exists: reproducible, verifiable CI builds, so that what is
signed can be traced to a public commit and pipeline. The disabled SignPath
step in `release-build.yml` stays where it is; once an account exists it
signs `Samsara.exe` and this setup exe between packaging and the draft
release. Until then, `docs/INSTALL.md` tells users what the warning means and
how to verify the download by SHA-256 instead.
