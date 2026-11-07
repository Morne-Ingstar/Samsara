"""Optional playback transport only. Never launches or signals an application."""
from . import RATE
from .storage import read_wav


def detect(devices=None):
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()
    outputs = [dict(id=i, name=d["name"]) for i, d in enumerate(devices)
               if d.get("max_output_channels", 0) > 0 and "cable input" in d["name"].casefold()
               and "vb-audio" in d["name"].casefold()]
    inputs = [dict(id=i, name=d["name"]) for i, d in enumerate(devices)
              if d.get("max_input_channels", 0) > 0 and "cable output" in d["name"].casefold()
              and "vb-audio" in d["name"].casefold()]
    return dict(available=bool(outputs and inputs), playback_devices=outputs, capture_devices=inputs,
                message="VB-CABLE available" if outputs and inputs else "needs VB-CABLE (owner one-time install)")


def play(path, device):
    import sounddevice as sd
    found = detect()
    if device not in {row["id"] for row in found["playback_devices"]}:
        raise ValueError("Select an explicit VB-CABLE Input output device")
    pcm = read_wav(path).astype("float32") / 32768
    sd.play(pcm, samplerate=RATE, device=device, blocking=True)
    return dict(played_samples=len(pcm), device=device,
                scope="transport only; observe the separately prepared TEST-profile application")
