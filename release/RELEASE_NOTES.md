# Samsara v0.9.0 - Windows Release

## Downloads

| File | Size | Description |
|------|------|-------------|
| **Samsara-Windows.7z** | 137 MB | Main application (CPU) |
| **Samsara-CUDA-Pack.7z** | 848 MB | GPU acceleration (optional) |

## Installation

### Basic Install (CPU)
1. Extract `Samsara-Windows.7z` to a folder (e.g., `C:\Samsara`)
2. Run `Samsara.exe`
3. First-run wizard will download the Whisper AI model (~75 MB - 3 GB depending on choice)

### GPU Acceleration (NVIDIA only)
If you have an NVIDIA GPU and want faster transcription:
1. Extract `Samsara-CUDA-Pack.7z`
2. Copy all DLL files to: `Samsara\_internal\ctranslate2\`
3. Restart Samsara

**Note:** If you already have CUDA Toolkit 12 installed system-wide, GPU acceleration may work without the CUDA pack.

## Requirements
- Windows 10/11 (64-bit)
- 4 GB RAM minimum (8 GB recommended)
- Microphone
- NVIDIA GPU with 4+ GB VRAM (optional, for GPU acceleration)

## What's New
See [CHANGELOG.md](https://github.com/Morne-Ingstar/samsara/blob/master/CHANGELOG.md)
