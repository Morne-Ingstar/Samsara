"""Samsara TTS subsystem — Phase 1b: engine upgrades + AudioCoordinator.

Public API:
    WinRTEngine      — primary engine (Windows, requires winsdk)
    EdgeTTSEngine    — Azure Neural TTS via edge-tts (requires internet)
    AudioCoordinator — state machine; use coordinator.speak() from plugins
    TTSEngine        — abstract base for all engines
    VoiceInfo        — voice metadata dataclass
    SpeechHandle     — handle to an in-progress utterance
    WinRTHelper      — async/sync bridge (internal; exposed for tests)
    TTSError, EngineUnavailableError, RenderError — exceptions
"""

from .coordinator import AudioCoordinator
from .edge_tts_engine import EdgeTTSEngine
from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError, TTSError
from .winrt_engine import WinRTEngine
from .winrt_helper import WinRTHelper, get_helper

__all__ = [
    "WinRTEngine",
    "EdgeTTSEngine",
    "AudioCoordinator",
    "TTSEngine",
    "VoiceInfo",
    "SpeechHandle",
    "WinRTHelper",
    "get_helper",
    "TTSError",
    "EngineUnavailableError",
    "RenderError",
]
