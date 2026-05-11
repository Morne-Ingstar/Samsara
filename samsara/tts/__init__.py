"""Samsara TTS subsystem — Phase 1a: WinRT engine foundation.

Public API:
    WinRTEngine   — primary engine (Windows, requires winsdk)
    TTSEngine     — abstract base for all engines
    VoiceInfo     — voice metadata dataclass
    SpeechHandle  — handle to an in-progress utterance
    WinRTHelper   — async/sync bridge (internal; exposed for tests)
    TTSError, EngineUnavailableError, RenderError — exceptions
"""

from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError, TTSError
from .winrt_engine import WinRTEngine
from .winrt_helper import WinRTHelper, get_helper

__all__ = [
    "WinRTEngine",
    "TTSEngine",
    "VoiceInfo",
    "SpeechHandle",
    "WinRTHelper",
    "get_helper",
    "TTSError",
    "EngineUnavailableError",
    "RenderError",
]
