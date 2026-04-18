"""
Samsara Voice Dictation & Control

A modular speech-to-text and voice command application.
"""

__version__ = "1.0.0"

# Active modules
try:
    from .commands import CommandExecutor
except ImportError:
    CommandExecutor = None

try:
    from .ui import SplashScreen
except ImportError:
    SplashScreen = None

__all__ = [
    'CommandExecutor',
    'SplashScreen',
]
