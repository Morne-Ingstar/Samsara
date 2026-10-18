"""Qt update dialog and opt-in automatic-check coordinator.

All network, hashing, extraction, and staging work runs off the Qt thread.
Automatic checks are caller-controlled and remain off unless the saved
``updates.automatic_checks`` setting is explicitly true.
"""

from __future__ import annotations

import os
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from samsara import __version__
from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara.ui import qt_runtime
from samsara.updater import (
    check_for_update,
    is_frozen_build,
    launch_prepared_update,
    prepare_update,
    update_unavailable_reason,
)
from samsara.ui import theme
from samsara.ui.update_channel_qt import channel_token, includes_betas, save_beta_choice


logger = get_logger(__name__)
AUTO_CHECK_INTERVAL_S = 24 * 60 * 60
_automatic_check_failure_reported = False

_dialog: "_UpdateDialog | None" = None


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} bytes"


_BODY_MIN_WIDTH = 472   # the dialog's 520 minimum less its 24 px margins


class _BodyScroll(QScrollArea):
    """The window's text. It asks for the room its wrapped labels need and
    scrolls, rather than squeezing them, when the screen has less."""

    def sizeHint(self):
        body = self.widget()
        # As if the scrollbar were showing, so the window never asks for one line too few.
        bar = self.verticalScrollBar().sizeHint().width()
        width = max(_BODY_MIN_WIDTH, self.width() - 2 * self.frameWidth()) - bar
        layout = body.layout()
        height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else body.sizeHint().height()
        return QSize(width + 2 * self.frameWidth(), height + 2 * self.frameWidth())


