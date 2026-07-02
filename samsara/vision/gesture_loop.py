"""GestureLoop -- daemon poll thread that reads camera frames, classifies hand
poses via MediaPipe, and dispatches Samsara commands on deliberate gestures.

Mirrors the WakeConsumer pattern:
  - explicit start() / stop() lifecycle
  - daemon thread, never blocks the Qt/main thread
  - device-loss recovery: camera re-acquired automatically
  - all dispatch goes through the existing command executor path or direct
    app method calls -- no new dispatch channel invented here

The four V1 actions and their dispatch paths:

    dictation_toggle  -- app.start_recording() / app.stop_recording() (toggled)
    ava_mode          -- command executor: "hey ava" (V1: greeting + ready)
    stop_cancel       -- app.cancel_recording() + coordinator.cancel_speech()
    window_chooser    -- command executor: "show numbers"

Any other action string is forwarded verbatim to process_text() with
force_commands=True, so gestures are fully remappable via config without
changing code.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Suppress MediaPipe / oneDNN noise before the import
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")


class GestureLoop:
    """Daemon thread subscribing to CameraService and dispatching on poses.

    Args:
        app:            DictationApp instance (stored; no Qt access from thread).
        camera:         CameraService instance.
        gesture_config: The ``gesture`` sub-dict from app.config.
    """

    def __init__(self, app, camera, gesture_config: dict) -> None:
        self._app = app
        self._camera = camera
        self._cfg = gesture_config

        self._running = False
        self._thread: threading.Thread | None = None
        self._reader = None
        self._mp_hands = None
        self._recognizer = None

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Begin the gesture poll loop. Idempotent."""
        if self._running:
            return
        self._reader = self._camera.subscribe()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="gesture-loop"
        )
        self._thread.start()
        logger.info("[GESTURE] Loop started")

    def stop(self) -> None:
        """Stop the poll loop and release MediaPipe resources."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._reader is not None:
            self._camera.unsubscribe(self._reader)
            self._reader = None
        if self._mp_hands is not None:
            try:
                self._mp_hands.close()
            except Exception as e:
                logger.debug(f"stop: {e}")
            self._mp_hands = None
        logger.info("[GESTURE] Loop stopped")

    # ---- Poll loop ----------------------------------------------------------

    def _poll_loop(self) -> None:
        import mediapipe as mp
        from .gesture_recognizer import GestureRecognizer, classify_pose

        cfg = self._cfg
        min_det = cfg.get("min_detection_confidence", 0.6)
        min_trk = cfg.get("min_tracking_confidence", 0.5)
        hold_ms = cfg.get("hold_ms", 350)
        refractory = cfg.get("refractory_neutral_frames", 8)
        pose_map: dict[str, str] = cfg.get(
            "poses",
            {
                "open_palm": "dictation_toggle",
                "peace":     "ava_mode",
                "fist":      "stop_cancel",
                "shaka":     "window_chooser",
            },
        )

        hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=min_det,
            min_tracking_confidence=min_trk,
        )
        self._mp_hands = hands
        recognizer = GestureRecognizer(hold_ms=hold_ms, refractory_frames=refractory)
        self._recognizer = recognizer

        import cv2

        logger.info("[GESTURE] MediaPipe Hands initialized (hold=%dms, refractory=%d frames)",
                    hold_ms, refractory)

        last_frame = None

        while self._running:
            if self._reader is None:
                time.sleep(0.01)
                continue

            frame = self._reader.get(timeout=0.1)
            if frame is None:
                recognizer.update("other")
                continue
            if frame is last_frame:
                # Same frame object -- camera hasn't produced a new one yet
                time.sleep(0.005)
                continue
            last_frame = frame

            try:
                pose = self._classify_frame(frame, hands, classify_pose, cv2)
            except Exception as exc:
                logger.warning("[GESTURE] classify error: %s", exc)
                recognizer.update("other")
                continue

            _, fired = recognizer.update(pose)
            if fired:
                action = pose_map.get(pose)
                if action:
                    logger.info("[GESTURE] %s -> %s", pose, action)
                    self._dispatch(action)

        try:
            hands.close()
        except Exception as e:
            logger.debug(f"_poll_loop: {e}")
        self._mp_hands = None

    @staticmethod
    def _classify_frame(frame, hands, classify_pose, cv2) -> str:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = hands.process(rgb)
        if not result.multi_hand_landmarks:
            return "other"
        lm = result.multi_hand_landmarks[0].landmark
        return classify_pose(lm)

    # ---- Dispatch -----------------------------------------------------------

    def _dispatch(self, action: str) -> None:
        """Fire *action* on a short-lived daemon thread so the poll loop is free."""
        threading.Thread(
            target=self._execute_action,
            args=(action,),
            daemon=True,
            name=f"gesture-act-{action}",
        ).start()

    def _execute_action(self, action: str) -> None:
        app = self._app
        try:
            if action == "dictation_toggle":
                if getattr(app, "recording", False):
                    app.stop_recording()
                else:
                    app.start_recording()

            elif action == "stop_cancel":
                if getattr(app, "recording", False):
                    app.cancel_recording()
                coordinator = getattr(app, "audio_coordinator", None)
                if coordinator is not None:
                    coordinator.cancel_speech()

            elif action == "ava_mode":
                app.command_executor.process_text("hey ava", app, force_commands=True)

            elif action == "window_chooser":
                app.command_executor.process_text("show numbers", app, force_commands=True)

            else:
                # Treat as a voice command phrase -- fully remappable via config
                app.command_executor.process_text(action, app, force_commands=True)

        except Exception as exc:
            logger.error("[GESTURE] dispatch %r failed: %s", action, exc)
