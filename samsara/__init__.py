"""
Samsara Voice Dictation & Control

A modular speech-to-text and voice command application.
"""

__version__ = "0.22.1"

import logging as _logging
_logger = _logging.getLogger(__name__)

# Active modules (may fail on headless systems — pyautogui needs a display)
try:
    from .commands import CommandExecutor
except Exception:
    _logger.exception("CommandExecutor import failed; running without command support")
    CommandExecutor = None

try:
    from .ui import SplashScreen
except Exception:
    SplashScreen = None

__all__ = [
    'CommandExecutor',
    'SplashScreen',
]
