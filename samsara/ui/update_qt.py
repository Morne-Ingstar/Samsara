"""Qt update dialog and opt-in automatic-check coordinator.

All network, hashing, extraction, and staging work runs off the Qt thread.
Automatic checks are caller-controlled and remain off unless the saved
``updates.automatic_checks`` setting is explicitly true.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
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


logger = get_logger(__name__)
AUTO_CHECK_INTERVAL_S = 24 * 60 * 60

_dialog: "_UpdateDialog | None" = None


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} bytes"


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
        self.setStyleSheet(
            "QDialog { background: #0A0A0B; color: #E8E8EA; }"
            "QLabel { color: #D4D4D8; font-size: 13px; }"
            "QPushButton { min-height: 38px; padding: 0 18px; "
            "background: #17171A; border: 1px solid #3F3F46; border-radius: 7px; }"
            "QPushButton:hover { border-color: #5EEAD4; }"
            "QPushButton:disabled { color: #66666D; }"
            "QProgressBar { background: #17171A; border: 1px solid #3F3F46; "
            "border-radius: 5px; text-align: center; }"
            "QProgressBar::chunk { background: #5EEAD4; border-radius: 4px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("Samsara Updates")
        title.setStyleSheet("color: #5EEAD4; font-size: 19px; font-weight: 700;")
        layout.addWidget(title)

        privacy = QLabel(
            "Samsara has no update server or push-notification channel. Checks "
            "contact GitHub Releases directly. Samsara sends no audio, dictated "
            "text, settings, logs, or device identifier. GitHub receives the "
            "ordinary IP address and request headers required for the connection."
        )
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        self._status = QLabel("Ready to check for a packaged update.")
        self._status.setObjectName("updateStatusLabel")
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Update status")
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setObjectName("updateProgressBar")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_button = QPushButton("Close")
        close_button.setObjectName("closeUpdateDialogButton")
        close_button.clicked.connect(self.close)
        self._primary = QPushButton("Check Now")
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

    def start_check(self) -> None:
        if self._phase in {"checking", "preparing"}:
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

        def _worker():
            try:
                release = check_for_update(current_version=__version__)
            except Exception as exc:
                logger.warning("[UPDATE] Manual update check failed: %s", exc)
                self._check_finished.emit(None, str(exc))
                return
            self._check_finished.emit(release, "")

        thread_registry.spawn("update-manual-check", _worker, daemon=True)

    def _primary_clicked(self) -> None:
        if self._phase == "available" and self._release is not None:
            self._start_install()
        else:
            self.start_check()

    def _on_check_finished(self, release, error: str) -> None:
        if self._closed:
            self.deleteLater()
            return
        self._primary.setEnabled(True)
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
        self._release = release
        self._phase = "available"
        self._primary.setEnabled(True)
        self._primary.setText(f"Install v{release.version}…")
        self._primary.setAccessibleName(f"Install Samsara version {release.version}")
        self._status.setText(
            f"Samsara v{release.version} is available — you have v{__version__}. "
            f"Download size: {_format_bytes(release.asset_size)}."
        )

    def _start_install(self) -> None:
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

        def _progress(downloaded: int, total: int):
            self._progress_changed.emit(downloaded, total)

        def _worker():
            try:
                prepared = prepare_update(release, progress_callback=_progress)
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

        try:
            launch_prepared_update(prepared, current_pid=os.getpid())
        except Exception as exc:
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
        QTimer.singleShot(0, self.app.quit_app)


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


def maybe_start_automatic_update_check(app, on_available) -> bool:
    """Start one explicitly enabled, at-most-daily GitHub check.

    Returns ``True`` only when a worker was started. Failures are logged but
    intentionally do not interrupt a user's startup.
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

    def _worker():
        try:
            release = check_for_update(current_version=__version__)
        except Exception as exc:
            logger.info("[UPDATE] Automatic check unavailable: %s", exc)
            return
        if release is not None:
            qt_runtime.post(lambda: on_available(release))

    thread_registry.spawn("update-automatic-check", _worker, daemon=True)
    return True


__all__ = [
    "AUTO_CHECK_INTERVAL_S",
    "maybe_start_automatic_update_check",
    "show_update_dialog",
]
