"""
Windows audio device switching via NirCmd.

NirCmd (https://www.nirsoft.net/utils/nircmd.html) is a tiny freeware CLI that
flips the Windows default playback/recording device without any UI. Drop
nircmd.exe into tools/ and these functions drive it via subprocess.

All functions degrade gracefully when nircmd.exe is missing -- they log once
and return False rather than raising, so voice commands never crash the app.
"""

import shutil
import subprocess
from pathlib import Path


def _find_nircmd():
    """Locate nircmd.exe. Checks tools/ dir first, then system PATH."""
    project_root = Path(__file__).parent.parent
    local = project_root / "tools" / "nircmd.exe"
    if local.exists():
        return str(local)
    found = shutil.which("nircmd")
    if found:
        return found
    return None


def switch_audio_output(device_name: str) -> bool:
    """Switch default Windows audio playback device.

    Args:
        device_name: Exact Windows device name (e.g. "Speakers",
                     "Headphones"). Find names via Win+R -> mmsys.cpl.
    Returns:
        True if switch succeeded, False otherwise.
    """
    nircmd = _find_nircmd()
    if not nircmd:
        print("[AUDIO] nircmd.exe not found. Download from "
              "nirsoft.net/utils/nircmd.html and place in tools/")
        return False
    try:
        subprocess.run([nircmd, "setdefaultsounddevice", device_name],
                       check=True, capture_output=True)
        print(f"[AUDIO] Switched output to: {device_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[AUDIO] Failed to switch to '{device_name}': {e}")
        return False


def switch_audio_input(device_name: str) -> bool:
    """Switch default Windows audio recording device."""
    nircmd = _find_nircmd()
    if not nircmd:
        print("[AUDIO] nircmd.exe not found.")
        return False
    try:
        # The "1" flag tells NirCmd this is a recording device
        subprocess.run([nircmd, "setdefaultsounddevice", device_name, "1"],
                       check=True, capture_output=True)
        print(f"[AUDIO] Switched input to: {device_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[AUDIO] Failed to switch input to '{device_name}': {e}")
        return False


def set_volume(level_percent: int) -> bool:
    """Set system volume (0-100)."""
    nircmd = _find_nircmd()
    if not nircmd:
        return False
    nircmd_level = int((level_percent / 100) * 65535)
    try:
        subprocess.run([nircmd, "setsysvolume", str(nircmd_level)],
                       check=True, capture_output=True)
        print(f"[AUDIO] Volume set to {level_percent}%")
        return True
    except subprocess.CalledProcessError:
        return False
