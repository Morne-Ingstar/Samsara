"""Voice commands for switching the Windows default audio device.

One dynamic handler per direction (output/input): the plugin system passes
the text after the matched phrase as `remainder`, so "switch to speakers"
resolves to device alias "speakers" without a hardcoded handler per device.
Users add new devices by editing config.json's audio_devices mapping.
"""

from samsara.plugin_commands import command
from samsara.audio_switch import switch_audio_output, switch_audio_input


def _resolve_device(app, spoken_name):
    """Look up Windows device name from spoken alias.

    Checks audio_devices config dict for a friendly-name mapping.
    Falls back to using the spoken name directly (user might say
    the exact Windows device name).
    """
    devices = {}
    if app is not None and hasattr(app, 'config'):
        devices = app.config.get('audio_devices', {}) or {}
    spoken_lower = spoken_name.lower().strip()
    for key, value in devices.items():
        if key.lower() == spoken_lower:
            return value
    return spoken_name


@command("switch to", aliases=["use", "switch audio to"])
def switch_to(app, remainder):
    """Switch audio output. 'switch to speakers', 'use headset', etc."""
    if not remainder:
        print("[AUDIO] Switch to what? Say a device name.")
        return False
    device = _resolve_device(app, remainder)
    return switch_audio_output(device)


@command("switch mic to", aliases=["switch microphone to",
                                   "use mic", "use microphone"])
def switch_mic(app, remainder):
    """Switch audio input. 'switch mic to headset', etc."""
    if not remainder:
        print("[AUDIO] Switch mic to what? Say a device name.")
        return False
    device = _resolve_device(app, remainder)
    return switch_audio_input(device)
