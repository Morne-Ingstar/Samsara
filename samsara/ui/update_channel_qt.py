"""Explicit release-channel choice; changing it never performs a check."""
from contextlib import nullcontext
import json
import os
from pathlib import Path

from samsara.config_defaults import cfg_get


def includes_betas(config):
    return cfg_get(config if isinstance(config, dict) else {}, 'updates.include_prerelease') is True


def channel_token(app):
    return (includes_betas(getattr(app, 'config', {})),
            getattr(app, '_update_channel_generation', 0))


def save_beta_choice(app, enabled):
    config = getattr(app, 'config', None)
    save = getattr(app, 'update_config_and_save', None)
    if not isinstance(config, dict) or not callable(save) or getattr(app, '_config_load_failed', False) is True:
        return False
    previous = config.get('updates', {})
    previous = dict(previous) if isinstance(previous, dict) else {}
    updates = dict(previous, include_prerelease=bool(enabled))
    try:
        if save({'updates': updates}) is False:
            raise OSError('Update channel was not saved')
        path = getattr(app, 'config_path', None)
        if isinstance(path, (str, os.PathLike)):
            # The app's save API logs some failures instead of raising them.
            # Check only this choice in the persisted record before showing it.
            stored = json.loads(Path(path).read_text(encoding='utf-8'))
            if includes_betas(stored) != bool(enabled):
                raise OSError('Update channel was not persisted')
        if includes_betas(getattr(app, 'config', {})) != bool(enabled):
            raise OSError('Update channel was not applied')
    except (OSError, ValueError, TypeError):
        lock = getattr(app, '_config_lock', None)
        with lock if lock is not None else nullcontext():
            config = app.config
            current = config.get('updates', {})
            current = dict(current) if isinstance(current, dict) else {}
            if 'include_prerelease' in previous:
                current['include_prerelease'] = previous['include_prerelease']
            else:
                current.pop('include_prerelease', None)
            config['updates'] = current
        return False
    app._update_channel_generation = getattr(app, '_update_channel_generation', 0) + 1
    return True
