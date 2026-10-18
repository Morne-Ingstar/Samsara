from samsara.config_schema import migrate_removed_settings

def test_migration_drops_dead_settings_and_keeps_output_identity():
    cfg = {'output_device': None, 'output_device_name': 'General headset',
           'streaming_hotkey': 'f9', 'tts': {'pitch': 1.2, 'use_for_errors': False},
           'audio_coordinator': {'enabled': False, 'duck_factor': .7},
           'smart_corrections': {'backend': 'cloud', 'allow_cloud_fallback': True},
           'ava_edit': {'timeout_s': 9}}
    assert migrate_removed_settings(cfg)
    assert cfg['output_device_name'] == 'General headset'
    assert cfg['tts'] == {'feedback_level': 'silent', 'enabled': False}
    assert cfg['audio_coordinator'] == {'duck_factor': .7}
    assert cfg['smart_corrections'] == {}
    assert cfg['ava_edit']['timeout_seconds'] == 9

def test_migration_is_idempotent():
    cfg = {'ava_edit': {'timeout_seconds': 9}}
    assert migrate_removed_settings(cfg)
    from copy import deepcopy
    migrated = deepcopy(cfg)
    assert not migrate_removed_settings(cfg)
    assert cfg == migrated
