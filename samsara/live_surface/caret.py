"""Bounded, off-UI-thread caret lookup for temporary surface avoidance.

Windows providers are injected so the geometry policy is testable without a
desktop.  Production callers may provide a UIA lookup and a GetGUIThreadInfo
fallback; neither provider is allowed to block the Qt thread or multiply
workers when a provider stalls.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
from typing import Callable

from samsara.live_surface.placement import Rect

DEADLINE_S = .050
CACHE_S = .500


@dataclass(frozen=True)
class CaretBounds:
    rect: Rect
    hwnd: int | None = None


class CaretLocator:
    """One outstanding lookup, 50ms freshness deadline and 500ms cache."""

    def __init__(self, uia: Callable[[], CaretBounds | None] | None = None,
                 gui_thread_info: Callable[[], CaretBounds | None] | None = None,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self._uia, self._fallback, self._clock = uia, gui_thread_info, clock
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="samsara-caret")
        self._future: Future | None = None
        self._cache: CaretBounds | None = None
        self._cache_at = float("-inf")
        self._lock = threading.Lock()

    def request(self) -> CaretBounds | None:
        """Return fresh cached data or start one bounded asynchronous lookup."""
        now = self._clock()
        with self._lock:
            if self._cache is not None and now - self._cache_at <= CACHE_S:
                return self._cache
            if self._future is not None and self._future.done():
                try:
                    value = self._future.result()
                except Exception:
                    value = None
                self._future = None
                if value is not None:
                    self._cache, self._cache_at = value, now
                    return value
            if self._future is None:
                self._future = self._executor.submit(self._lookup)
        return None

    def _lookup(self) -> CaretBounds | None:
        started = self._clock()
        for provider in (self._uia, self._fallback):
            if provider is None or self._clock() - started > DEADLINE_S:
                continue
            try:
                value = provider()
            except Exception:
                value = None
            if value is not None:
                return value
        return None

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
