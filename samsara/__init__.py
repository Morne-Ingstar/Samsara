"""
Samsara Voice Dictation & Control

A modular speech-to-text and voice command application.
"""

__version__ = "1.0.0"

# Active modules (may fail on headless systems — pyautogui needs a display)
try:
    from .commands import CommandExecutor
except Exception:
    CommandExecutor = None

try:
    from .ui import SplashScreen
except Exception:
    SplashScreen = None

__all__ = [
    'CommandExecutor',
    'SplashScreen',
]
