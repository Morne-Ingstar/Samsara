"""Qt system tray icon for Samsara.

Drop-in replacement for the pystray.Icon usage in dictation.py.
Exposes the same attribute interface the rest of the app uses:
    .icon  = pil_image     (property setter, thread-safe)
    .title = "Samsara - X" (property setter, thread-safe)
    .stop()                (thread-safe, hides icon)

Must be created on the Qt thread (via QTimer.singleShot or similar).
All Signal-based methods are safe to call from any thread.
"""

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara.log import get_logger

logger = get_logger(__name__)


class SamsaraTrayQt(QObject):
    """QSystemTrayIcon wrapper matching pystray.Icon's property interface."""

    _icon_sig    = Signal(object)  # PIL Image
    _tooltip_sig = Signal(str)
    _hide_sig    = Signal()

    def __init__(self, app):
        super().__init__()
        self._app  = app
        self._available_update = None
        self._tray = QSystemTrayIcon()
        self._menu = QMenu()
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.messageClicked.connect(self._on_message_clicked)

        # Wire thread-safe signals
        self._icon_sig.connect(self._apply_icon)
        self._tooltip_sig.connect(self._tray.setToolTip)
        self._hide_sig.connect(self._tray.hide)

        # Initial icon + tooltip
        try:
            self._apply_icon(app.create_icon_image(active=False))
        except Exception as e:
            logger.debug(f"__init__: {e}")
        self._tray.setToolTip("Samsara")
        self._tray.show()

        # A visible tray is not proof that startup succeeded: Whisper, CUDA,
        # VAD, and the listening services are still loading at this point.
        # Confirm an update only after DictationApp publishes 100% startup.
        # Until then the detached updater retains the rollback installation.
        self._startup_health_done = False
        self._startup_health_timer = QTimer(self)
        self._startup_health_timer.setInterval(250)
        self._startup_health_timer.timeout.connect(self._poll_startup_health)
        self._startup_health_timer.start()

    # ------------------------------------------------------------------
    # pystray-compatible property interface (all thread-safe)
    # ------------------------------------------------------------------

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, pil_image):
        self._icon_sig.emit(pil_image)

    @property
    def title(self):
        return self._tray.toolTip()

    @title.setter
    def title(self, text: str):
        self._tooltip_sig.emit(str(text))

    def stop(self):
        try:
            self._startup_health_timer.stop()
        except Exception:
            pass
        self._hide_sig.emit()

    # ------------------------------------------------------------------
    # Qt thread methods
    # ------------------------------------------------------------------

    def _poll_startup_health(self):
        """Confirm a replacement only after the whole app is operational."""
        if self._startup_health_done:
            return
        if bool(vars(self._app).get("_startup_failed", False)):
            # Do not acknowledge the new build. The detached helper observes
            # the missing health handshake and restores the previous version.
            self._startup_health_timer.stop()
            return

        progress = vars(self._app).get("_splash_progress", 0)
        if not isinstance(progress, (int, float)) or progress < 100:
            return

        self._startup_health_done = True
        self._startup_health_timer.stop()
        try:
            from samsara.updater import reconcile_update_on_startup

            update_status = reconcile_update_on_startup()
            if update_status is not None:
                if update_status.state == "installed":
                    self._tray.showMessage(
                        "Samsara updated",
                        update_status.message,
                        QSystemTrayIcon.MessageIcon.Information,
                        8000,
                    )
                elif update_status.state in {"failed", "rolled_back"}:
                    self._tray.showMessage(
                        "Samsara update problem",
                        update_status.message,
                        QSystemTrayIcon.MessageIcon.Warning,
                        12000,
                    )
                elif update_status.state == "cleanup_pending":
                    self._tray.showMessage(
                        "Samsara update cleanup pending",
                        "Leftover update files remain from a previous update "
                        f"and cleanup will retry automatically. {update_status.message}",
                        QSystemTrayIcon.MessageIcon.Warning,
                        10000,
                    )
        except Exception as exc:
            logger.warning("[UPDATE] Could not reconcile previous update: %s", exc)

        # The coordinator is a no-op unless the user explicitly enabled
        # once-daily GitHub checks. Waiting for healthy startup means a broken
        # build neither confirms itself nor makes an update-network request.
        try:
            from samsara.ui.update_qt import maybe_start_automatic_update_check

            maybe_start_automatic_update_check(
                self._app, self._show_update_available,
            )
        except Exception as exc:
            logger.warning("[UPDATE] Could not schedule automatic check: %s", exc)

    def _apply_icon(self, pil_image):
        try:
            rgba = pil_image.convert("RGBA")
            data = rgba.tobytes()
            qi = QImage(data, rgba.width, rgba.height,
                        QImage.Format.Format_RGBA8888)
            self._tray.setIcon(QIcon(QPixmap.fromImage(qi)))
        except Exception as exc:
            print(f"[TRAY] Icon update failed: {exc}")

    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            try:
                self._app.show_main_window()
            except Exception as e:
                logger.debug(f"_on_activated: {e}")

    def _open_update_dialog(self):
        from samsara.ui.update_qt import show_update_dialog

        show_update_dialog(
            self._app,
            check_immediately=self._available_update is None,
            initial_release=self._available_update,
        )

    def _show_update_available(self, release):
        """Runs on the Qt thread after an opted-in background check."""
        self._available_update = release
        self._tray.showMessage(
            "Samsara update available",
            f"Version {release.version} is ready. Click this notification or "
            "open the tray menu to install it.",
            QSystemTrayIcon.MessageIcon.Information,
            12000,
        )

    def _on_message_clicked(self):
        if self._available_update is not None:
            self._open_update_dialog()

    def _rebuild_menu(self):
        """Rebuild the full context menu from live app state.

        Called by QMenu.aboutToShow each time the user right-clicks the
        tray icon -- once per menu open, not on every hover.
        """
        app  = self._app
        menu = self._menu
        menu.clear()

        # ---- Show / hide hub ----
        show_act = menu.addAction("Show Samsara")
        show_act.triggered.connect(lambda: app.show_main_window())
        menu.setDefaultAction(show_act)
        menu.addSeparator()

        # ---- Microphone submenu ----
        mic_label = f"[MIC]  {app.get_current_microphone_name()}"
        mic_sub = QMenu(mic_label)
        if not app._is_audio_capture_active():
            try:
                app.available_mics = app.get_available_microphones()
                app._reconcile_microphone_selection()
            except Exception as e:
                logger.debug(f"_rebuild_menu: {e}")
        current_mic = app.config.get('microphone')
        for mic in app.available_mics:
            act = mic_sub.addAction(mic['name'])
            act.setCheckable(True)
            act.setChecked(mic['id'] == current_mic)
            act.triggered.connect(
                lambda checked, mid=mic['id']: app.switch_microphone_and_refresh(mid)
            )
        menu.addMenu(mic_sub)

        # ---- Mode submenu ----
        mode = app.config.get('mode', 'hold')
        mode_sub  = QMenu(f"Mode:  {mode.title()}")
        mode_grp  = QActionGroup(mode_sub)
        mode_grp.setExclusive(True)
        for label, val in [
            ("Hold to Talk",             "hold"),
            ("Toggle (click to start/stop)", "toggle"),
            ("Continuous",               "continuous"),
        ]:
            act = mode_sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(mode == val)
            act.triggered.connect(
                lambda checked, m=val: app.switch_mode_from_tray(m) if checked else None
            )
            mode_grp.addAction(act)
        menu.addMenu(mode_sub)

        # ---- Wake word ----
        ww_phrase = app.config.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)
        ww_act = menu.addAction(f"Wake Word  ({ww_phrase})")
        ww_act.setCheckable(True)
        ww_act.setChecked(bool(app.config.get('wake_word_enabled', False)))
        ww_act.triggered.connect(
            lambda checked: app.set_wake_word_enabled(checked)
        )

        # ---- Streaming mode ----
        stream_act = menu.addAction("Streaming Mode  (CapsLock)")
        stream_act.setCheckable(True)
        stream_act.setChecked(bool(app.config.get('streaming_mode', False)))
        stream_act.triggered.connect(
            lambda checked: app.set_streaming_mode(checked)
        )

        # ---- Gesture lane ----
        gesture_act = menu.addAction("Gesture Lane  (webcam)")
        gesture_act.setCheckable(True)
        gesture_act.setChecked(bool(app.config.get('gesture', {}).get('enabled', False)))
        gesture_act.triggered.connect(
            lambda checked: app.set_gesture_enabled(checked)
        )

        # ---- Snooze submenu ----
        snoozed = getattr(app, 'snoozed', False)
        snooze_sub = QMenu("Snoozed" if snoozed else "Snooze")
        for label, mins in [
            ("5 minutes",    5),
            ("15 minutes",   15),
            ("30 minutes",   30),
            ("1 hour",       60),
            ("Until resumed", None),
        ]:
            act = snooze_sub.addAction(label)
            act.setEnabled(not snoozed)
            act.triggered.connect(
                lambda checked, m=mins: app.snooze_listening(m)
            )
        snooze_sub.addSeparator()
        resume_act = snooze_sub.addAction("Resume now")
        resume_act.setEnabled(snoozed)
        resume_act.triggered.connect(lambda: app.resume_listening())
        menu.addMenu(snooze_sub)

        menu.addSeparator()

        # ---- Daily-use quick access (2026-07-10 declutter pass) ----
        # One click for a daily user with chronic finger-joint pain: the
        # status/mode controls above stay here (operational toggles a user
        # adjusts routinely), plus the reference/visibility windows below.
        # Occasional tools and dev/debug surfaces are grouped into the
        # Tools / Developer submenus further down -- see there for the
        # full placement rationale.
        menu.addAction("Settings").triggered.connect(lambda: app.open_settings())
        update_label = (
            f"Install Samsara v{self._available_update.version}…"
            if self._available_update is not None else
            "Check for Updates…"
        )
        menu.addAction(update_label).triggered.connect(self._open_update_dialog)
        menu.addAction("History").triggered.connect(lambda: app.open_history())
        menu.addAction("Quick Reference").triggered.connect(
            lambda: app.open_quick_reference())

        cr_act = menu.addAction("Command Reference")
        cr_act.setCheckable(True)
        cr_act.setChecked(getattr(getattr(app, 'cheat_sheet', None), '_visible', False))
        cr_act.triggered.connect(lambda: app.toggle_cheat_sheet())

        li_act = menu.addAction("Show Listening Indicator")
        li_act.setCheckable(True)
        li_act.setChecked(bool(app.config.get('listening_indicator_enabled', False)))
        li_act.triggered.connect(lambda: app.toggle_listening_indicator())

        move_act = menu.addAction("Move listening indicator...")
        move_act.triggered.connect(lambda: app.enter_indicator_move_mode())

        menu.addSeparator()

        # ---- Tools submenu: occasionally-used setup/training/review tools ----
        tools_sub = QMenu("Tools")
        tools_sub.addAction("Interactive Tutorial").triggered.connect(
            lambda: app.show_tutorial())
        tools_sub.addSeparator()
        tools_sub.addAction("Mic Setup Guide").triggered.connect(
            lambda: app.open_mic_setup_guide())
        tools_sub.addAction("Ava Guide").triggered.connect(
            lambda: app.open_ava_guide())
        tools_sub.addAction("Voice Training").triggered.connect(
            lambda: app.open_voice_training())
        tools_sub.addAction("Benchmark Review").triggered.connect(
            lambda: app.open_benchmark_review())
        tools_sub.addAction("Correct Last Dictation").triggered.connect(
            lambda: app.open_correction_capture())
        tools_sub.addAction("Stress Test Wizard").triggered.connect(
            lambda: app.open_stress_test_wizard())
        tools_sub.addSeparator()
        tools_sub.addAction("Recalibrate Mic").triggered.connect(
            lambda: app.recalibrate_mic())
        tools_sub.addSeparator()

        cleanup_sub = QMenu("Cleanup")
        cleanup_grp = QActionGroup(cleanup_sub)
        cleanup_grp.setExclusive(True)
        cleanup_mode = app.config.get('cleanup_mode', 'clean')
        for label, val in [
            ("Clean  (remove fillers)", "clean"),
            ("Verbatim  (no cleanup)",  "verbatim"),
        ]:
            act = cleanup_sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(cleanup_mode == val)
            act.triggered.connect(
                lambda checked, v=val: app.set_cleanup_mode(v) if checked else None
            )
            cleanup_grp.addAction(act)
        tools_sub.addMenu(cleanup_sub)

        tools_sub.addSeparator()
        info_hotkey = tools_sub.addAction(f"Hotkey:  {app.config.get('hotkey', '?')}")
        info_hotkey.setEnabled(False)
        info_model = tools_sub.addAction(f"Model:  {app.config.get('model_size', '?')}")
        info_model.setEnabled(False)

        menu.addMenu(tools_sub)

        # ---- Developer submenu: debug/diagnostic surfaces ----
        dev_sub = QMenu("Developer")
        dev_sub.addAction("Dictation Diagnostics").triggered.connect(
            lambda: app.open_dictation_diagnostics())
        dev_sub.addAction("Wake Word Debug").triggered.connect(
            lambda: app.open_wake_word_debug())
        dev_sub.addAction("View Live Log").triggered.connect(
            lambda: app.open_log_viewer())
        dev_sub.addSeparator()
        dev_sub.addAction("Calibrate Echo Cancellation").triggered.connect(
            lambda: app.calibrate_echo_cancellation())
        dev_sub.addSeparator()
        dev_sub.addAction("Open Config Folder").triggered.connect(
            lambda: app.open_config_folder())
        dev_sub.addSeparator()
        dev_sub.addAction("Preview First-Run (fresh profile)").triggered.connect(
            lambda: app.preview_first_run())
        logs_sub = QMenu("View Logs")
        logs_sub.addAction("Main Log").triggered.connect(
            lambda: app.open_main_log())
        logs_sub.addAction("Voice Training Log").triggered.connect(
            lambda: app.open_voice_training_log())
        dev_sub.addMenu(logs_sub)

        menu.addMenu(dev_sub)
        menu.addSeparator()

        menu.addAction("Exit").triggered.connect(lambda: app.quit_app())
