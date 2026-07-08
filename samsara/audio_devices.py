"""Pure helpers for audio input device selection.

Kept separate from dictation.py's get_available_microphones()/
refresh_audio_devices() (which talk to sounddevice/PortAudio) so the
selection-preservation logic can be unit-tested without touching real audio,
and so Qt UI code (settings_qt.py, the setup wizards) can share it.
"""


def pick_index_by_name(devices, name):
    """Return the position of the device named `name` in `devices`, or None.

    `devices` is a list of dicts shaped like get_available_microphones()'s
    return value (each with a 'name' key). Matching is by name, not by
    PortAudio device id/index -- indices shift after re-enumeration, but a
    device's reported name is stable across reconnects.

    Returns None (never raises, never guesses) when `name` is falsy or not
    present in `devices` -- callers fall back to their own default (e.g. the
    first item, or "System default") in that case.
    """
    if not name:
        return None
    for idx, dev in enumerate(devices):
        if dev.get('name') == name:
            return idx
    return None
