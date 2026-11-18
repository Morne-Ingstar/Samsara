"""1262 input boundary adapter. PCM stays in memory; no physical input opens.

Loaded only by the guarded disposable-profile plugin, before ACE starts.
Product callbacks, rings, consumers, VAD and decoder remain installed code.
"""
from pathlib import Path
import threading
import time
import wave


def read_take(path):
    import numpy as np
    with wave.open(str(path), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise ValueError('Require corpus mono PCM16 16kHz WAV')
        if not .6 <= stream.getnframes() / 16000 <= 7:
            raise ValueError('Require a complete take between 0.6 and 7 seconds')
        frames = stream.getnframes()
        data = stream.readframes(frames)
        if len(data) != frames * 2:
            raise ValueError('Incomplete corpus take')
        return np.frombuffer(data, '<i2').astype('float32') / 32768


def choose_take(corpus):
    # Never read the JSON transcripts or recorder session. Stable WAV-only choice.
    for path in sorted((Path(corpus) / 'takes').glob('*.wav')):
        try:
            pcm = read_take(path)
            if float((pcm * pcm).mean()) > 1e-7:
                return path
        except (ValueError, wave.Error, OSError, EOFError):
            continue
    raise ValueError('No suitable recorded take')


class Replay:
    def __init__(self, pcm):
        self.pcm = pcm
        self.lock = threading.Lock()
        self.position = 0
        self.started = float('inf')
        self.frames = 0

    def queue(self, delay=0):
        with self.lock:
            self.position = 0
            self.started = time.monotonic() + delay

    def silence(self):
        with self.lock:
            self.started = float('inf')

    def block(self, count, rate, channels):
        import numpy as np
        output = np.zeros((count, channels), dtype='float32')
        with self.lock:
            if time.monotonic() >= self.started:
                # Sample positions follow the requested native stream rate.
                indices = self.position + np.arange(count) * 16000 / rate
                valid = indices < len(self.pcm)
                if valid.any():
                    output[valid, :] = np.interp(indices[valid], np.arange(len(self.pcm)), self.pcm)[:, None]
                self.position += count * 16000 / rate
            self.frames += count
        return output


def install(sd, replay):
    """Replace only the device boundary, for every copied-build input stream."""
    class InputStream:
        def __init__(self, *, samplerate, callback, blocksize=0, channels=1,
                     finished_callback=None, **kwargs):
            self.samplerate, self.callback = samplerate, callback
            self.blocksize = blocksize or int(samplerate / 10)
            self.channels = channels
            self.finished = finished_callback
            self.closed = self.active = False
            self.error = False
            self.stopping = threading.Event()

        def start(self):
            if self.active:
                return self
            self.active = True
            self.stopping.clear()
            def pump():
                try:
                    while not self.stopping.is_set():
                        began = time.monotonic()
                        block = replay.block(self.blocksize, self.samplerate, self.channels)
                        self.callback(block, self.blocksize, {}, sd.CallbackFlags())
                        self.stopping.wait(max(0, self.blocksize / self.samplerate - (time.monotonic() - began)))
                except Exception:
                    self.error = True
                finally:
                    self.active = False
                    if self.finished:
                        self.finished()
            self.thread = threading.Thread(target=pump, name='receipt-input', daemon=True)
            self.thread.start()
            return self

        def stop(self):
            self.stopping.set()
            if hasattr(self, 'thread'):
                self.thread.join(2)

        abort = stop

        def close(self):
            self.stop()
            self.closed = True

        def __enter__(self):
            return self.start()

        def __exit__(self, *_):
            self.close()

    sd.InputStream = InputStream
    # No raw-input fallback may open the owner's microphone.
    def refused(*args, **kwargs):
        raise RuntimeError('RawInputNotCovered')
    sd.RawInputStream = refused
    return InputStream
