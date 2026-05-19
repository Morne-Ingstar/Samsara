"""samsara.audio_engine — AudioCaptureEngine transport layer.

ACE-01: Frame + FrameBus + AudioCaptureEngine interfaces.
ACE-02: Real PortAudio capture, DebugRecorder, equivalence harness.
ACE-03: DictationSessionConsumer — hold-mode dictation via ring.
ACE-04: ContinuousConsumer, WakeConsumer — remaining paths migrated.
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
from .dictation_consumer import DictationSessionConsumer
from .continuous_consumer import ContinuousConsumer
from .wake_consumer import WakeConsumer

__all__ = [
    # Data structure
    'Frame',
    'FrameBus',
    'Reader',
    'AudioCaptureEngine',
    'DebugRecorder',
    'DictationSessionConsumer',
    'ContinuousConsumer',
    'WakeConsumer',
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
