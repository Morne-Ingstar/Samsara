"""Pause and safely resume media sessions for a recording window.

This deliberately uses Windows' media-session API, rather than synthetic
media keys.  The latter are toggle-like and Windows may debounce them, so
they cannot tell whether a particular app was playing when Samsara started.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from time import monotonic
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from samsara.runtime import thread_registry


logger = logging.getLogger("Samsara")
_missing_api_logged = False
_missing_api_lock = threading.Lock()


class MediaSessionUnavailable(RuntimeError):
    """The Windows media-session API is not available on this machine."""


class MediaSessionLayer(Protocol):
    """Small synchronous adapter; tests provide this instead of Windows."""

    def list_sessions(self) -> list[Any]: ...
    def playback_status(self, session: Any) -> str: ...
    def pause(self, session: Any) -> bool: ...
    def resume(self, session: Any) -> bool: ...
    def observe(self, session: Any, callback: Callable[[], None]) -> Callable[[], None]: ...
    def app_id(self, session: Any) -> str: ...


def _status(value: Any) -> str:
    """Normalize WinRT enums and the plain strings used by test doubles."""
    return str(getattr(value, "name", value)).rsplit(".", 1)[-1].lower()


def _run_operation(operation):
    """winsdk IAsyncOperation is awaitable, but asyncio.run needs a coroutine."""
    async def await_operation():
        return await operation
    return asyncio.run(await_operation())


class WinRTMediaSessionLayer:
    """Windows Runtime adapter, imported only when pause mode is selected."""

    def __init__(self) -> None:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )
        except ImportError as exc:
            raise MediaSessionUnavailable("winsdk media sessions unavailable") from exc
        self._manager_type = GlobalSystemMediaTransportControlsSessionManager
        self._manager = None

    def _get_manager(self):
        if self._manager is None:
            self._manager = _run_operation(self._manager_type.request_async())
        return self._manager

    def list_sessions(self) -> list[Any]:
        return list(self._get_manager().get_sessions())

    def playback_status(self, session: Any) -> str:
        return _status(session.get_playback_info().playback_status)

    def pause(self, session: Any) -> bool:
        return bool(_run_operation(session.try_pause_async()))

    def resume(self, session: Any) -> bool:
        return bool(_run_operation(session.try_play_async()))

    def observe(self, session: Any, callback: Callable[[], None]) -> Callable[[], None]:
        def changed(_sender, _args) -> None:
            callback()

        token = session.add_playback_info_changed(changed)
        return lambda: session.remove_playback_info_changed(token)

    def app_id(self, session: Any) -> str:
        return str(getattr(session, "source_app_user_model_id", "unknown"))


@dataclass
class _PausedSession:
    session: Any
    app_id: str
    remove_observer: Callable[[], None]
    pause_event_deadline: float = 0.0
    status_changed: bool = False


class RecordingMediaPauser:
    """Asynchronously pause playing sessions and conditionally resume them.

    One worker owns the WinRT objects for the entire recording.  ``start``
    returns immediately; ``finish`` merely signals the worker after final
    captured audio is available. Paused readings within one second of our
    request acknowledge it, including duplicates. Any non-paused reading
    (including Playing or Changing), or a later event, opts out of resume.
    """

    def __init__(
        self,
        layer_factory: Callable[[], MediaSessionLayer] = WinRTMediaSessionLayer,
        fallback: Callable[[], None] | None = None,
        fallback_finish: Callable[[], None] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        self._layer_factory = layer_factory
        self._fallback = fallback
        self._fallback_finish = fallback_finish
        self._on_failure = on_failure
        self._finish_requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._paused: list[_PausedSession] = []
        self._playing_at_start: set[str] = set()
        self._pause_attempted: set[str] = set()

    def start(self) -> None:
        self._worker = thread_registry.spawn("samsara.media-pause", self._run, daemon=True)

    def finish(self) -> None:
        """Request conditional resume; safe even if startup is still running."""
        self._finish_requested.set()
        if self._fallback_finish is not None:
            self._fallback_finish()

    def wait(self, timeout: float = 2.0) -> None:
        """Test-only convenience for waiting for the worker to settle."""
        if self._worker is not None:
            self._worker.join(timeout)

    def _use_fallback(self) -> None:
        if not self._finish_requested.is_set() and self._fallback is not None:
            self._fallback()

    def _failed(self, step: str, error: Any) -> None:
        logger.warning("[MEDIA] %s failed: %s", step, error)
        if self._on_failure is not None:
            self._on_failure(step, str(error))

    def _on_status(self, paused: _PausedSession, layer: MediaSessionLayer) -> None:
        try:
            state = layer.playback_status(paused.session)
        except Exception:
            paused.status_changed = True
            return
        # Pause completion and PlaybackInfoChanged can arrive in either order.
        # Repeated Paused readings in the request window are acknowledgements.
        if state != "paused" or monotonic() > paused.pause_event_deadline:
            paused.status_changed = True

    def _run(self) -> None:
        try:
            layer = self._layer_factory()
            sessions = layer.list_sessions()
            # Snapshot before pausing, including players a short hold never reaches.
            readable_sessions = []
            for session in sessions:
                try:
                    state = layer.playback_status(session)
                    if state == "playing":
                        self._playing_at_start.add(layer.app_id(session))
                    readable_sessions.append(session)
                except Exception:
                    # Unknown is not playing-at-start: do not take ownership.
                    # If it later reads Playing, respect that choice at release.
                    logger.warning("[MEDIA] Hold-start session unreadable; skipping session")
            if sessions and not readable_sessions:
                self._failed("media snapshot", "no readable media sessions")
                self._use_fallback()
                return
            sessions = readable_sessions
        except MediaSessionUnavailable as exc:
            global _missing_api_logged
            with _missing_api_lock:
                if not _missing_api_logged:
                    logger.warning("[MEDIA] Media-session API unavailable; using recording duck")
                    _missing_api_logged = True
            self._failed("media-session API", exc)
            self._use_fallback()
            return
        except Exception as exc:
            self._failed("list_sessions", exc)
            self._use_fallback()
            return

        refused = False
        for session in sessions:
            if self._finish_requested.is_set():
                break
            try:
                if layer.playback_status(session) != "playing":
                    continue
                app_id = layer.app_id(session)
                self._pause_attempted.add(app_id)
                paused = _PausedSession(session, app_id, lambda: None)
                paused.pause_event_deadline = monotonic() + 1.0
                paused.remove_observer = layer.observe(
                    session, lambda paused=paused: self._on_status(paused, layer),
                )
                if not layer.pause(session) or layer.playback_status(session) != "paused":
                    paused.remove_observer()
                    refused = True
                    logger.info("[MEDIA] Pause refused by app_id=%s", paused.app_id)
                    self._failed("pause", "app refused the pause request")
                    continue
                with self._lock:
                    self._paused.append(paused)
            except Exception as exc:
                refused = True
                logger.info("[MEDIA] Pause failed for app_id=%s", layer.app_id(session))
                self._failed("pause", exc)

        if refused:
            self._use_fallback()
        logger.info("[MEDIA] Paused %d session(s)", len(self._paused))
        self._finish_requested.wait()
        self._resume_owned(layer)

    def _resume_owned(self, layer: MediaSessionLayer) -> None:
        with self._lock:
            paused, self._paused = self._paused, []
        try:
            playing_elsewhere = False
            for session in layer.list_sessions():
                try:
                    app_id = layer.app_id(session)
                    if (app_id not in self._playing_at_start
                            and app_id not in self._pause_attempted
                            and layer.playback_status(session) == "playing"):
                        playing_elsewhere = True
                        break
                except Exception:
                    # A single closing/unreadable session cannot veto resume.
                    continue
        except Exception as exc:
            playing_elsewhere = True
            logger.warning("[MEDIA] Resume session check failed: %s", exc)
        for item in paused:
            try:
                # A second user pause while already paused is inherently not
                # observable through this API.  The observable cases are
                # conservative: only our unchanged Paused session resumes.
                if (not playing_elsewhere and not item.status_changed
                        and layer.playback_status(item.session) == "paused"):
                    if not layer.resume(item.session):
                        self._failed("resume", "app refused the resume request")
                else:
                    logger.info("[MEDIA] Leaving changed app_id=%s paused", item.app_id)
            except Exception as exc:
                logger.info("[MEDIA] Resume skipped for app_id=%s", item.app_id)
                self._failed("resume", exc)
            finally:
                try:
                    item.remove_observer()
                except Exception:
                    pass


class RecordingDucker:
    """A recording-owned lease; capture and release never wait for COM.

    Finish before/during startup is remembered. The same worker restores its
    lease as soon as startup returns, so a short hold cannot leave a late duck.
    """

    def __init__(self, factory, on_failure):
        self._factory = factory
        self._on_failure = on_failure
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._worker = None

    def start(self):
        with self._lock:
            if self._worker is None and not self._finished.is_set():
                self._worker = thread_registry.spawn("samsara.recording-duck", self._run, daemon=True)

    def finish(self):
        self._finished.set()

    def wait(self, timeout=2.0):
        if self._worker is not None:
            self._worker.join(timeout)

    def _run(self):
        ducker = None
        try:
            ducker = self._factory()
            if self._finished.is_set():
                return
            ducker.start()
            if ducker.last_error:
                self._on_failure("recording duck", ducker.last_error)
            self._finished.wait()
        except Exception as exc:
            self._on_failure("recording duck", str(exc))
        finally:
            if ducker is not None:
                ducker.stop()
