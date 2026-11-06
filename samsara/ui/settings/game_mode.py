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
    """Group the live engine with Game mode's existing preference control."""
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget
    from samsara.ui import theme
    from .sidebar import icon_family

    class RuntimeLabel(QLabel):
        """Reserve wrapped ink height inside Settings' nested row layouts."""
        def fit_height(self):
            if self.width() > 0:
                self.setMinimumHeight(max(self.fontMetrics().height(),
                                          self.heightForWidth(self.width())))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self.fit_height()

    row = owner._rows_by_widget['game_enabled']
    row.setObjectName('gameModeSection')
    row.grid.setContentsMargins(0, 28, 0, 18)
    enabled = owner._widgets['game_enabled']
    row.heading.setStyleSheet('font-weight: 600;')
    # Keep the switch beside the heading even when SettingRow moves its now
    # empty value holder below the text at narrow widths.
    row.values.removeWidget(enabled)
    row.value.hide()
    row.text.layout().removeWidget(row.heading)
    header = QWidget(row)
    header.setObjectName('gameModeHeader')
    line = QHBoxLayout(header)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(10)
    family = icon_family()
    if family:
        icon = QLabel(chr(0xE7FC), header)  # Game, in Settings' existing icon font.
        icon.setObjectName('gameModeIcon')
        icon.setFont(theme.qfont(theme.TYPE_HEADING, family=family))
        icon.setFixedSize(24, 24)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line.addWidget(icon)
    line.addWidget(row.heading)
    badge = QLabel(header)
    badge.setObjectName('gameRuntimeBadge')
    badge.setTextFormat(Qt.TextFormat.PlainText)
    badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    line.addWidget(badge)
    line.addStretch()
    if row.help is not None:
        row.grid.removeWidget(row.help)
        line.addWidget(row.help)
    line.addWidget(enabled)
    row.grid.removeWidget(row.text)
    row.grid.addWidget(header, 0, 0, 1, 3)
    row.grid.addWidget(row.text, 2, 0, 1, 3)
    label = RuntimeLabel(row.text)
    label.setObjectName('gameRuntimeStatus')
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    text_policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    text_policy.setHeightForWidth(True)
    label.setSizePolicy(text_policy)
    column = row.text.layout()
    column.setSpacing(8)
    column.insertWidget(0, label)
    detail = RuntimeLabel(row.text)
    detail.setObjectName('gameRuntimeDetail')
    detail.setTextFormat(Qt.TextFormat.PlainText)
    detail.setWordWrap(True)
    detail.setMinimumWidth(0)
    detail.setSizePolicy(text_policy)
    column.insertWidget(1, detail)
    row.detail.setContentsMargins(0, 4, 0, 0)
    owner._widgets['game_runtime_status'] = label
    owner._widgets['game_runtime_badge'] = badge
    owner._widgets['game_runtime_detail'] = detail
    previous_style = None

    def refresh():
        nonlocal previous_style
        state, text, reason = _runtime_status(owner.app)
        # PLACEHOLDER COPY (1152b): distinguish unsaved preference from runtime.
        if enabled.isChecked() != cfg_get(owner.app.config, 'game_mode.enabled'):
            action = 'Enable' if enabled.isChecked() else 'Disable'
            reason += f' {action} game mode on Apply.'
        reason = reason.strip()
        if label.text() != text:
            label.setText(text)
            label.setAccessibleName(text)
            label.fit_height()
        if badge.text() != state:
            badge.setText(state)
            badge.setAccessibleName(state)
        if detail.text() != reason:
            detail.setText(reason)
            detail.setAccessibleName(reason)
            detail.setVisible(bool(reason))
            detail.fit_height()
        name = f'Game mode: {state}. {text}'
        row.setAccessibleName(name)
        row.setAccessibleDescription(reason)
        enabled.setAccessibleDescription(f'{name}. {reason}')
        style_key = (state == 'Active', theme.active_theme())
        if style_key != previous_style:
            previous_style = style_key
            active = state == 'Active'
            fill = theme.ACCENT_DIM if active else theme.BG2
            ink = theme.ACCENT if active else theme.TEXT_PRIMARY
            badge.setStyleSheet(f"""
                QLabel#gameRuntimeBadge {{ background: {fill}; color: {ink};
                    border: none; border-radius: 12px;
                    padding: 3px 10px; font-size: {theme.TYPE_BODY}px; font-weight: 600; }}
            """)

    refresh()
    enabled.toggled.connect(refresh)
    timer = QTimer(label)
    timer.setInterval(500)
    timer.timeout.connect(lambda: refresh() if label.isVisible() else None)
    timer.start()


def _model_name(name):
    """Readable standard model names; custom model identifiers stay intact."""
    base = name.removesuffix('.en')
    if base in ('tiny', 'base', 'small', 'medium', 'large'):
        return f'{base.capitalize()} model' + (' (English)' if name.endswith('.en') else '')
    return name


def _runtime_status(app):
    """Read one policy snapshot; never describe a saved model as running."""
    from samsara.game_mode import _busy, _model_device, MAX_SWITCHES_PER_WINDOW, SWITCH_WINDOW_SECONDS

    # PLACEHOLDER COPY (1152b)
    policy = getattr(app, '_game_mode', None)
    if policy is None:
        loading = getattr(app, 'loading_model', False)
        return ('Starting' if loading else 'Not ready',
                'Currently running: Speech model loading' if loading else
                'Currently running: Speech model not ready', '')
    with policy._lock:
        active, wanted = policy.active, policy.wanted
        name = policy._active_name if active else policy._primary_name
        device = _model_device(app.model, app.device_type)
        device = {'cuda': 'NVIDIA GPU', 'cpu': 'CPU'}.get(device, device.upper())
        text = f'Currently running: {_model_name(name)} \u00b7 {device}'
        if policy._loading_name:
            return 'Switching', text, f'Preparing {_model_name(policy._loading_name)}. Dictation keeps the model shown above.'
        if wanted and policy._load_failed:
            return 'Model unavailable', text, 'The game CPU model could not load. Dictation keeps the model shown above.'
        if wanted != active:
            direction = 'on' if wanted else 'off'
            if _busy(app):
                return 'Waiting until idle', text, f'Game mode will switch {direction} when dictation is idle.'
            if wanted and policy.resident is None:
                return 'Preparing model', text, 'Game mode is requested; its CPU model is not ready yet.'
            recent = sum(policy.clock() - at < SWITCH_WINDOW_SECONDS for at in policy._switches)
            reason = ('Recent model switches must settle before changing again.'
                      if recent >= MAX_SWITCHES_PER_WINDOW else
                      f'Game mode is requested {direction}; waiting for the model switch.')
            return 'Waiting to switch', text, reason
        if active:
            return 'Active', text, 'Game mode is using the CPU model. Live words are off.'
        if not cfg_get(app.config, 'game_mode.enabled'):
            reason = 'Game mode is disabled.'
        elif policy.automatic:
            reason = 'Automatic detection is enabled; no game is currently detected.'
        else:
            reason = 'Game mode is off for this session.'
        return 'Off', text, reason
