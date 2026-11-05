"""Value-free external-config diagnostics (1146); no profile reads."""
from pathlib import PurePath

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.config_seed import build_default_config


# Only product-defined names may reach a log. An unknown JSON key can itself
# contain a credential, private text or a forged log line; regexes cannot
# distinguish all such values from ordinary setting names.
_SAFE_KEYS = frozenset(key.split('.')[0] for key in SETTINGS_SCHEMA) | frozenset(
    build_default_config(home=PurePath('.'), macro_defaults={}, notification_defaults={})
)


def log_external_config_changes(logger, changed):
    """Log changed names only, without reading or formatting their values."""
    for key in changed:
        label = key if isinstance(key, str) and key in _SAFE_KEYS else '[redacted key]'
        logger.info('[CONFIG] External edit detected: %s changed', label)