class _UpdateDialog(QDialog):
    _check_finished = Signal(object, str)
    _prepare_finished = Signal(object, str)
    _progress_changed = Signal(int, int)

    def __init__(self, app, *, initial_release=None):
        super().__init__()
        self.app = app
        self._release = initial_release
        self._phase = "available" if initial_release is not None else "idle"
        self._closed = False

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Samsara Updates")
        self.setMinimumWidth(520)
        self.setModal(False)
        self.setStyleSheet(theme.build_stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        self._body = QWidget()
        self._body.setObjectName("updateBody")
        self._body.setStyleSheet("#updateBody { background: transparent; }")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self._scroll = _BodyScroll()
        self._scroll.setObjectName("updateBodyScroll")
        self._scroll.setStyleSheet("#updateBodyScroll { background: transparent; border: none; }")
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(0)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setWidget(self._body)
        self._body.installEventFilter(self)
        layout.addWidget(self._scroll, 1)

        title = QLabel("Samsara Updates")
        title.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_TITLE}px; font-weight: 700;")
        body.addWidget(title)
        body.addWidget(QLabel(f"Current version: v{__version__}"))
        self._previous_row = _PreviousVersionRow(app)
        body.addWidget(self._previous_row)
        from samsara.update_installer import dialog_notice, RELEASES_URL
        installer_notice = dialog_notice()
        self._installer_note = None
        if installer_notice:
            note = QLabel(f'{installer_notice} <a href="{RELEASES_URL}" style="text-decoration:none;color:{theme.ACCENT}">Get the installer</a>')
            note.setObjectName('installerUpdateNotice')
            note.setWordWrap(True)
            note.setOpenExternalLinks(True)
            body.addWidget(note)
            self._installer_note = note

        privacy = QLabel(
            "Samsara has no update server or push-notification channel. Checks "
            "contact GitHub Releases directly. Samsara sends no audio, dictated "
            "text, settings, logs, or device identifier. GitHub receives the "
            "ordinary IP address and request headers required for the connection."
        )
        privacy.setWordWrap(True)
        body.addWidget(privacy)

        self._beta_choice = QCheckBox("Include beta updates")
        self._beta_choice.setObjectName("includeBetaUpdatesCheckbox")
        self._beta_choice.setAccessibleName(self._beta_choice.text())
        self._beta_choice.setChecked(includes_betas(getattr(app, "config", {})))
        self._beta_choice.toggled.connect(self._on_channel_changed)
        body.addWidget(self._beta_choice)
        beta_note = QLabel("Beta updates are experimental and less tested. This does not enable automatic checks.")
        beta_note.setWordWrap(True)
        body.addWidget(beta_note)

        self._status = QLabel("Ready to check for a packaged update.")
        self._status.setObjectName("updateStatusLabel")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Update status")
        body.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setObjectName("updateProgressBar")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        body.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_button = QPushButton("Close")
        theme.make_secondary(close_button)
        close_button.setObjectName("closeUpdateDialogButton")
        close_button.clicked.connect(self.close)
        self._primary = QPushButton("Check Now")
        theme.make_primary(self._primary)
        self._primary.setObjectName("updatePrimaryButton")
        self._primary.setAccessibleName("Check for Samsara updates")
        self._primary.clicked.connect(self._primary_clicked)
        buttons.addWidget(close_button)
        buttons.addWidget(self._primary)
        layout.addLayout(buttons)

        self._check_finished.connect(self._on_check_finished)
        self._prepare_finished.connect(self._on_prepare_finished)
        self._progress_changed.connect(self._on_progress_changed)

        if initial_release is not None:
            self._show_available(initial_release)

    def eventFilter(self, watched, event):
        # A longer message changes what the text needs: ask for the room now.
        if watched is self._body and event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._grow_to_fit)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._previous_row.refresh()
        self._grow_to_fit()
        QTimer.singleShot(0, self._grow_to_fit)   # again once the width has settled

    def _grow_to_fit(self) -> None:
        """Open tall enough for all the text, up to most of the screen; past
        that the text scrolls (never clipped, never on top of the next line)."""
        if self._closed or not self.isVisible():
            return
        screen = self.screen()
        # The room around the text (margins, buttons) is what the window has
        # beyond the scroll area right now.
        wanted = self.height() - self._scroll.height() + self._scroll.sizeHint().height()
        if screen:
            wanted = min(wanted, int(screen.availableGeometry().height() * 0.9))
        if wanted != self.height():
            self.resize(self.width(), wanted)

    def closeEvent(self, event):
        if self._phase == "checking":
            # The urllib request is bounded but not cancellable. Keep the Qt
            # object alive, hidden, until its worker reports back so the worker
            # never emits into an already-destroyed QObject.
            self._closed = True
            self.hide()
            event.ignore()
            return
        if self._phase == "preparing":
            self._status.setText(
                "The verified update is still being prepared. Samsara will restart "
                "only after preparation succeeds."
            )
            event.ignore()
            return
        self._closed = True
        super().closeEvent(event)

    def _on_channel_changed(self, enabled) -> None:
        if not save_beta_choice(self.app, enabled):
            blocked = self._beta_choice.blockSignals(True)
            self._beta_choice.setChecked(includes_betas(getattr(self.app, "config", {})))
            self._beta_choice.blockSignals(blocked)
            self._status.setText("Could not save the update choice. Your previous choice remains in effect.")
            return
        self._release = None
        self._phase = "idle"
        self._primary.setText("Check Now")
        self._primary.setEnabled(True)
        self._status.setText("Update choice saved. Check Now when you are ready.")

    def start_check(self) -> None:
        if self._phase in {"checking", "preparing"} or previous_request_pending():
            return
        reason = update_unavailable_reason()
        if reason:
            self._phase = "unsupported"
            self._primary.setEnabled(False)
            self._status.setText(reason)
            return

        self._phase = "checking"
        self._release = None
        self._primary.setEnabled(False)
        self._primary.setText("Checking…")
        self._status.setText("Checking GitHub Releases…")
        self._check_channel = channel_token(self.app)
        options: dict[str, Any] = {"allow_prerelease": True} if self._check_channel[0] else {}
        self._beta_choice.setEnabled(False)

        def _worker():
            try:
                release = check_for_update(current_version=__version__, **options)
            except Exception as exc:
                logger.warning("[UPDATE] Manual update check failed: %s", exc)
                self._check_finished.emit(None, str(exc))
                return
            self._check_finished.emit(release, "")

        thread_registry.spawn("update-manual-check", _worker, daemon=True)

    def _primary_clicked(self) -> None:
        if self._phase == 'installer':
            from samsara.update_installer import RELEASES_URL
            QDesktopServices.openUrl(QUrl(RELEASES_URL))
        elif self._phase == "available" and self._release is not None:
            self._start_install()
        else:
            self.start_check()

    def _on_check_finished(self, release, error: str) -> None:
        if self._closed:
            self.deleteLater()
            return
        self._primary.setEnabled(True)
        self._beta_choice.setEnabled(True)
        if channel_token(self.app) != getattr(self, "_check_channel", channel_token(self.app)):
            self._release = None
            self._phase = "idle"
            self._primary.setText("Check Now")
            self._status.setText("Update choice changed. Check again for your current choice.")
            return
        if error:
            self._phase = "idle"
            self._primary.setText("Try Again")
            self._status.setText(
                "Couldn’t check for updates. Samsara remains unchanged. "
                "Check your connection and try again."
            )
            return
        if release is None:
            self._phase = "idle"
            self._primary.setText("Check Again")
            self._status.setText(f"Up to date — Samsara v{__version__}.")
            return
        self._show_available(release)

    def _show_available(self, release) -> None:
        if getattr(release,'installer_link_only',False):
            from samsara.update_installer import GUARD_MESSAGE
            self._installer_message(GUARD_MESSAGE)
            self._release = None
            self._phase = 'installer'
            self._primary.setText('Get the installer')
            self._primary.setEnabled(True)
            self._status.setText(GUARD_MESSAGE)
            return
        if str(getattr(release,'asset_url','')).endswith('.exe'):
            self._installer_message('Installed copies update through their installer.')
        self._release = release
        self._phase = "available"
        self._primary.setEnabled(True)
        self._primary.setText(f"Install v{release.version}…")
        self._primary.setAccessibleName(f"Install Samsara version {release.version}")
        self._status.setText(
            f"Samsara v{release.version} is available — you have v{__version__}. "
            f"Download size: {_format_bytes(release.asset_size)}."
        )

    def _installer_message(self, message):
        from samsara.update_installer import RELEASES_URL
        if self._installer_note is None:
            self._installer_note = QLabel()
            self._installer_note.setObjectName('installerUpdateNotice')
            self._installer_note.setWordWrap(True)
            self._installer_note.setOpenExternalLinks(True)
            self._body.layout().insertWidget(1,self._installer_note)
        self._installer_note.setText(f'{message} <a href="{RELEASES_URL}" style="text-decoration:none;color:{theme.ACCENT}">Get the installer</a>')

    def _start_install(self) -> None:
        if previous_request_pending():
            return
        release = self._release
        if release is None:
            return
        reply = QMessageBox.question(
            self,
            "Install Samsara update?",
            f"Samsara v{release.version} will be downloaded, verified, and staged.\n\n"
            "Samsara will then close and restart. Finish or commit any current "
            "dictation before continuing. Your configuration, custom commands, "
            "drop-in command plugins, and CUDA pack will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._phase = "preparing"
        self._primary.setEnabled(False)
        self._primary.setText("Preparing…")
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._status.setText("Downloading the verified update from GitHub…")
        self._prepare_channel = channel_token(self.app)
        options: dict[str, Any] = {"allow_prerelease": True} if self._prepare_channel[0] else {}
        self._beta_choice.setEnabled(False)

        def _progress(downloaded: int, total: int):
            self._progress_changed.emit(downloaded, total)

        def _worker():
            try:
                prepared = prepare_update(release, progress_callback=_progress, **options)
            except Exception as exc:
                logger.exception("[UPDATE] Could not prepare update")
                self._prepare_finished.emit(None, str(exc))
                return
            self._prepare_finished.emit(prepared, "")

        thread_registry.spawn("update-download-prepare", _worker, daemon=True)

    def _on_progress_changed(self, downloaded: int, total: int) -> None:
        if self._closed or self._phase != "preparing":
            return
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(min(100, int(downloaded * 100 / total)))
        else:
            self._progress.setRange(0, 0)
        self._status.setText(
            f"Downloading update: {_format_bytes(downloaded)} of "
            f"{_format_bytes(total) if total else 'unknown size'}…"
        )

    def _on_prepare_finished(self, prepared, error: str) -> None:
        if self._closed:
            return
        self._beta_choice.setEnabled(True)
        if channel_token(self.app) != getattr(self, "_prepare_channel", channel_token(self.app)):
            self._release = None
            self._phase = "idle"
            self._progress.setVisible(False)
            self._primary.setText("Check Now")
            self._primary.setEnabled(True)
            self._status.setText("Update choice changed. Check again before installing.")
            return
        if error or prepared is None:
            self._phase = "available"
            self._progress.setVisible(False)
            self._primary.setEnabled(True)
            self._primary.setText(f"Try v{self._release.version} Again")
            self._status.setText(
                "The update could not be prepared or verified. Samsara remains "
                "unchanged. Details were written to the Live Log."
            )
            return

        # Both archive and installer helpers wait for this intentional exit.
        try:
            allowed = self.app._resolve_parked_draft_before_quit()
        except Exception:
            logger.exception("[UPDATE] Could not resolve pending draft")
            allowed = False
        if not allowed:
            self._progress.setVisible(False)
            self._show_available(self._release)
            return

        try:
            launch_prepared_update(prepared, current_pid=os.getpid())
        except Exception:
            logger.exception("[UPDATE] Could not launch updater helper")
            self._phase = "available"
            self._progress.setVisible(False)
            self._primary.setEnabled(True)
            self._primary.setText(f"Try v{self._release.version} Again")
            self._status.setText(
                "The updater could not start. Samsara remains open and unchanged. "
                "Details were written to the Live Log."
            )
            return

        self._status.setText("Update verified. Closing Samsara and installing now…")
        self._primary.setEnabled(False)
        from samsara.process_exit import trace_exit
        trace_exit("update-helper-launched-requesting-quit", log=logger)
        def quit_after_install():
            self.app.quit_app(_restart_draft_resolved=True)
        QTimer.singleShot(0, quit_after_install)


def previous_entry():
    """Shared row/menu availability; full verification happens after confirmation."""
    from samsara.previous_version import previous_info
    previous = previous_info()
    reason = update_unavailable_reason()
    if previous is None:
        # STAND-IN COPY (651): includes retained builds without the capability.
        return None, "No previous version is available to go back to."
    if reason:
        return previous, reason
    if previous_request_pending():
        return previous, "Preparing the previous version…"
    return previous, ""


class _PreviousVersionRow(QWidget):
    def __init__(self, app):
        super().__init__()
        self._app = app
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.detail = QLabel()
        self.detail.setObjectName('previousVersionDetails')
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        self.button = QPushButton('Go back to the previous version')
        self.button.setObjectName('previousVersionButton')
        self.button.setMinimumHeight(40)
        theme.make_secondary(self.button)
        self.button.clicked.connect(lambda: request_previous_version(app, self.window()))
        layout.addWidget(self.detail)
        layout.addWidget(self.button)
        self.refresh()

    def refresh(self):
        previous, reason = previous_entry()
        if previous:
            date = str(previous['kept_at'])[:10]
            size = previous.get('size_bytes', 0)
            cost = f' · uses ~{_format_bytes(size)}' if isinstance(size, (int, float)) and size > 0 else ''
            self.detail.setText(f"Previous version: v{previous['version']} · kept {date}{cost}" + (f'\n{reason}' if reason else ''))
        else:
            self.detail.setText(reason)
        self.button.setEnabled(not reason)
        self.button.setToolTip(reason)


def previous_dialog(previous, *, failed_version=None, parent=None):
    """Owner-provided confirmation/offer copy, shared with the render proof."""
    dialog = QMessageBox(parent)
    dialog.setWindowTitle('Samsara')
    dialog.setStyleSheet(theme.build_stylesheet())
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    version = previous['version']
    dialog.setText(
        f"Samsara v{failed_version} didn't start twice. Go back to v{version}?"
        if failed_version else f"Go back to v{version}? Samsara will restart."
    )
    go_back = dialog.addButton('Go back', QMessageBox.ButtonRole.AcceptRole)
    keep = dialog.addButton('Keep trying' if failed_version else 'Cancel', QMessageBox.ButtonRole.RejectRole)
    dialog.setDefaultButton(keep)
    dialog.setEscapeButton(keep)
    for button in (go_back, keep):
        button.setMinimumHeight(40)
    return dialog, go_back


_previous_request = None


def previous_request_pending():
    return _previous_request is not None


class _PreviousRequest(QObject):
    finished = Signal(object, str)

    def __init__(self, app, parent, previous):
        super().__init__()
        self.app, self.parent_widget = app, parent
        self.previous = previous
        self.armed = False
        self.finished.connect(self.complete)

    def prepare(self):
        from samsara.previous_version import prepare_revert
        try:
            self.finished.emit(prepare_revert(expected=self.previous), '')
        except Exception as exc:
            self.finished.emit(None, str(exc))

    def complete(self, prepared, error):
        global _previous_request
        try:
            if error:
                raise RuntimeError(error)
            if not self.app._resolve_parked_draft_before_quit():
                from pathlib import Path
                Path(prepared.workspace_dir).rmdir()  # Empty preparation only.
                return
            from samsara.previous_version import launch_revert, mark_healthy
            launch_revert(prepared)
            self.armed = True
            mark_healthy()
            QTimer.singleShot(0, lambda: self.app.quit_app(_restart_draft_resolved=True))
        except Exception as exc:
            logger.exception('[UPDATE] Could not go back to previous version')
            # STAND-IN COPY (651): failure detail remains editable by the owner.
            QMessageBox.warning(self.parent_widget, 'Samsara', f'Could not go back. Samsara is still open.\n{exc}')
        finally:
            if not self.armed:
                _previous_request = None
                if _dialog is not None:
                    _dialog._previous_row.refresh()
                self.deleteLater()


def request_previous_version(app, parent=None):
    """Called on the Qt thread by either Updates or the tray."""
    global _previous_request
    if previous_request_pending() or (_dialog is not None and _dialog._phase in {'checking', 'preparing'}):
        return
    previous, reason = previous_entry()
    if reason:
        return
    dialog, go_back = previous_dialog(previous, parent=parent)
    dialog.exec()
    accepted = dialog.clickedButton() is go_back
    dialog.hide()
    if not accepted:
        return
    _previous_request = _PreviousRequest(app, parent, previous)
    if _dialog is not None:
        _dialog._previous_row.refresh()
    thread_registry.spawn('previous-version-prepare', _previous_request.prepare, daemon=True)


def add_previous_tray_item(menu, app):
    previous, reason = previous_entry()
    if previous is None:
        return None
    action = menu.addAction('Go back to the previous version')
    action.setEnabled(not reason)
    action.setToolTip(reason)
    action.triggered.connect(lambda: request_previous_version(app))
    return action


def startup_previous_offer(previous, version):
    """Only the recovery offer starts Qt early; normal startup stays lightweight."""
    import threading
    from samsara.previous_version import launch_revert, prepare_revert
    finished, answer = threading.Event(), []

    def ask():
        try:
            dialog, go_back = previous_dialog(previous, failed_version=version)
            dialog.exec()
            answer.append(dialog.clickedButton() is go_back)
            dialog.hide()
        finally:
            finished.set()

    qt_runtime.ensure_started()
    qt_runtime.post(ask)
    finished.wait()
    if not answer or not answer[0]:
        return False
    try:
        launch_revert(prepare_revert(expected=previous))  # Main boot thread, never the Qt thread.
    except Exception as exc:
        finished.clear()
        detail = str(exc)

        def show_failure():
            try:
                QMessageBox.warning(None, 'Samsara', f'Could not go back. Samsara will keep trying.\n{detail}')
            finally:
                finished.set()

        qt_runtime.post(show_failure)
        finished.wait()
        return False
    qt_runtime.shutdown()
    return True


def _clear_dialog() -> None:
    global _dialog
    _dialog = None


def show_update_dialog(app, *, check_immediately=False, initial_release=None):
    """Show or focus the singleton update dialog. Must run on the Qt thread."""
    global _dialog
    if _dialog is None:
        _dialog = _UpdateDialog(app, initial_release=initial_release)
        _dialog.destroyed.connect(lambda *_args: _clear_dialog())
    elif initial_release is not None:
        _dialog._show_available(initial_release)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    if check_immediately and initial_release is None:
        QTimer.singleShot(0, _dialog.start_check)
    return _dialog


def maybe_start_automatic_update_check(app, on_available, on_error=None) -> bool:
    """Start one explicitly enabled, at-most-daily GitHub check.

    Returns ``True`` only when a worker was started. Failures are logged and,
    when supplied, reported once without interrupting a user's startup.
    """
    if not is_frozen_build() or update_unavailable_reason():
        return False
    update_settings = app.config.get("updates", {})
    if not isinstance(update_settings, dict):
        return False
    if not bool(update_settings.get("automatic_checks", False)):
        return False

    now = time.time()
    try:
        last_check = float(update_settings.get("last_check_epoch", 0.0))
    except (TypeError, ValueError):
        last_check = 0.0
    if now - last_check < AUTO_CHECK_INTERVAL_S:
        return False

    saved = dict(update_settings)
    saved["last_check_epoch"] = now
    updater = getattr(app, "update_config_and_save", None)
    if callable(updater):
        updater({"updates": saved})
    else:
        logger.warning("[UPDATE] Automatic check skipped: config persistence unavailable")
        return False

    logger.info("[UPDATE] Starting opted-in automatic GitHub release check")
    checked_channel = channel_token(app)
    options: dict[str, Any] = {"allow_prerelease": True} if checked_channel[0] else {}

    def _deliver(callback, value):
        if channel_token(app) == checked_channel:
            callback(value)

    def _worker():
        global _automatic_check_failure_reported
        try:
            release = check_for_update(current_version=__version__, **options)
        except Exception as exc:
            logger.info("[UPDATE] Automatic check unavailable: %s", exc)
            if on_error is not None and not _automatic_check_failure_reported:
                _automatic_check_failure_reported = True
                error = str(exc)
                qt_runtime.post(lambda error=error: _deliver(on_error, error))
            return
        if release is not None:
            qt_runtime.post(lambda: _deliver(on_available, release))

    thread_registry.spawn("update-automatic-check", _worker, daemon=True)
    return True


__all__ = [
    "AUTO_CHECK_INTERVAL_S",
    "maybe_start_automatic_update_check",
    "show_update_dialog",
]
