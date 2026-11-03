"""One delivery policy for Windows notifications; user reminders bypass it."""

LEVELS = ('important', 'all', 'off')
CLASSES = ('important', 'info', 'reminder')


def saved_level(config):
    """Existing users and invalid saved values get the quiet default."""
    value = config.get('notifications_level', 'important')
    return value if value in LEVELS else 'important'


def notify(config, level, deliver):
    """Call deliver only when this class is enabled; report actual admission.

    Read configuration at delivery time, on the UI thread for tray balloons,
    so queued notices respect the latest saved preference.
    """
    if level not in CLASSES:
        raise ValueError(f'Unknown notification class: {level!r}')
    preference = saved_level(config)
    if level != 'reminder' and not (
        preference == 'all' or (preference == 'important' and level == 'important')
    ):
        return False
    deliver()
    return True


class TrayNotifications:
    """Notification methods bound to the tray QObject, with no separate lifetime.

    The host owns the queued signal, tray icon and app. Keep the shell dispatch
    and click navigation here alongside the policy they implement.
    """

    def notify_warning(self, title: str, text: str) -> None:
        """Important availability warning, callable from any thread (1020)."""
        from PySide6.QtWidgets import QSystemTrayIcon
        self._notify('important', title, text, QSystemTrayIcon.MessageIcon.Warning, 12000)

    def _show_warning_balloon(self, title: str, text: str) -> None:
        self.notify_warning(title, text)

    def notify_info(self, title: str, text: str) -> None:
        self._notify('info', title, text)

    def _notify(self, level, title, text, icon=None, timeout=10000):
        if icon is None:
            from PySide6.QtWidgets import QSystemTrayIcon
            icon = QSystemTrayIcon.MessageIcon.Information
        self._notification_sig.emit(level, str(title), str(text), icon, timeout)

    def _deliver_notification(self, level, title, text, icon, timeout):
        def deliver():
            # PLACEHOLDER COPY (1020): QSystemTrayIcon has no action buttons.
            body = text if level == 'reminder' else (
                text + '\n\nClick to change Windows notifications.'
            )
            self._tray.showMessage(title, body, icon, timeout)
            self._last_notification_class = level

        return notify(self._app.config, level, deliver)

    def _on_message_clicked(self):
        if self._last_notification_class not in ('important', 'info'):
            return
        from samsara.ui.settings_qt import SettingsQt

        settings = getattr(self._app, '_settings_qt', None)
        if settings is None:
            settings = self._app._settings_qt = SettingsQt(self._app)
        settings._show_preferences('General')
        settings._window.focus_setting('notifications_level')
