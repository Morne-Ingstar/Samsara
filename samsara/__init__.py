"""
Samsara Voice Dictation & Control

A modular speech-to-text and voice command application.
"""

__version__ = "0.30.0-beta.10"

def __getattr__(name):
    """Resolve public classes on demand; required import failures stay visible."""
    if name == 'CommandExecutor':
        from .commands import CommandExecutor
        return CommandExecutor
    if name == 'SplashScreen':
        from .ui.splash_qt import SplashScreenQt
        return SplashScreenQt
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    'CommandExecutor',
    'SplashScreen',
]
