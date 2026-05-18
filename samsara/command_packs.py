"""Command pack definitions for Samsara.

A pack is a named group of related commands. Users can enable/disable packs
in Settings to control which commands are active, improving recognition
accuracy by reducing the matcher's search space.

Pack state is stored in config['command_packs']. The 'core' pack is
always_on -- it cannot be disabled. All other packs respect the user's
preference, defaulting to default_enabled when not in config.
"""

PACKS = {
    'core': {
        'label': 'Core',
        'description': 'Essential commands — undo, copy, paste, window snap, scroll',
        'always_on': True,
        'default_enabled': True,
    },
    'text-editing': {
        'label': 'Text Editing',
        'description': 'Punctuation, formatting, bold/italic/underline, find, select',
        'always_on': False,
        'default_enabled': True,
    },
    'window-management': {
        'label': 'Window Management',
        'description': 'Move windows between monitors, cursor teleportation, movie mode',
        'always_on': False,
        'default_enabled': True,
    },
    'browsers': {
        'label': 'Browsers',
        'description': 'Open Chrome/Firefox/Edge, tab control, zoom, navigation',
        'always_on': False,
        'default_enabled': True,
    },
    'media': {
        'label': 'Media & Music',
        'description': 'Spotify, play/pause, volume, next/previous track',
        'always_on': False,
        'default_enabled': True,
    },
    'smart-home': {
        'label': 'Smart Home',
        'description': 'Hyperion LED strip control, light effects and colors',
        'always_on': False,
        'default_enabled': False,
    },
    '3d-printing': {
        'label': '3D Printing',
        'description': 'FlashForge printer control — start, pause, status',
        'always_on': False,
        'default_enabled': False,
    },
    'stremio': {
        'label': 'Stremio',
        'description': 'Stremio media player — pause, skip, fullscreen',
        'always_on': False,
        'default_enabled': False,
    },
    'screen-capture': {
        'label': 'Screen Capture',
        'description': 'Screenshot, GIF recording, screen region capture',
        'always_on': False,
        'default_enabled': False,
    },
    'macros': {
        'label': 'Custom Macros',
        'description': 'Going dark, focus mode, break time, morning routine',
        'always_on': False,
        'default_enabled': False,
    },
    'gaming': {
        'label': 'Gaming',
        'description': 'Hold keys, jump, release all — game-specific controls',
        'always_on': False,
        'default_enabled': False,
    },
    'mouse': {
        'label': 'Mouse Control',
        'description': 'Left click, right click, double click by voice',
        'always_on': False,
        'default_enabled': False,
    },
    'audio': {
        'label': 'Audio Devices',
        'description': 'Switch between speakers, headset, earbuds',
        'always_on': False,
        'default_enabled': False,
    },
    'utilities': {
        'label': 'Utilities',
        'description': 'Timer, web shortcuts, quick ask, calculator, notepad',
        'always_on': False,
        'default_enabled': False,
    },
    'smart-actions': {
        'label': 'Smart Actions',
        'description': 'Brain dump, voice notes, AI agent routing',
        'always_on': False,
        'default_enabled': True,
    },
    'tasks': {
        'label': 'Tasks',
        'description': 'Add items to task lists by voice',
        'always_on': False,
        'default_enabled': True,
    },
    'health': {
        'label': 'Health Tracking',
        'description': 'Pain levels, medication logging, symptom tracking, health summaries',
        'always_on': False,
        'default_enabled': True,
    },
    'alarms': {
        'label': 'Alarm Commands',
        'description': 'Voice control for alarms — complete, dismiss, list, enable/disable',
        'always_on': False,
        'default_enabled': True,
    },
    'discord': {
        'label': 'Discord',
        'description': 'Send messages to Discord channels via webhook',
        'always_on': False,
        'default_enabled': False,
    },
    'ai': {
        'label': 'AI Assistant',
        'description': 'Local AI via Ollama — ask questions, summarise, generate text',
        'always_on': False,
        'default_enabled': False,
    },
    'accessibility': {
        'label': 'Accessibility',
        'description': 'Show Numbers overlay, semantic clicking by voice',
        'always_on': False,
        'default_enabled': True,
    },
}


def get_enabled_packs(config: dict) -> set:
    """Return the set of pack names that are currently enabled.

    Reads config['command_packs'], applies defaults for missing packs,
    and always includes always_on packs regardless of config.
    """
    user_packs = config.get('command_packs', {})
    enabled = set()
    for pack_id, meta in PACKS.items():
        if meta['always_on']:
            enabled.add(pack_id)
        elif user_packs.get(pack_id, meta['default_enabled']):
            enabled.add(pack_id)
    return enabled


def default_pack_config() -> dict:
    """Return the default command_packs config block."""
    return {pack_id: meta['default_enabled'] for pack_id, meta in PACKS.items()}
