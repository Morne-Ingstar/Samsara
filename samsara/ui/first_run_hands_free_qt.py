"""Explicit hands-free consent in setup; no live configuration writes (620)."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from samsara import config_defaults
from samsara.ui import theme


class HandsFreeOffer(QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        from samsara.ui.first_run_pages_qt import SetupSegmentedChoice, label

        self.config = config
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SPACE_1)
        row = QHBoxLayout()
        # Stand-in wording; owner review is recorded in ASK.json.
        title = label('Turn on hands-free now?')
        row.addWidget(title, 1)
        self.combo = QComboBox(self)
        self.combo.addItem('Yes', True)
        self.combo.addItem('No', False)
        self.combo.setCurrentIndex(0 if config_defaults.cfg_get(config, 'command_mode.enabled') else 1)
        self.combo.hide()
        self.segments = SetupSegmentedChoice(self.combo)
        self.segments.setAccessibleName(title.text())
        for button in self.segments.buttons:
            button.setMinimumHeight(44)
            button.setAccessibleName(f'{title.text()} {button.text()}')
        row.addWidget(self.segments, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(row)
        self.description = label('')
        column.addWidget(self.description)
        self.refresh(config_defaults.cfg_get(config, 'command_mode.button'))

    def refresh(self, button):
        from samsara.ui.settings.shared_qt import _readable_hotkey

        key = _readable_hotkey(button)
        action = (f'Tap {key} to start and tap it again to stop'
                  if config_defaults.cfg_get(self.config, 'command_mode.mode') == 'toggle'
                  else f'Hold {key} to start and release it to stop')
        self.description.setText(f'{action} a hands-free session for dictation and commands.')
