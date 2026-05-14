"""WinRT async/sync bridge.

WinRT coroutines cannot be awaited from ordinary synchronous code and cannot
be driven by asyncio.run() per-call (event-loop spin-up overhead adds ~10 ms
per call, which compounds badly for rapid TTS requests).

The solution is one persistent event loop in a daemon thread. All WinRT async
calls are submitted via run_coroutine_threadsafe and block the calling thread
only until the WinRT operation completes.

Usage:
    from samsara.tts.winrt_helper import get_helper

    helper = get_helper()
    result = helper.run_sync(some_winrt_coroutine())
"""

import asyncio
import threading


class WinRTHelper:
    """Bridges WinRT async calls into Samsara's synchronous architecture."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="winrt-event-loop"
        )
        self._thread.start()

    def run_sync(self, awaitable):
        """Run a WinRT awaitable on the background loop; block until done.

        Args:
            awaitable: a Python coroutine OR a WinRT IAsyncOperation /
                IAsyncOperationWithProgress. Both are awaitable in Python
                but only Python coroutines are accepted by
                run_coroutine_threadsafe. WinRT operations are wrapped in
                a thin async shim automatically.

        Returns:
            The result of the awaitable.

        Raises:
            Any exception raised inside the awaitable.
        """
        import inspect

        if inspect.iscoroutine(awaitable):
            coro = awaitable
        else:
            # WinRT IAsyncOperation -- wrap in a coroutine shim.
            async def _wrap():
                return await awaitable
            coro = _wrap()

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def shutdown(self):
        """Stop the background event loop and join the thread."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


# Module-level singleton. Engine code calls get_helper() rather than
# instantiating directly, so all synthesis calls share one event loop.
_helper: WinRTHelper | None = None
_helper_lock = threading.Lock()


def get_helper() -> WinRTHelper:
    """Return (creating if necessary) the shared WinRTHelper singleton."""
    global _helper
    if _helper is None:
        with _helper_lock:
            if _helper is None:
                _helper = WinRTHelper()
    return _helper
