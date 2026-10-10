"""Network voice decoding; PyAV stays in-process, optional ffmpeg stays hidden."""
import io
import subprocess
import sys

from .exceptions import RenderError


def _pydub_fallback(mp3_bytes: bytes, target_sr: int) -> bytes:
    """Use pydub's WAV reader without its unhidden ffmpeg/probe launchers."""
    from pydub import AudioSegment

    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    result = subprocess.run(
        [AudioSegment.converter, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "mp3", "-read_ahead_limit", "-1", "-i", "cache:pipe:0",
         "-acodec", "pcm_s16le", "-vn",
         "-f", "wav", "pipe:1"],
        input=mp3_bytes, capture_output=True, check=True, **options,
    )
    segment = AudioSegment.from_wav(io.BytesIO(result.stdout))
    return segment.set_frame_rate(target_sr).set_channels(1).set_sample_width(2).raw_data


def mp3_to_pcm(mp3_bytes: bytes, target_sr: int = 44100) -> bytes:
    """Return packed little-endian signed 16-bit mono PCM at target_sr."""
    try:
        try:
            import av
        except ImportError:
            return _pydub_fallback(mp3_bytes, target_sr)

        import numpy as np

        chunks = []
        # Keep the input layout while resampling, then average channels just as
        # pydub does. swresample's default stereo-to-mono matrix boosts gain.
        resampler = av.AudioResampler(format="s16p", rate=target_sr)

        def append_frames(frames):
            for frame in frames:
                samples = frame.to_ndarray()
                mono = samples.astype(np.int64).sum(axis=0) // samples.shape[0]
                chunks.append(mono.astype("<i2").tobytes())

        with av.open(io.BytesIO(mp3_bytes), mode="r", format="mp3") as container:
            for frame in container.decode(audio=0):
                append_frames(resampler.resample(frame))
            append_frames(resampler.resample(None))  # drain delayed tail samples
        return b"".join(chunks)
    except Exception as exc:
        raise RenderError(f"MP3 decode failed: {exc}") from exc

