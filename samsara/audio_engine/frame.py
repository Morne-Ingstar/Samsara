"""Frame definition and FrameBus constants for the AudioCaptureEngine.

All values marked [LOCKED] are binding per the frozen spec at
C:\\Users\\Morne\\Documents\\Claude\\audiocaptureengine_01_spec_v2_FROZEN.md
and must not be changed without reopening the ARC review process.

Constants are the single source of truth for the entire audio_engine
package. Nothing in consumers should hard-code these values.
"""

import numpy as np

from samsara.constants import PREBUFFER_SECONDS as _PREBUFFER_SECONDS

# ── [LOCKED] Bus format constants ─────────────────────────────────────────────

SAMPLE_RATE: int = 16000
"""[LOCKED] Canonical bus sample rate in Hz. All consumers receive 16kHz
int16 audio; resampling from the native device rate happens once at the
engine head (ACE-02). Never change this without reopening ARC review."""

FRAME_MS: int = 100
"""Duration of one Frame in milliseconds. Chosen to match the 100ms
blocksize used in the ACE-00 jitter probe (4410 samples @ 44100Hz proved
acceptable jitter under concurrent inference load). Changing this would
require re-running ACE-00 against the new blocksize."""

FRAME_SIZE: int = SAMPLE_RATE * FRAME_MS // 1000
"""Number of int16 samples per Frame: 16000 * 100 // 1000 = 1600."""

# ── Ring sizing ────────────────────────────────────────────────────────────────

RING_SECONDS: int = 60
"""Total ring buffer depth in seconds. Must satisfy:
RING_SECONDS >= PREBUFFER_SECONDS + max_utterance_duration.
At 60s: supports utterances up to 58.5s before overrun.
Cost: RING_FRAMES * FRAME_SIZE * 2 bytes = ~19.2 MB at current
settings — acceptable for a desktop application."""

RING_FRAMES: int = RING_SECONDS * 1000 // FRAME_MS
"""Number of Frame slots in the ring: 60 * 1000 // 100 = 600."""

# ── Prebuffer sizing — must mirror samsara.constants, never diverge ───────────

PREBUFFER_SECONDS: float = _PREBUFFER_SECONDS
"""Rolling pre-trigger window in seconds. AUTHORITATIVE SOURCE:
samsara.constants.PREBUFFER_SECONDS (currently 1.5). Imported and
mirrored here so callers within audio_engine reference a local name
without accidentally hard-coding a different value. If the value in
samsara.constants changes, this changes automatically."""

PREBUFFER_FRAMES: int = int(PREBUFFER_SECONDS * 1000) // FRAME_MS
"""Number of frames in the prebuffer window: int(1.5 * 1000) // 100 = 15.

[LOCKED] Prebuffer is implemented as a Reader cursor rewind, not a copy.
See FrameBus.Reader.rewind() in ring.py. This structurally eliminates the
prebuffer-regression bug class (ARC confirmed): a consumer that forgets to
rewind simply starts later in the stream rather than silently skipping
history. There is no copy to forget to prepend."""


# ── Frame ─────────────────────────────────────────────────────────────────────

class Frame:
    """A single audio frame assembled from a FrameBus ring slot.

    pcm is a NumPy int16 ARRAY VIEW into the ring's pre-allocated memory.
    It is NOT a copy. The view is valid only until the FrameBus writer
    wraps around and overwrites this ring slot (approximately RING_SECONDS
    of wall time at the current write rate). Consumers must:

        1. Process or copy the data within the current read cycle.
        2. Never retain a Frame reference beyond one read pass.
        3. Never write to frame.pcm (the ring owns the memory).

    Attributes:
        seq:          Monotonic write counter. Gaps in seq between
                      consecutive frames indicate dropped audio (the writer
                      lapped this reader). seq == 0 on the first write.
        t_capture:    time.perf_counter() recorded inside the capture
                      callback at the moment the block was written.
        pcm:          int16 ndarray of length FRAME_SIZE (1600 samples =
                      100ms at 16kHz). This is a view — see note above.
        device_epoch: Incremented each time the audio stream is reopened
                      (device switch, BT reconnect, recovery). An epoch
                      change means the audio stream is NOT contiguous with
                      the previous epoch. Per the [LOCKED] discontinuity
                      rule, any active utterance spanning an epoch boundary
                      MUST be aborted, not stitched.
    """

    __slots__ = ('seq', 't_capture', 'pcm', 'device_epoch')

    def __init__(
        self,
        seq: int,
        t_capture: float,
        pcm: np.ndarray,
        device_epoch: int,
    ) -> None:
        self.seq          = seq
        self.t_capture    = t_capture
        self.pcm          = pcm
        self.device_epoch = device_epoch

    def __repr__(self) -> str:
        return (
            f"Frame(seq={self.seq}, epoch={self.device_epoch}, "
            f"t={self.t_capture:.6f}, pcm.shape={self.pcm.shape})"
        )
