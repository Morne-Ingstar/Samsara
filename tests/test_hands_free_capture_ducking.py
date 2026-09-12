"""Tests for hands-free capture-window ducking (2026-07-24 amendment):
DictationApp's idle-duck (wake-word toggle) and capture-duck (per
utterance) layers, wired via a shared mocked SessionDucker so construction
args (duck_level, exclude_pids) and start()/stop() call sequencing can be
asserted without touching real WASAPI.

DESIGN INTENT under test: ducking anchors to ACTIVE CAPTURE and the
wake-word TOGGLE, never to any session-end/exit event -- this file
explicitly spies on every exit API (exit_ava_command_session,
exit_command_mode, stop_wake_word_mode, _end_wake_session, _reset_wake_
dictation) and asserts NONE of them are ever called by the new ducking
methods.

Exercises the REAL bound DictationApp methods via types.MethodType
against a minimal duck-typed `self`, matching this repo's established
pattern (see tests/test_ava_command_session_ghost_tap.py).
"""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


class _FakeTimer:
    """Records construction args; only fires when the test calls .fire()
    explicitly -- no real wall-clock delay."""
    instances: list["_FakeTimer"] = []

    def __init__(self, name, delay, fn, args=(), kwargs=None, **_):
        self.name = name
        self.delay = delay
        self.fn = fn
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args, **self.kwargs)


def _blocking_ducker_factory(started: threading.Event, release: threading.Event):
    """Return a SessionDucker replacement that blocks in start() until released."""

    class BlockingDucker:
        instances: list["BlockingDucker"] = []

        def __init__(self, *_, **__):
            self._tracked_by_id = {}
            self.stop_calls = 0
            self.start_calls = 0
            BlockingDucker.instances.append(self)

        @classmethod
        def reset(cls) -> None:
            cls.instances = []

        def start(self) -> None:
            self.start_calls += 1
            started.set()
            release.wait()

        def stop(self) -> None:
            self.stop_calls += 1

    return BlockingDucker


@pytest.fixture(autouse=True)
def _fake_timer(monkeypatch):
    _FakeTimer.instances = []
    monkeypatch.setattr(dictation.thread_registry, "timer", _FakeTimer)
    yield
    _FakeTimer.instances = []


_EXIT_API_NAMES = (
    "exit_ava_command_session",
    "exit_command_mode",
    "stop_wake_word_mode",
    "_end_wake_session",
    "_reset_wake_dictation",
)

_BOUND_METHODS = (
    "_hands_free_duck_excludes",
    "_bump_wake_gate_freeze",
    "_wake_gate_frozen",
    "_start_hands_free_idle_duck",
    "_stop_hands_free_idle_duck",
    "_open_hands_free_capture_duck",
    "_close_hands_free_capture_duck",
    "_restore_hands_free_capture_duck_now",
)


def _make_app(monkeypatch, *, ducking_cfg=None, mock_ducker_cls=None):
    app = types.SimpleNamespace()
    app.config = {"ducking": dict(ducking_cfg or {
        "hands_free_enabled": True,
        "hands_free_level": 0.15,
        "hands_free_idle_level": 0.8,
    })}
    app._hands_free_idle_ducker = None
    app._hands_free_capture_ducker = None
    app._hands_free_duck_lock = threading.Lock()
    app._hands_free_duck_restore_timer = None
    app._hands_free_capture_duck_restore_generation = 0
    app._hands_free_capture_duck_restore_token = object()
    app._hands_free_capture_duck_owners = set()
    app._hands_free_capture_duck_owner_seq = 0
    app._hands_free_capture_duck_start_generation = 0
    app._hands_free_capture_duck_starting = False
    app._wake_gate_freeze_until = 0.0
    app.audio_coordinator = None
    app._log_duck_result = dictation.DictationApp._log_duck_result

    for name in _EXIT_API_NAMES:
        setattr(app, name, Mock(name=name))

    for name in _BOUND_METHODS:
        setattr(app, name, types.MethodType(getattr(dictation.DictationApp, name), app))

    if mock_ducker_cls is not None:
        monkeypatch.setattr(dictation.audio_ducking, "SessionDucker", mock_ducker_cls)

    return app


