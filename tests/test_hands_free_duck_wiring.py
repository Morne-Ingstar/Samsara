"""Tests for the hands-free capture-window duck WIRING in wake_consumer.py
(2026-07-24 amendment): the safe accessors and the dispatch-wrapper that
every capture-window-close call site (silence-timeout discard, OWW-
rejection, abort_utterance, the toggle FIFO worker, the async dispatch
wrapper) shares.

Deliberately does NOT reconstruct a full WakeConsumer+engine+reader
harness -- that's already exercised end-to-end without regression by the
existing test_wake_consumer_*.py suites (Mock()-based app doubles tolerate
the new getattr(...) calls harmlessly, confirmed by the full regression
run). This file isolates and directly verifies the wiring CONTRACT the
rest of the module relies on: safe-no-op when unwired, forwards when
wired, and the finally-shape wrapper closes even on exception.
"""
from unittest.mock import Mock

from samsara.audio_engine.wake_consumer import WakeConsumer


class TestOpenHandsFreeDuckSafe:
    def test_calls_app_method_when_present(self):
        app = Mock()
        WakeConsumer._open_hands_free_duck_safe(app)
        app._open_hands_free_capture_duck.assert_called_once_with()

    def test_noop_when_app_lacks_the_method(self):
        class Bare:
            pass

        WakeConsumer._open_hands_free_duck_safe(Bare())  # must not raise

    def test_swallows_exception_from_app_method(self):
        app = Mock()
        app._open_hands_free_capture_duck.side_effect = RuntimeError("boom")
        WakeConsumer._open_hands_free_duck_safe(app)  # must not raise


class TestCloseHandsFreeDuckSafe:
    def test_calls_app_method_when_present(self):
        app = Mock()
        WakeConsumer._close_hands_free_duck_safe(app)
        app._close_hands_free_capture_duck.assert_called_once_with(None)

    def test_calls_app_method_with_owner_token(self):
        app = Mock()
        WakeConsumer._close_hands_free_duck_safe(app, owner_token=456)
        app._close_hands_free_capture_duck.assert_called_once_with(456)

    def test_noop_when_app_lacks_the_method(self):
        class Bare:
            pass

        WakeConsumer._close_hands_free_duck_safe(Bare())  # must not raise

    def test_swallows_exception_from_app_method(self):
        app = Mock()
        app._close_hands_free_capture_duck.side_effect = RuntimeError("boom")
        WakeConsumer._close_hands_free_duck_safe(app)  # must not raise


class TestWrapWithDuckClose:
    def test_closes_after_successful_call(self):
        app = Mock()
        fn = Mock()
        wrapped = WakeConsumer._wrap_with_duck_close(app, fn, owner_token=789)

        wrapped(1, 2, kw="x")

        fn.assert_called_once_with(1, 2, kw="x")
        app._close_hands_free_capture_duck.assert_called_once_with(789)

    def test_closes_even_when_wrapped_fn_raises(self):
        app = Mock()

        def fn(*a, **kw):
            raise RuntimeError("transcription blew up")

        wrapped = WakeConsumer._wrap_with_duck_close(app, fn, owner_token=456)

        try:
            wrapped()
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the wrapped exception to propagate")

        app._close_hands_free_capture_duck.assert_called_once_with(456)
