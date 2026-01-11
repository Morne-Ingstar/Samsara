"""
Samsara Voice Dictation & Control

A modular speech-to-text and voice command application.
"""

__version__ = "1.0.0"

# Core modules that don't require heavy dependencies
from .config import Config
from .speech import SpeechRecognizer, TextProcessor

# Optional modules - may fail if dependencies not installed
try:
    from .audio import AudioCapture, AudioPlayer
except ImportError:
    AudioCapture = None
    AudioPlayer = None

try:
    from .commands import CommandExecutor
except ImportError:
    CommandExecutor = None

try:
    from .ui import SplashScreen
except ImportError:
    SplashScreen = None

__all__ = [
    'Config',
    'AudioCapture',
    'AudioPlayer',
    'SpeechRecognizer',
    'TextProcessor',
    'CommandExecutor',
    'SplashScreen',
]
