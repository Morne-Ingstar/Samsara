"""Local summaries remain optional and never create an implicit cloud route."""
from time import monotonic

from samsara.ava_readiness import configured_provider


def summary_available(app):
    return (configured_provider(app) == 'ollama'
            and app.config.get('ollama', {}).get('enabled', True)
            and monotonic() >= getattr(app, '_ava_summary_retry_after', 0))


def defer_summary(app):
    # Preserve pending sessions, but do not retry an absent server every turn.
    app._ava_summary_retry_after = monotonic() + 300
