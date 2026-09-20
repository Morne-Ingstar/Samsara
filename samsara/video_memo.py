"""Optional-camera video memo capture using the app's existing ACE mic ring.

OpenCV and PyAV are imported only when a video memo is started. The module is
safe to import in a core install, where the optional Camera pack is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
import importlib
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from samsara.audio_engine.ring import EMPTY
from samsara.quick_memo import add_memo, memo_dir

logger = logging.getLogger("Samsara")
SAMPLE_RATE = 16000
DEFAULT_MAX_DURATION_S = 300
CAMERA_PACK_NOTICE = "Video memos need the Camera pack"


class VideoMemoError(RuntimeError):
    """A video memo could not start or continue."""


class CameraUnavailable(VideoMemoError):
    """No usable webcam is available."""


@dataclass(frozen=True)
class VideoMemoResult:
    path: Path | None
    partial: bool = False
    cancelled: bool = False
    error: str | None = None
    record: dict | None = None


class _PyAvWriter:
    """Small MP4 muxer adapter; instantiated only inside the optional pack."""

    def __init__(self, path, first_frame, fps, av_module):
        import numpy as np  # noqa: PLC0415 -- already a core dependency

        height, width = np.asarray(first_frame).shape[:2]
        self._av = av_module
        self._container = av_module.open(str(path), mode="w", format="mp4")
        self._video = self._container.add_stream("libx264", rate=fps)
        self._video.width = int(width)
        self._video.height = int(height)
        self._video.pix_fmt = "yuv420p"
        self._audio = self._container.add_stream("aac", rate=SAMPLE_RATE)
        self._audio.layout = "mono"
        self._resampler = av_module.AudioResampler(
            format="fltp", layout="mono", rate=SAMPLE_RATE,
        )
        self._audio_pts = 0

    def write_video(self, image, pts):
        frame = self._av.VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = int(pts)
        frame.time_base = Fraction(1, 30)
        for packet in self._video.encode(frame):
            self._container.mux(packet)

    def write_audio(self, pcm):
        import numpy as np  # noqa: PLC0415

        samples = np.asarray(pcm, dtype=np.int16).reshape(1, -1)
        if not samples.size:
            return
        frame = self._av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._audio_pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self._audio_pts += samples.shape[1]
        for converted in self._resampler.resample(frame):
            for packet in self._audio.encode(converted):
                self._container.mux(packet)

    def close(self):
        try:
            for packet in self._video.encode(None):
                self._container.mux(packet)
            for converted in self._resampler.resample(None):
                for packet in self._audio.encode(converted):
                    self._container.mux(packet)
            for packet in self._audio.encode(None):
                self._container.mux(packet)
        finally:
            self._container.close()


class VideoMemoCapture:
    """One bounded camera+ACE recording; injectable backends keep tests safe."""

    def __init__(self, engine, *, home=None, max_duration_s=DEFAULT_MAX_DURATION_S,
                 camera_factory=None, writer_factory=None, clock=time.monotonic,
                 thread_factory=threading.Thread, on_complete=None, fps=30):
        self.engine = engine
        self.home = home
        self.max_duration_s = max(1.0, min(1800.0, float(max_duration_s)))
        self.camera_factory = camera_factory
        self.writer_factory = writer_factory
        self.clock = clock
        self.thread_factory = thread_factory
        self.on_complete = on_complete
        self.fps = int(fps)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active = False
        self._discard = False
        self._worker = None
        self._camera = None
        self._reader = None
        self._writer = None
        self._first_frame = None
        self._started_at = None
        self._audio_epoch = None
        self._last_audio_at = None
        self._target = None
        self._temporary = None
        self.result = None

    @property
    def active(self):
        with self._lock:
            return self._active

    @staticmethod
    def _timestamp_path(directory):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = directory / f"{stamp}.mp4"
        suffix = 1
        while (path.exists()
               or path.with_name(path.stem + ".recording.mp4").exists()
               or path.with_name(path.stem + "_partial.mp4").exists()):
            path = directory / f"{stamp}_{suffix}.mp4"
            suffix += 1
        return path

    def _load_backends(self):
        av_module = None
        if self.writer_factory is None:
            try:
                av_module = importlib.import_module("av")
            except Exception as exc:
                raise VideoMemoError(CAMERA_PACK_NOTICE) from exc
        if self.camera_factory is None:
            try:
                cv2 = importlib.import_module("cv2")
            except Exception as exc:
                raise VideoMemoError(CAMERA_PACK_NOTICE) from exc

            def camera_factory():
                camera = cv2.VideoCapture(0)
                if not camera.isOpened():
                    camera.release()
                    raise CameraUnavailable("No camera found")
                camera.set(cv2.CAP_PROP_FPS, self.fps)
                return camera

            self.camera_factory = camera_factory
        if self.writer_factory is None:
            self.writer_factory = lambda path, frame: _PyAvWriter(
                path, frame, self.fps, av_module,
            )

    def prepare(self):
        """Check optional codec imports before the spoken countdown begins."""
        if self.engine is None or not getattr(self.engine, "_running", False):
            raise VideoMemoError("The microphone capture is not available")
        self._load_backends()
        return True

    def start(self):
        with self._lock:
            if self._active:
                raise VideoMemoError("A video memo is already recording")
        camera = reader = writer = None
        temporary = None
        try:
            self.prepare()
            camera = self.camera_factory()
            ok, first_frame = camera.read()
            if not ok or first_frame is None:
                raise CameraUnavailable("No camera found")
            directory = memo_dir(self.home) / "video"
            directory.mkdir(parents=True, exist_ok=True)
            target = self._timestamp_path(directory)
            temporary = target.with_name(target.stem + ".recording.mp4")
            writer = self.writer_factory(temporary, first_frame)
            reader = self.engine.register_consumer(f"video-memo-{uuid.uuid4().hex[:8]}")
            with self._lock:
                self._camera = camera
                self._reader = reader
                self._writer = writer
                self._first_frame = first_frame
                self._target = target
                self._temporary = temporary
                self._started_at = self.clock()
                self._last_audio_at = self._started_at
                self._stop.clear()
                self._discard = False
                self._active = True
                self.result = None
                self._worker = self.thread_factory(
                    target=self._record_loop, name="video-memo-capture", daemon=True,
                )
                self._worker.start()
            return True
        except Exception:
            if reader is not None:
                try:
                    self.engine.unregister_consumer(reader)
                except Exception:
                    logger.debug("[VIDEO-MEMO] Could not unregister a failed reader", exc_info=True)
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if camera is not None:
                try:
                    camera.release()
                except Exception:
                    pass
            raise

    def _write_audio(self):
        while True:
            frame = self._reader.read_next()
            if frame is EMPTY:
                return
            epoch = getattr(frame, "device_epoch", None)
            if self._audio_epoch is None:
                self._audio_epoch = epoch
            elif epoch != self._audio_epoch:
                raise VideoMemoError("The microphone changed devices during recording")
            pcm = getattr(frame, "pcm", None)
            if pcm is not None and len(pcm):
                self._writer.write_audio(pcm)
                self._last_audio_at = self.clock()

    def _record_loop(self):
        error = None
        try:
            first = self._first_frame
            self._first_frame = None
            self._writer.write_video(first, 0)
            last_video_pts = 0
            while not self._stop.is_set():
                elapsed = self.clock() - self._started_at
                if elapsed >= self.max_duration_s:
                    break
                ok, image = self._camera.read()
                if not ok or image is None:
                    raise VideoMemoError("The camera stopped responding")
                pts = max(last_video_pts + 1, int(elapsed * self.fps))
                self._writer.write_video(image, pts)
                last_video_pts = pts
                self._write_audio()
                if self.clock() - self._last_audio_at > 5.0:
                    raise VideoMemoError("The microphone stopped responding")
                self._stop.wait(1.0 / max(1, self.fps))
        except Exception as exc:
            error = exc
            logger.warning("[VIDEO-MEMO] Recording ended with an error: %s", exc)
        finally:
            self._finalize(partial=error is not None, error=error)

    def _finalize(self, *, partial=False, error=None):
        with self._lock:
            if not self._active:
                return self.result
            self._active = False
            camera, reader, writer = self._camera, self._reader, self._writer
            temporary, target = self._temporary, self._target
            discarded = self._discard
            self._camera = self._reader = self._writer = None
        try:
            if writer is not None:
                writer.close()
        except Exception as exc:
            partial = True
            error = error or exc
            logger.warning("[VIDEO-MEMO] MP4 finalization failed: %s", exc)
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass
        if reader is not None:
            try:
                self.engine.unregister_consumer(reader)
            except Exception as exc:
                logger.warning("[VIDEO-MEMO] Could not unregister the mic reader: %s", exc)

        path = None
        record = None
        if temporary is not None and temporary.exists():
            if discarded:
                temporary.unlink(missing_ok=True)
            else:
                path = (target.with_name(target.stem + "_partial.mp4")
                        if partial else target)
                try:
                    os.replace(temporary, path)
                except OSError as exc:
                    path = temporary
                    partial = True
                    error = error or exc
                    logger.warning("[VIDEO-MEMO] Could not move finished file: %s", exc)
                if path.exists():
                    try:
                        record = add_memo(
                            "Video memo" + (" (partial)" if partial else ""),
                            source="video", home=self.home, video_path=path, partial=partial,
                        )
                    except Exception as exc:
                        error = error or exc
                        logger.warning("[VIDEO-MEMO] Could not add recording to memo index: %s", exc)
        result = VideoMemoResult(
            path=path, partial=partial, cancelled=discarded,
            error=str(error) if error else None, record=record,
        )
        with self._lock:
            self.result = result
        if self.on_complete is not None:
            try:
                self.on_complete(result)
            except Exception:
                logger.exception("[VIDEO-MEMO] Completion callback failed")
        return result

    def stop(self, *, discard=False, timeout=8.0):
        with self._lock:
            if not self._active:
                return self.result
            self._discard = bool(discard)
            self._stop.set()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(timeout)))
        if self.active and (worker is None or not worker.is_alive()):
            return self._finalize(partial=False)
        return self.result


__all__ = [
    "CAMERA_PACK_NOTICE", "DEFAULT_MAX_DURATION_S", "CameraUnavailable",
    "VideoMemoCapture", "VideoMemoError", "VideoMemoResult",
]