def _assert_no_exit_apis_called(app):
    for name in _EXIT_API_NAMES:
        getattr(app, name).assert_not_called()


class TestIdleDuckEngagesWithToggle:
    def test_constructs_with_idle_level_and_excludes(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {"a": object(), "b": object()}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        app._start_hands_free_idle_duck()

        mock_cls.assert_called_once_with(duck_level=0.8, exclude_pids=set())
        instance.start.assert_called_once()
        assert app._hands_free_idle_ducker is instance
        _assert_no_exit_apis_called(app)

    def test_stop_restores_idle_ducker(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        app._start_hands_free_idle_duck()

        app._stop_hands_free_idle_duck()

        instance.stop.assert_called_once()
        assert app._hands_free_idle_ducker is None
        _assert_no_exit_apis_called(app)

    def test_disabled_flag_never_constructs(self, monkeypatch):
        mock_cls = MagicMock()
        app = _make_app(
            monkeypatch,
            ducking_cfg={"hands_free_enabled": False, "hands_free_level": 0.15,
                         "hands_free_idle_level": 0.8},
            mock_ducker_cls=mock_cls,
        )
        app._start_hands_free_idle_duck()
        mock_cls.assert_not_called()

    def test_idle_level_1_0_never_constructs(self, monkeypatch):
        """1.0 == no idle duck, by design -- not a failure."""
        mock_cls = MagicMock()
        app = _make_app(
            monkeypatch,
            ducking_cfg={"hands_free_enabled": True, "hands_free_level": 0.15,
                         "hands_free_idle_level": 1.0},
            mock_ducker_cls=mock_cls,
        )
        app._start_hands_free_idle_duck()
        mock_cls.assert_not_called()

    def test_second_start_is_a_noop(self, monkeypatch):
        mock_cls = MagicMock()
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        app._start_hands_free_idle_duck()
        app._start_hands_free_idle_duck()
        assert mock_cls.call_count == 1

    def test_stop_also_tears_down_active_capture_duck(self, monkeypatch):
        """Belt-and-suspenders: wake-word toggle off must never leave
        anything ducked, even mid capture-window."""
        mock_cls = MagicMock()
        idle_instance = MagicMock(_tracked_by_id={})
        capture_instance = MagicMock(_tracked_by_id={})
        mock_cls.side_effect = [idle_instance, capture_instance]
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        app._start_hands_free_idle_duck()
        app._open_hands_free_capture_duck()
        assert app._hands_free_capture_ducker is capture_instance

        app._stop_hands_free_idle_duck()

        capture_instance.stop.assert_called_once()
        idle_instance.stop.assert_called_once()
        assert app._hands_free_capture_ducker is None
        assert app._hands_free_idle_ducker is None
        _assert_no_exit_apis_called(app)


class TestCaptureDuckOpenClose:
    def test_open_constructs_with_capture_level_and_excludes(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {"a": object()}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        owner = app._open_hands_free_capture_duck()

        assert owner == 1
        mock_cls.assert_called_once_with(duck_level=0.15, exclude_pids=set())
        instance.start.assert_called_once()
        _assert_no_exit_apis_called(app)

    def test_open_returns_the_same_owner_when_explicit(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {"a": object()}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        owner_a = app._open_hands_free_capture_duck(owner_token=123)
        owner_b = app._open_hands_free_capture_duck(owner_token=123)

        assert owner_a == 123
        assert owner_b == 123
        assert mock_cls.call_count == 1

    def test_close_schedules_delayed_restore_not_immediate(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        owner_a = app._open_hands_free_capture_duck()
        owner_b = app._open_hands_free_capture_duck()

        app._close_hands_free_capture_duck(owner_a)
        assert app._hands_free_capture_duck_owners == {owner_b}

        instance.stop.assert_not_called()  # not restored yet -- debounced
        assert len(_FakeTimer.instances) == 0

    def test_overlapping_ownership_only_restores_after_last_window_closes(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        owner_a = app._open_hands_free_capture_duck()
        owner_b = app._open_hands_free_capture_duck()

        app._close_hands_free_capture_duck(owner_a)
        assert len(_FakeTimer.instances) == 0
        assert app._hands_free_capture_duck_owners == {owner_b}

        app._close_hands_free_capture_duck(owner_b)
        assert len(_FakeTimer.instances) == 1
        _FakeTimer.instances[0].fire()

        instance.stop.assert_called_once()
        assert app._hands_free_capture_duck_owners == set()

    def test_restore_fires_stop_after_timer(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        owner = app._open_hands_free_capture_duck()
        # 2026-07-24 amendment (docs/reviews/hands_free_path_review.md,
        # "Medium/low -- duck ownership"): close with no owner_token is now
        # an intentional no-op, not a forwarded close -- pass the real
        # owner this open returned.
        app._close_hands_free_capture_duck(owner)

        _FakeTimer.instances[0].fire()

        instance.stop.assert_called_once()
        assert app._hands_free_capture_ducker is None
        _assert_no_exit_apis_called(app)

    def test_restore_on_discard_same_as_transcription_complete(self, monkeypatch):
        """Buffer-discarded and transcription-complete both call close --
        no distinction at this layer."""
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        owner = app._open_hands_free_capture_duck()

        app._close_hands_free_capture_duck(owner)  # simulates either close reason

        _FakeTimer.instances[0].fire()
        instance.stop.assert_called_once()

    def test_rapid_reopen_cancels_pending_restore_and_reuses_duck(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        owner = app._open_hands_free_capture_duck()
        app._close_hands_free_capture_duck(owner)
        pending = _FakeTimer.instances[0]

        app._open_hands_free_capture_duck()  # a new utterance starts before the tail fires

        assert pending.cancelled is True
        assert mock_cls.call_count == 1, "must reuse the still-active duck, not construct a second one"
        instance.stop.assert_not_called()

    def test_disabled_flag_never_constructs(self, monkeypatch):
        mock_cls = MagicMock()
        app = _make_app(
            monkeypatch,
            ducking_cfg={"hands_free_enabled": False, "hands_free_level": 0.15,
                         "hands_free_idle_level": 0.8},
            mock_ducker_cls=mock_cls,
        )
        app._open_hands_free_capture_duck()
        mock_cls.assert_not_called()

    def test_close_without_open_is_a_noop(self, monkeypatch):
        mock_cls = MagicMock()
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        app._close_hands_free_capture_duck()  # never opened
        assert _FakeTimer.instances == []

    def test_blocked_capture_start_is_invalidated_by_toggle_off(self, monkeypatch):
        start_started = threading.Event()
        start_release = threading.Event()
        mock_cls = _blocking_ducker_factory(start_started, start_release)
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        owner = {}
        open_errors = []

        def _open():
            try:
                owner["token"] = app._open_hands_free_capture_duck()
            except BaseException as exc:
                open_errors.append(exc)

        open_thread = threading.Thread(target=_open)
        open_thread.start()

        assert start_started.wait(timeout=1.0)
        app._stop_hands_free_idle_duck()
        # Let the blocked start complete after shutdown invalidation.
        start_release.set()
        open_thread.join(timeout=1.0)

        assert not open_thread.is_alive()
        assert open_errors == [], open_errors
        assert app._hands_free_capture_ducker is None
        # Invalidated (start_generation moved under it while blocked in
        # ducker.start()) -- _open_hands_free_capture_duck's own
        # "lost ownership" path returns None, not the provisional token;
        # this is the case this test's name describes.
        assert owner["token"] is None
        assert mock_cls.instances
        assert mock_cls.instances[0].stop_calls == 1

    def test_owner_a_releases_while_starting_and_owner_b_keeps_ducker(self, monkeypatch):
        start_started = threading.Event()
        start_release = threading.Event()
        mock_cls = _blocking_ducker_factory(start_started, start_release)
        mock_cls.reset()
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        owner_a: dict[str, int] = {}
        owner_b: dict[str, int] = {}
        owner_a_token = 111
        open_errors = []

        def _open_a():
            try:
                owner_a["token"] = app._open_hands_free_capture_duck(
                    owner_token=owner_a_token
                )
            except BaseException as exc:
                open_errors.append(exc)

        open_thread = threading.Thread(target=_open_a)
        open_thread.start()

        try:
            assert start_started.wait(timeout=1.0)
            owner_b_token = app._open_hands_free_capture_duck(owner_token=234)
            owner_b["token"] = owner_b_token

            app._close_hands_free_capture_duck(owner_token=owner_a_token)
            assert app._hands_free_capture_duck_owners == {owner_b["token"]}

            start_release.set()
            open_thread.join(timeout=1.0)
            assert not open_thread.is_alive()
            assert open_errors == [], open_errors

            assert app._hands_free_capture_ducker is mock_cls.instances[0]
            assert mock_cls.instances[0].stop_calls == 0

            app._close_hands_free_capture_duck(owner_b["token"])
            _FakeTimer.instances[0].fire()
            assert mock_cls.instances[0].stop_calls == 1
        finally:
            start_release.set()
            open_thread.join(timeout=1.0)
            if open_thread.is_alive():
                raise AssertionError("capture-duck opener thread did not stop")


class TestNoSessionEndSemanticsIntroduced:
    """Explicit spy-based assertion per the task's own test requirement:
    no ducking code path calls any session-end/exit function."""

    def test_full_open_close_cycle_never_touches_exit_apis(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)

        app._start_hands_free_idle_duck()
        owner = app._open_hands_free_capture_duck()
        app._close_hands_free_capture_duck(owner)
        _FakeTimer.instances[0].fire()
        app._stop_hands_free_idle_duck()

        _assert_no_exit_apis_called(app)


class TestWakeGateFreeze:
    def test_not_frozen_by_default(self, monkeypatch):
        app = _make_app(monkeypatch)
        assert app._wake_gate_frozen() is False

    def test_frozen_immediately_after_a_duck_transition(self, monkeypatch):
        app = _make_app(monkeypatch)
        app._bump_wake_gate_freeze()
        assert app._wake_gate_frozen() is True

    def test_unfrozen_after_settle_window_elapses(self, monkeypatch):
        app = _make_app(monkeypatch)
        clock = {"t": 1000.0}
        monkeypatch.setattr(dictation.time, "monotonic", lambda: clock["t"])
        app._bump_wake_gate_freeze()
        assert app._wake_gate_frozen() is True
        clock["t"] += dictation._WAKE_GATE_FREEZE_SETTLE_S + 0.1
        assert app._wake_gate_frozen() is False

    def test_frozen_continuously_while_tts_speaking_regardless_of_settle_window(self, monkeypatch):
        app = _make_app(monkeypatch)
        coordinator = Mock()
        coordinator.is_speaking = True
        app.audio_coordinator = coordinator
        clock = {"t": 1000.0}
        monkeypatch.setattr(dictation.time, "monotonic", lambda: clock["t"])
        # No duck transition at all -- TTS alone must freeze.
        assert app._wake_gate_frozen() is True
        clock["t"] += 10 * dictation._WAKE_GATE_FREEZE_SETTLE_S
        assert app._wake_gate_frozen() is True  # still speaking -- still frozen

    def test_opening_a_capture_duck_bumps_the_freeze(self, monkeypatch):
        mock_cls = MagicMock()
        instance = mock_cls.return_value
        instance._tracked_by_id = {}
        app = _make_app(monkeypatch, mock_ducker_cls=mock_cls)
        assert app._wake_gate_frozen() is False

        app._open_hands_free_capture_duck()

        assert app._wake_gate_frozen() is True

    def test_never_raises_on_broken_coordinator(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.audio_coordinator = object()  # no .is_speaking attribute
        assert app._wake_gate_frozen() is False
