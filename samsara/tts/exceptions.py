class TTSError(Exception):
    """Base for all TTS subsystem errors."""


class EngineUnavailableError(TTSError):
    """Engine could not initialize (missing dependencies, OS limitations, etc.)."""


class RenderError(TTSError):
    """Synthesis or playback failed mid-operation."""
