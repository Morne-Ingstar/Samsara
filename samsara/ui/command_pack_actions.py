"""Command pack navigation and consent-aware live activation (784)."""
from contextlib import nullcontext
from copy import deepcopy

from samsara.command_packs import PACKS, get_enabled_packs


def settings_controller(app):
    from samsara.ui.settings_qt import SettingsQt
    controller = getattr(app, '_settings_qt', None)
    if controller is None:
        controller = app._settings_qt = SettingsQt(app)
    return controller


def open_packs(app):
    from samsara.ui.settings.tools_page import open_packs as open_section
    open_section(settings_controller(app))


def refresh_runtime(app, previous, *, refresh_reference=True, rebuild_matcher=True):
    """Publish one new frozen matcher when effective pack membership changes."""
    if set(previous) == get_enabled_packs(app.config):
        return
    rebuild = getattr(getattr(app, 'command_executor', None), 'rebuild_matcher', None)
    if rebuild_matcher and callable(rebuild):
        rebuild()
    if refresh_reference:
        refresh = getattr(getattr(app, 'cheat_sheet', None), 'refresh', None)
        if callable(refresh):
            refresh()


def _persist_pack(app, pack_id, *, accept_hermes=False):
    with getattr(app, '_config_lock', None) or nullcontext():
        keys = ('command_packs', 'hermes') if accept_hermes else ('command_packs',)
        before = {key: deepcopy(app.config[key]) for key in keys if key in app.config}
        app.config.setdefault('command_packs', {})[pack_id] = True
        if accept_hermes:
            app.config.setdefault('hermes', {})['notice_accepted'] = True
        try:
            app.save_config()
        except Exception:
            for key in keys:
                if key in before:
                    app.config[key] = before[key]
                else:
                    app.config.pop(key, None)
            raise


def enable_pack(app, pack_id, parent=None):
    """Use the existing preference and permission gates; never run a command."""
    if pack_id not in PACKS:
        return False
    previous = get_enabled_packs(app.config)
    if pack_id in previous:
        return True
    if pack_id == 'hermes':
        from samsara.agent_channel.hermes import installed
        from samsara.ui import hermes_notice
        if not installed():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(parent, 'Hermes',
                                    'Install Hermes before turning on its command pack.')
            return False
        if (app.config.get('hermes') or {}).get('notice_accepted') is not True:
            if not hermes_notice.request_acceptance(parent):
                return False
        _persist_pack(app, pack_id, accept_hermes=True)
    elif pack_id == 'ai':
        from samsara.ui import ava_consent_qt
        from samsara import ava_readiness
        signature = ava_readiness.settings_signature(app)
        policy = ava_consent_qt.selected_ava_policy(app.config)
        record = ava_consent_qt.request_consent(parent, app.config, cloud_enabled=policy == 'cloud')
        if record is None:
            return False
        updated = ava_consent_qt.apply_accepted_ava(app, record, policy=policy, persist=False)
        app.update_config({key: updated[key] for key in ava_consent_qt._PREFERENCE_KEYS}, save=True)
        ava_readiness.recheck_after_settings(app, signature)
    elif pack_id == 'agents':
        # This switch also synchronises the optional local server. Reuse it.
        controller = settings_controller(app)
        controller._show_tool('Command library')
        controller._window._widgets['_pack_checkboxes']['agents'].setChecked(True)
    else:
        _persist_pack(app, pack_id)
    refresh_runtime(app, previous, refresh_reference=False)
    return pack_id in get_enabled_packs(app.config)
