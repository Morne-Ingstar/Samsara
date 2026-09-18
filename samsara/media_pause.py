"""Pause and safely resume media sessions for a recording window.

This deliberately uses Windows' media-session API, rather than synthetic
media keys.  The latter are toggle-like and Windows may debounce them, so
they cannot tell whether a particular app was playing when Samsara started.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol


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
            self._manager = asyncio.run(self._manager_type.request_async())
        return self._manager

    def list_sessions(self) -> list[Any]:
        return list(self._get_manager().get_sessions())

    def playback_status(self, session: Any) -> str:
        return _status(session.get_playback_info().playback_status)

    def pause(self, session: Any) -> bool:
        return bool(asyncio.run(session.try_pause_async()))

    def resume(self, session: Any) -> bool:
        return bool(asyncio.run(session.try_play_async()))

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
    saw_our_pause: bool = False
    status_changed: bool = False


class RecordingMediaPauser:
    """Asynchronously pause playing sessions and conditionally resume them.

    One worker owns the WinRT objects for the entire recording.  ``start``
    returns immediately; ``finish`` merely signals the worker after final
    captured audio is available.  A later status event means the user or app
    changed playback, so the session is intentionally left alone.
    """

    def __init__(
        self,
        layer_factory: Callable[[], MediaSessionLayer] = WinRTMediaSessionLayer,
        fallback: Callable[[], None] | None = None,
    ) -> None:
        self._layer_factory = layer_factory
        self._fallback = fallback
        self._finish_requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._paused: list[_PausedSession] = []

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run, name="samsara.media-pause", daemon=True,
        )
        self._worker.start()

    def finish(self) -> None:
        """Request conditional resume; safe even if startup is still running."""
        self._finish_requested.set()

    def wait(self, timeout: float = 2.0) -> None:
        """Test-only convenience for waiting for the worker to settle."""
        if self._worker is not None:
            self._worker.join(timeout)

    def _use_fallback(self) -> None:
        if not self._finish_requested.is_set() and self._fallback is not None:
            self._fallback()

    def _on_status(self, paused: _PausedSession, layer: MediaSessionLayer) -> None:
        try:
            state = layer.playback_status(paused.session)
        except Exception:
            paused.status_changed = True
            return
        # The Playing -> Paused transition caused by our own pause is expected.
        # Any subsequent event, including another Paused event, is ambiguity
        # and therefore opts out of resume.
        if not paused.saw_our_pause and state == "paused":
            paused.saw_our_pause = True
        else:
            paused.status_changed = True

    def _run(self) -> None:
        try:
            layer = self._layer_factory()
            sessions = layer.list_sessions()
        except MediaSessionUnavailable:
            global _missing_api_logged
            with _missing_api_lock:
                if not _missing_api_logged:
                    logger.warning("[MEDIA] Media-session API unavailable; using recording duck")
                    _missing_api_logged = True
            self._use_fallback()
            return
        except Exception as exc:
            logger.warning("[MEDIA] Media-session API failed; using recording duck: %s", exc)
            self._use_fallback()
            return

        refused = False
        for session in sessions:
            if self._finish_requested.is_set():
                break
            try:
                if layer.playback_status(session) != "playing":
                    continue
                paused = _PausedSession(session, layer.app_id(session), lambda: None)
                paused.remove_observer = layer.observe(
                    session, lambda paused=paused: self._on_status(paused, layer),
                )
                if not layer.pause(session) or layer.playback_status(session) != "paused":
                    paused.remove_observer()
                    refused = True
                    logger.info("[MEDIA] Pause refused by app_id=%s", paused.app_id)
                    continue
                paused.saw_our_pause = True
                with self._lock:
                    self._paused.append(paused)
            except Exception:
                refused = True
                logger.info("[MEDIA] Pause failed for app_id=%s", layer.app_id(session))

        if refused:
            self._use_fallback()
        logger.info("[MEDIA] Paused %d session(s)", len(self._paused))
        self._finish_requested.wait()
        self._resume_owned(layer)

    def _resume_owned(self, layer: MediaSessionLayer) -> None:
        with self._lock:
            paused, self._paused = self._paused, []
        for item in paused:
            try:
                # A second user pause while already paused is inherently not
                # observable through this API.  The observable cases are
                # conservative: only our unchanged Paused session resumes.
                if (not item.status_changed
                        and layer.playback_status(item.session) == "paused"):
                    layer.resume(item.session)
                else:
                    logger.info("[MEDIA] Leaving changed app_id=%s paused", item.app_id)
            except Exception:
                logger.info("[MEDIA] Resume skipped for app_id=%s", item.app_id)
            finally:
                try:
                    item.remove_observer()
                except Exception:
                    pass
