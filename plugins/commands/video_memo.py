"""Voice entry points for optional Camera-pack video memos."""
from __future__ import annotations

import logging
import threading

from samsara.plugin_commands import command
from samsara.session_modes import DispatchOutcome
from samsara.video_memo import (
    CAMERA_PACK_NOTICE, CameraUnavailable, VideoMemoCapture, VideoMemoError,
    VideoMemoResult,
)

logger = logging.getLogger("Samsara")


class VideoMemoController:
    def __init__(self, app):
        self.app = app
        self._lock = threading.RLock()
        self._capture = None
        self._starting = False
        self._cancel_start = threading.Event()
        self._unregister = None
        self._hotkey = None
        self._route_stop = False

    @property
    def active(self):
        with self._lock:
            return self._starting or bool(self._capture and self._capture.active)

    def _schedule_notice(self, text, kind="warning", *, after_s=0.18):
        def show():
            chip = getattr(self.app, "_show_outcome_chip", None)
            if callable(chip):
                schedule = getattr(self.app, "_schedule_ui", None)
                if callable(schedule):
                    schedule(chip, text, kind)
                else:
                    chip(text, kind)

        timer = threading.Timer(after_s, show)
        timer.daemon = True
        timer.start()

    def _speak(self, text):
        coordinator = getattr(self.app, "audio_coordinator", None)
        if coordinator is not None:
            try:
                coordinator.speak(text, category="agent_response", interruptible=False)
                return
            except Exception:
                logger.debug("[VIDEO-MEMO] Speech notification failed", exc_info=True)
        engine = getattr(self.app, "tts_engine", None)
        if engine is not None:
            try:
                engine.speak(text)
            except Exception:
                logger.debug("[VIDEO-MEMO] TTS notification failed", exc_info=True)

    def _cleanup_hooks(self):
        unregister, self._unregister = self._unregister, None
        if callable(unregister):
            try:
                unregister()
            except Exception:
                logger.debug("[VIDEO-MEMO] Could not unregister session hook", exc_info=True)
        hotkey, self._hotkey = self._hotkey, None
        if hotkey is not None:
            try:
                import keyboard  # noqa: PLC0415
                keyboard.remove_hotkey(hotkey)
            except Exception:
                logger.debug("[VIDEO-MEMO] Could not remove finish hotkey", exc_info=True)

    def start(self):
        with self._lock:
            if self._starting or (self._capture and self._capture.active):
                self._schedule_notice("A video memo is already recording", "warning")
                return None
            self._starting = True
            self._cancel_start.clear()
        try:
            register = getattr(self.app, "register_active_capture_interceptor", None)
            if not callable(register):
                raise VideoMemoError("Voice capture controls are unavailable")
            self._unregister = register(self.intercept)
            import keyboard  # noqa: PLC0415 -- core hotkey dependency
            key = (getattr(self.app, "config", {}) or {}).get("cancel_hotkey", "escape")
            self._hotkey = keyboard.add_hotkey(key, self.finish, suppress=False)
        except Exception as exc:
            with self._lock:
                self._starting = False
            self._cleanup_hooks()
            self._report_start_failure(exc)
            return None
        worker = threading.Thread(target=self._countdown_and_start,
                                  name="video-memo-start", daemon=True)
        worker.start()
        return None  # queued: dispatch shows its working chip without an error sound

    def _countdown_and_start(self):
        try:
            config = getattr(self.app, "config", {}) or {}
            try:
                cap = float(config.get("video_memo_max_duration_s", 300))
            except (TypeError, ValueError):
                cap = 300
            capture = VideoMemoCapture(
                getattr(self.app, "_ace_engine", None),
                max_duration_s=cap,
                on_complete=self._capture_completed,
            )
            capture.prepare()
            self._speak("Starting video memo in three seconds.")
            if self._cancel_start.wait(3.0):
                self._cleanup_hooks()
                with self._lock:
                    self._starting = False
                return
            with self._lock:
                if self._cancel_start.is_set():
                    return
                self._capture = capture
            capture.start()
            with self._lock:
                self._starting = False
            self._schedule_notice("Recording video memo", "live", after_s=0)
        except Exception as exc:
            with self._lock:
                self._starting = False
                self._capture = None
            self._cleanup_hooks()
            self._report_start_failure(exc)

    def _report_start_failure(self, error):
        if isinstance(error, CameraUnavailable) or "camera" in str(error).lower() and "pack" not in str(error).lower():
            message = "No camera found. Say take a memo to record audio."
        elif CAMERA_PACK_NOTICE.lower() in str(error).lower() or isinstance(error, ImportError):
            message = CAMERA_PACK_NOTICE
        else:
            message = f"Video memo could not start: {error}"
        self._speak(message)
        self._schedule_notice(message, "warning")

    def _capture_completed(self, result):
        with self._lock:
            route_stop = self._route_stop
            self._capture = None
            self._starting = False
        self._cleanup_hooks()
        if route_stop:
            return
        if result.cancelled:
            message, kind = "Video memo discarded", "warning"
        elif result.path is None:
            message, kind = "Video memo stopped without a saved file", "warning"
        elif result.partial:
            message = f"Partial video memo kept: {result.path.name}"
            kind = "warning"
        else:
            message = f"Video memo saved: {result.path.name}"
            kind = "success"
        if result.error:
            message += f". {result.error}"
        self._speak(message)
        self._schedule_notice(message, kind, after_s=0)

    def _end(self, *, discard=False):
        with self._lock:
            if self._starting and self._capture is None:
                self._cancel_start.set()
                self._starting = False
                self._cleanup_hooks()
                return VideoMemoResult(path=None, cancelled=discard)
            capture = self._capture
            if capture is None or not capture.active:
                return None
            self._route_stop = True
        try:
            result = capture.stop(discard=discard)
        finally:
            with self._lock:
                self._route_stop = False
                still_active = capture.active
                if not still_active:
                    self._capture = None
                    self._starting = False
            if not still_active:
                self._cleanup_hooks()
        return result

    def finish(self):
        result = self._end(discard=False)
        if result is None:
            if self.active:
                message = "Video memo is still finishing"
                self._speak(message)
                self._schedule_notice(message, "warning")
                return True
            return False
        if result.path is not None:
            label = (f"Partial video kept: {result.path.name}" if result.partial
                     else f"Video memo saved: {result.path.name}")
            self._speak(label)
            self._schedule_notice(label, "warning" if result.partial else "success")
        elif not result.cancelled:
            message = "Video memo start cancelled"
            self._speak(message)
            self._schedule_notice(message, "warning")
        return True

    def cancel(self):
        result = self._end(discard=True)
        if result is None:
            return False
        self._speak("Video memo discarded")
        return True

    def intercept(self, normalized, raw_text, _signals):
        if not self.active:
            return None
        if normalized == "cancel":
            result = self._end(discard=True)
            return DispatchOutcome("active_capture_cancelled", {
                "label": ("Video memo discarded" if result and result.cancelled
                          else "Discarding video memo" if result is None else "Video memo cancelled"),
            })
        result = self._end(discard=False)
        if result is None:
            return DispatchOutcome("active_capture_failed", {
                "label": "Video memo is still finishing", "chip_kind": "warning",
            })
        if result.path is None:
            label = "Video memo start cancelled"
        elif result.partial:
            label = f"Partial video kept: {result.path.name}"
        else:
            label = f"Video memo saved: {result.path.name}"
        self._speak(label)
        return DispatchOutcome("active_capture_stopped", {"label": label})


def _controller(app):
    controller = getattr(app, "_video_memo_controller", None)
    if controller is None:
        controller = VideoMemoController(app)
        app._video_memo_controller = controller
    return controller


@command("record video memo", aliases=["video memo", "start video memo"],
         pack="utilities", risk_class="reversible", reversible=True,
         side_effects=["camera", "audio", "file"])
def start_video_memo(app, remainder):
    """Count down, then record webcam video with audio from the ACE ring."""
    return _controller(app).start()


@command("stop memo", aliases=["stop video memo"], pack="utilities",
         risk_class="reversible", reversible=True,
         side_effects=["camera", "audio", "file"])
def stop_video_memo(app, remainder):
    """Finish an active video memo; otherwise leave ordinary routing intact."""
    controller = getattr(app, "_video_memo_controller", None)
    return bool(controller and controller.finish())


__all__ = ["VideoMemoController", "start_video_memo", "stop_video_memo"]
