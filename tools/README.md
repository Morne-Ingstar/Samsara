# External Tools

## Archived one-off helpers

Task-specific probes and local workflow helpers are kept in [archive/](archive/README.md)
for reference. They are outside the supported runtime and release tooling.

## nircmd.exe (required for audio switching)
Download from: https://www.nirsoft.net/utils/nircmd.html
Extract nircmd.exe into this folder.
Used for: instant audio device switching, volume control.
Size: ~40KB. Free, portable, no install needed.

To find your exact device names: Win+R -> mmsys.cpl -> Playback tab.
Add friendly aliases in Samsara's config.json under "audio_devices".
