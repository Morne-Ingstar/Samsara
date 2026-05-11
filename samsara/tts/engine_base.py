"""Abstract TTSEngine interface and associated data types.

All TTS engine implementations must subclass TTSEngine and implement every
abstract method. Consumers (plugins, AudioCoordinator) always talk to the
abstract interface so swapping engines (WinRT → Piper → cloud) requires no
consumer code changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class VoiceInfo:
    voice_id: str
    display_name: str
    language: str
    gender: str  # "male" | "female" | "neutral" | "unknown"


@dataclass
class SpeechHandle:
    """Opaque handle to an in-progress (or queued) utterance.

    Returned immediately by speak(). Callers hold on to this to cancel,
    duck, or poll state. The utterance_id is unique per speak() call.
    """
    utterance_id: str
    # Internal state slot -- engines may set this to track playback status.
    # Consumers should treat this as opaque.
    _state: str = field(default="pending", repr=False, compare=False)


class TTSEngine(ABC):
    """Abstract base for all TTS engines.

    Phase 1a implementation notes for incomplete features:
    - queue_mode: only "append" is fully implemented. Other modes degrade to
      "append" until Phase 1b.
    - category: accepted and stored; not yet used for priority arbitration
      (Phase 2).
    - set_volume fade_ms: instant volume change is acceptable in Phase 1a; the
      5 ms interpolation is Phase 1b polish.
    """

    @abstractmethod
    def speak(
        self,
        text: str,
        *,
        voice_id: Optional[str] = None,
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
        category: str = "general",
        queue_mode: str = "append",
        on_done: Optional[Callable] = None,
    ) -> SpeechHandle:
        """Synthesize and play `text`. Returns immediately with a handle."""

    @abstractmethod
    def cancel(self, handle: SpeechHandle) -> None:
        """Stop playback for a specific utterance."""

    @abstractmethod
    def cancel_all(self) -> None:
        """Stop all active and queued utterances."""

    @abstractmethod
    def set_volume(
        self, handle: SpeechHandle, volume: float, fade_ms: int = 5
    ) -> None:
        """Adjust playback volume for a live utterance.

        Phase 1a: fade_ms is ignored; change is instantaneous.
        Phase 1b: linear interpolation over fade_ms milliseconds.
        """

    @abstractmethod
    def is_speaking(self) -> bool:
        """True if any utterance is currently playing."""

    @abstractmethod
    def list_voices(self) -> list:
        """Return a list of VoiceInfo objects for installed voices."""

    @abstractmethod
    def shutdown(self) -> None:
        """Cancel everything, join threads, release resources."""
