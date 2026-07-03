"""CameraService -- single cv2.VideoCapture owner shared across gesture loop
and future vision probe.

The gesture loop subscribes for a steady stream of low-res frames via
subscribe().  The vision probe (Phase 2) calls snapshot() which pauses the
gesture stream, grabs one high-res frame, then restores the gesture profile.

Privacy / power: when the gesture lane is disabled or the app is idle, stop()
releases the capture handle completely so the camera LED goes dark.
"""

import logging
import threading
import time

import cv2

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

_GESTURE_PROFILE = {"width": 640, "height": 480, "fps": 30}
_SNAPSHOT_PROFILE = {"width": 1280, "height": 720}

_MAX_CONSECUTIVE_FAILURES = 10
_REACQUIRE_DELAY_S = 5.0


class FrameReader:
    """Handle returned by CameraService.subscribe(). Thread-safe."""

    def __init__(self):
        self._frame = None
        self._seq = 0
        self._lock = threading.Lock()
        self._event = threading.Event()

    def _update(self, frame):
        with self._lock:
            self._frame = frame
            self._seq += 1
        self._event.set()
        self._event.clear()

    def get(self, timeout: float = 0.1):
        """Block up to *timeout* seconds for a new frame; return it or None."""
        self._event.wait(timeout)
        with self._lock:
            return self._frame

    def get_nowait(self):
        """Return the latest frame without blocking, or None."""
        with self._lock:
            return self._frame


class CameraService:
    """Singleton owner of the cv2.VideoCapture handle.

    Usage::

        svc = CameraService.get_instance()
        svc.start(device_index=0, profile=_GESTURE_PROFILE)
        reader = svc.subscribe()
        frame = reader.get()          # gesture loop
        frame = svc.snapshot()        # vision probe (Phase 2)
        svc.stop()
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "CameraService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._cap = None
        self._cap_lock = threading.Lock()

        self._running = False
        self._poll_thread: threading.Thread | None = None

        # Pause/resume mechanism for snapshot()
        self._run_event = threading.Event()   # set = poll loop should read frames
        self._paused_event = threading.Event()  # set = poll loop has acknowledged pause
        self._run_event.set()

        # Active subscribers (FrameReader objects)
        self._readers: list[FrameReader] = []
        self._readers_lock = threading.Lock()

        self._active_profile: dict = {}
        self._consecutive_failures: int = 0

    # ---- Lifecycle ----------------------------------------------------------

    def start(self, device_index: int = 0, profile: dict | None = None) -> None:
        """Open the camera and begin the frame-read loop. Idempotent."""
        if self._running:
            return
        self._device_index = device_index
        profile = profile or _GESTURE_PROFILE
        if not self._open_cap(profile):
            logger.error("[CAM] Failed to open camera %d", device_index)
            return
        self._running = True
        self._run_event.set()
        self._poll_thread = thread_registry.spawn(
            "gesture-cam", self._poll_loop, daemon=True
        )
        logger.info("[CAM] CameraService started (device=%d, profile=%s)", device_index, profile)

    def stop(self) -> None:
        """Stop the frame loop and release the camera handle (LED goes dark)."""
        self._running = False
        self._run_event.set()  # unblock if paused
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None
        self._release_cap()
        logger.info("[CAM] CameraService stopped")

    def subscribe(self) -> FrameReader:
        """Return a FrameReader that receives the latest frames."""
        reader = FrameReader()
        with self._readers_lock:
            self._readers.append(reader)
        return reader

    def unsubscribe(self, reader: FrameReader) -> None:
        with self._readers_lock:
            try:
                self._readers.remove(reader)
            except ValueError as e:
                logger.debug(f"unsubscribe: {e}")

    # ---- Snapshot (Phase 2 vision probe entry point) -----------------------

    def snapshot(self, high_res: bool = True):
        """Pause gesture stream, grab one frame at the requested profile, revert.

        Returns an BGR ndarray or None on failure. The gesture stream resumes
        automatically before this method returns.
        """
        if not self._running or self._cap is None:
            return None

        # Pause the frame loop
        self._run_event.clear()
        self._paused_event.wait(timeout=1.0)

        frame = None
        try:
            with self._cap_lock:
                profile = _SNAPSHOT_PROFILE if high_res else _GESTURE_PROFILE
                self._apply_profile(profile)
                ret, frame = self._cap.read()
                self._apply_profile(_GESTURE_PROFILE)
                if not ret:
                    frame = None
        finally:
            self._paused_event.clear()
            self._run_event.set()

        return frame

    # ---- Internal -----------------------------------------------------------

    def _open_cap(self, profile: dict) -> bool:
        with self._cap_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            cap = cv2.VideoCapture(self._device_index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self._device_index)
            if not cap.isOpened():
                return False
            self._cap = cap
            self._apply_profile(profile)
            return True

    def _release_cap(self) -> None:
        with self._cap_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
        self._active_profile = {}

    def _apply_profile(self, profile: dict) -> None:
        if self._cap is None:
            return
        if profile.get("width"):
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, profile["width"])
        if profile.get("height"):
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, profile["height"])
        if profile.get("fps"):
            self._cap.set(cv2.CAP_PROP_FPS, profile["fps"])
        self._active_profile = profile

    def _poll_loop(self) -> None:
        while self._running:
            # Snapshot pause: signal paused, wait for resume
            if not self._run_event.is_set():
                self._paused_event.set()
                self._run_event.wait()
                continue

            with self._cap_lock:
                cap = self._cap
            if cap is None:
                time.sleep(0.02)
                continue

            ret, frame = cap.read()

            if not ret:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "[CAM] %d consecutive read failures -- attempting re-open",
                        self._consecutive_failures,
                    )
                    time.sleep(_REACQUIRE_DELAY_S)
                    if not self._open_cap(_GESTURE_PROFILE):
                        logger.error("[CAM] Re-open failed; retrying in %ss", _REACQUIRE_DELAY_S)
                    else:
                        self._consecutive_failures = 0
                        logger.info("[CAM] Camera re-acquired")
                else:
                    time.sleep(0.005)
                continue

            self._consecutive_failures = 0
            with self._readers_lock:
                for reader in self._readers:
                    reader._update(frame)
