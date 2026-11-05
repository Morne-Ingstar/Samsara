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
    # PLACEHOLDER COPY (1152): describe retained mode and the exit delay.
    for key, label, text, control in (
        ('game_enabled', 'Game mode', 'Use a resident CPU model while gaming. Live words are off; changes wait until dictation is idle.', enabled),
        ('game_detect', 'Game detection', 'Auto detects fullscreen GPU use and stays on across alt-tabs. It leaves after the game is closed or minimized for 45 seconds. Manual uses the tray or “game mode on/off”.', detect),
        ('game_model', 'Game CPU model', 'Loaded on first use and kept in memory. English-only models end in .en; choose a multilingual model for other languages.', model),
    ):
        owner._widgets[key] = control
        layout.addLayout(owner._setting_row(label, text, control))

    def save(acc):
        result = dict(owner.app.config.get('game_mode', {}) or {})
        result.update(enabled=enabled.isChecked(), detect=detect.currentText(), cpu_model=model.currentText())
        return {'game_mode': result}

    owner._save_fns.append(save)


def build_runtime(owner):
    """Attach to the real destination row, after source controls are moved."""
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QLabel, QSizePolicy
    from samsara.game_mode import runtime_text

    row = owner._rows_by_widget['adv_device']
    label = QLabel(row)
    label.setObjectName('gameRuntimeStatus')
    label.setProperty('role', 'description')
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    row.text.layout().addWidget(label)
    owner._widgets['game_runtime_status'] = label
    label.setText(runtime_text(owner.app))
    timer = QTimer(label)
    timer.setInterval(500)
    timer.timeout.connect(lambda: label.setText(runtime_text(owner.app)) if label.isVisible() else None)
    timer.start()
