"""Game-mode controls beside the dictation compute device."""
from samsara.config_defaults import cfg_get


def build(owner, layout):
    from PySide6.QtWidgets import QCheckBox
    from .presentation import CleanComboBox
    # PLACEHOLDER COPY (1058)
    enabled = QCheckBox()
    enabled.setAccessibleName('Enable game mode')
    enabled.setChecked(cfg_get(owner.app.config, 'game_mode.enabled'))
    detect = CleanComboBox()
    detect.addItems(['auto', 'manual'])
    detect.setCurrentText(cfg_get(owner.app.config, 'game_mode.detect'))
    detect.setAccessibleName('Game mode detection')
    model = CleanComboBox()
    model.addItems(['tiny.en', 'base.en', 'small.en', 'tiny', 'base', 'small'])
    model.setCurrentText(cfg_get(owner.app.config, 'game_mode.cpu_model'))
    model.setAccessibleName('Game mode CPU model')
    for key, label, text, control in (
        ('game_enabled', 'Game mode', 'Keep a second CPU model ready while a fullscreen game uses the graphics card. Live words are off; changes wait until dictation is idle.', enabled),
        ('game_detect', 'Game detection', 'Auto checks fullscreen coverage and the foreground process’s 3D GPU usage. Manual uses the tray or “game mode on/off”.', detect),
        ('game_model', 'Game CPU model', 'Loaded on first use and kept in memory. English-only models end in .en; choose a multilingual model for other languages.', model),
    ):
        owner._widgets[key] = control
        layout.addLayout(owner._setting_row(label, text, control))

    def save(acc):
        result = dict(owner.app.config.get('game_mode', {}) or {})
        result.update(enabled=enabled.isChecked(), detect=detect.currentText(), cpu_model=model.currentText())
        return {'game_mode': result}

    owner._save_fns.append(save)
