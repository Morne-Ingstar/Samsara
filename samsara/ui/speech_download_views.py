"""Home's speech download status and Retry action, shared by both headers."""
from samsara.speech_readiness import status
from samsara.ui import theme


def _action(app):
    if getattr(app, '_speech_model_phase', '') == 'failed':
        return 'Retry', 'retry_speech'  # PLACEHOLDER COPY (1007)
    return 'Getting ready', 'none'


def state_view(app):
    label, action = _action(app)
    return 'getting_ready', status(app), label, action


def badge_view(app):
    label, action = _action(app)
    return label, theme.TEXT_SECONDARY, status(app), action
