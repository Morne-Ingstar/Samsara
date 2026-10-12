"""Hermes pack controls, shown only for a PATH installation."""
from contextlib import nullcontext
from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QVBoxLayout, QWidget
from samsara.agent_channel.hermes import EFFORTS, installed, validated_settings
from samsara.config_defaults import cfg_get
from samsara.ui import hermes_notice, theme
from .calm_controls import switch
from .presentation import SettingRow
from .calm_layout import RowLayout


class HermesSection(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.setObjectName("hermesSection")
        self.owner = owner
        config = owner.app.config
        self.accepted = cfg_get(config, "hermes.notice_accepted") is True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox()
        self.enabled.setChecked(bool((config.get("command_packs") or {}).get("hermes", False)) and self.accepted)
        self.enabled.setAccessibleName("Hermes (experimental)")
        switch(self.enabled)
        row = SettingRow("Hermes (experimental)", "Talk to your own Hermes while you keep dictating.", [self.enabled])
        row.detail.show()
        row._calm_layout = RowLayout(row)
        layout.addWidget(row)
        self.options = QWidget()
        choices = QVBoxLayout(self.options)
        choices.setContentsMargins(0, 0, 0, 0)
        for key in ("profile", "model", "effort"):
            if key == "effort":
                control = QComboBox()
                control.addItem("Hermes decides", "")
                for effort in EFFORTS:
                    control.addItem(effort, effort)
                control.setCurrentIndex(max(0, control.findData(cfg_get(config, "hermes." + key))))
            else:
                control = QLineEdit(str(cfg_get(config, "hermes." + key)))
                control.setPlaceholderText("Hermes decides")
                control.setMaxLength(80)
                control.setValidator(QRegularExpressionValidator(QRegularExpression(r"(?:[A-Za-z0-9_./:][A-Za-z0-9_./:\-]{0,79})?"), control))
                control.setMinimumWidth(160)
                control.setMaximumWidth(320)
            control.setMinimumHeight(theme.HIT_TARGET_MIN)
            control.setAccessibleName("Hermes " + key)
            setattr(self, key, control)
            owner._widgets["hermes_" + key] = control
            option = SettingRow(key.capitalize(), "", [control])
            option._calm_layout = RowLayout(option)
            choices.addWidget(option)
        help_label = QLabel('Command-only key (Ctrl+Alt+C): "ask Hermes …", "new Hermes session", "stop Hermes". '
                            'Use "read the rest" or "repeat that" for the reply.')
        help_label.setWordWrap(True)
        choices.addWidget(help_label)
        layout.addWidget(self.options)
        self.options.setVisible(self.enabled.isChecked())
        guard = getattr(owner, '_visit_callback', lambda callback, *widgets: callback)
        self._acceptance_callback = guard(self._record_acceptance, self)
        self.enabled.toggled.connect(guard(self.toggle, self))
        owner._widgets['_pack_checkboxes']['hermes'] = self.enabled
        owner._widgets['hermes_section'] = self

    def toggle(self, enabled):
        if enabled and not self.accepted:
            if not hermes_notice.request_acceptance(self.owner):
                self.enabled.setChecked(False)
                return
            if not self._acceptance_callback():
                return
        self.options.setVisible(enabled)

    def _record_acceptance(self):
        # Consent survives Close, rebuilds and unrelated Apply operations.
        app = self.owner.app
        with getattr(app, '_config_lock', None) or nullcontext():
            app.config.setdefault('hermes', {})['notice_accepted'] = True
            app.save_config()
        self.accepted = True
        return True

    def updates(self):
        settings = validated_settings({"profile": self.profile.text(), "model": self.model.text(),
                                       "effort": self.effort.currentData()})
        return {**(self.owner.app.config.get("hermes") or {}), **settings,
                "notice_accepted": self.accepted or cfg_get(self.owner.app.config, 'hermes.notice_accepted') is True}


def add_section(owner, layout):
    if installed():
        layout.addWidget(HermesSection(owner))
