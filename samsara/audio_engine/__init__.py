"""samsara.audio_engine — AudioCaptureEngine transport layer.

ACE-01: Frame + FrameBus + AudioCaptureEngine interfaces.
        Pre-allocated NumPy ring, atomic cursors. No PortAudio yet.

Nothing in the existing codebase imports this package. The legacy
capture path in dictation.py is untouched and remains the only path
that drives the running app. These modules are imported only by
tests/audio_engine/ until ACE-02.
"""

from .frame import (
    Frame,
    SAMPLE_RATE,
    FRAME_MS,
    FRAME_SIZE,
    RING_SECONDS,
    RING_FRAMES,
    PREBUFFER_SECONDS,
    PREBUFFER_FRAMES,
)
from .ring import (
    FrameBus,
    Reader,
    EMPTY,
    OVERRUN,
)
from .engine import AudioCaptureEngine
from .debug_recorder import DebugRecorder

__all__ = [
    # Data structure
    'Frame',
    'FrameBus',
    'Reader',
    'AudioCaptureEngine',
    'DebugRecorder',
    # Sentinels
    'EMPTY',
    'OVERRUN',
    # Constants
    'SAMPLE_RATE',
    'FRAME_MS',
    'FRAME_SIZE',
    'RING_SECONDS',
    'RING_FRAMES',
    'PREBUFFER_SECONDS',
    'PREBUFFER_FRAMES',
]
