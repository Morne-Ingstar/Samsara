"""Speech hardware notices shared by Compact and Full displays (917)."""
from __future__ import annotations

_dialogs = set()
# PLACEHOLDER COPY (917)
RESTART_NOTICE = 'Restart Samsara to apply speech hardware changes.'


def hardware_signature(config):
    return config.get('device', 'auto'), config.get('compute_type', 'float16')


def _notice(text, *, restart=None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    from samsara.ui import theme
    dialog = QMessageBox(QMessageBox.Icon.Information, 'Speech recognition', text,
                         QMessageBox.StandardButton.Ok)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setStyleSheet(theme.build_stylesheet() + f'\nQMessageBox {{ background-color: {theme.BG1}; }}')
    if callable(restart):
        button = dialog.addButton('Restart Samsara', QMessageBox.ButtonRole.AcceptRole)  # PLACEHOLDER COPY (917)
        button.clicked.connect(lambda _checked=False: restart())
    for button in dialog.buttons():
        theme.make_secondary(button)
        button.setMinimumHeight(theme.HIT_TARGET_MIN)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    _dialogs.add(dialog)
    dialog.finished.connect(lambda _result: _dialogs.discard(dialog))
    dialog.destroyed.connect(lambda _object=None: _dialogs.discard(dialog))
    dialog.show()
    return dialog


def show_fallback_notice():
    from samsara.gpu_fallback import FALLBACK_NOTICE
    return _notice(FALLBACK_NOTICE)


def request_settings_restart(app, previous):
    current = hardware_signature(app.config)
    if current == previous:
        return None
    restart = getattr(getattr(app, 'tray_icon', None), '_restart_samsara', None)
    return _notice(RESTART_NOTICE, restart=restart)
