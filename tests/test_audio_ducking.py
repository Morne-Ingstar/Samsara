"""Tests for samsara.audio_ducking's duck/restore state machine, with the
entire Core Audio COM layer mocked -- no audio hardware, no live COM calls,
no real ctypes vtable dispatch. Every test patches _ensure_com_init,
_get_session_enumerator, _iter_sessions, _get_session_volume,
_set_session_volume, and _release at the module level, so only the pure
Python duck()/restore() state-machine logic is under test.

Live COM-layer verification (real GUIDs/vtable indices against a running
Windows Core Audio session set) was done manually during development, not
here -- see samsara/audio_ducking.py's module docstring.
"""
from unittest.mock import patch

import pytest

from samsara import audio_ducking as ad


class FakeSession:
    """Stand-in for a (instance_id, pid, is_system_sounds, simple_volume)
    tuple's simple_volume slot -- get/set operate on .level directly, no
    ctypes involved."""

    def __init__(self, instance_id, pid, level, is_system_sounds=False):
        self.instance_id = instance_id
        self.pid = pid
        self.is_system_sounds = is_system_sounds
        self.level = level
        self.released = False

    def as_tuple(self):
        return (self.instance_id, self.pid, self.is_system_sounds, self)


@pytest.fixture(autouse=True)
def reset_module_state():
    """audio_ducking keeps module-level mutable state (_ducked,
    _saved_volumes) -- reset before and after every test so tests can't
    leak state into each other regardless of pass/fail."""
    ad._ducked = False
    ad._saved_volumes = {}
    yield
    ad._ducked = False
    ad._saved_volumes = {}


@pytest.fixture
def mocked_com(monkeypatch):
    """Patches the entire COM boundary. `sessions` is a mutable list of
    FakeSession the test can populate/mutate between duck() and restore()
    calls (e.g. to simulate a session vanishing)."""
    sessions = []

    def fake_get_volume(iface):
        return iface.level

    def fake_set_volume(iface, level):
        iface.level = level
        return True

    def fake_release(iface):
        if hasattr(iface, 'released'):
            iface.released = True

    monkeypatch.setattr(ad, '_ensure_com_init', lambda: True)
    monkeypatch.setattr(ad, '_get_session_enumerator', lambda: object())
    monkeypatch.setattr(ad, '_iter_sessions', lambda _enum: iter([s.as_tuple() for s in sessions]))
    monkeypatch.setattr(ad, '_get_session_volume', fake_get_volume)
    monkeypatch.setattr(ad, '_set_session_volume', fake_set_volume)
    monkeypatch.setattr(ad, '_release', fake_release)
    return sessions


class TestDuck:
    def test_ducks_non_self_non_system_sessions(self, mocked_com):
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)

        assert other.level == pytest.approx(0.2)
        assert ad.is_ducked() is True

    def test_skips_own_process_session(self, mocked_com):
        own = FakeSession('inst-self', pid=1234, level=0.9)
        other = FakeSession('inst-other', pid=999, level=0.8)
        mocked_com.extend([own, other])

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.1)

        assert own.level == pytest.approx(0.9)  # untouched
        assert other.level == pytest.approx(0.1)  # ducked

    def test_skips_system_sounds_session(self, mocked_com):
        system = FakeSession('inst-sys', pid=999, level=1.0, is_system_sounds=True)
        other = FakeSession('inst-other', pid=888, level=1.0)
        mocked_com.extend([system, other])

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.1)

        assert system.level == pytest.approx(1.0)  # untouched
        assert other.level == pytest.approx(0.1)

    def test_records_pre_duck_volume(self, mocked_com):
        other = FakeSession('inst-a', pid=999, level=0.73)
        mocked_com.append(other)

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)

        assert ad._saved_volumes['inst-a'] == pytest.approx(0.73)

    def test_is_idempotent_when_already_ducked(self, mocked_com):
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)
            other.level = 0.2  # simulate the duck having taken effect
            ad.duck(0.05)  # second call must be a no-op

        # Saved volume is still the ORIGINAL 0.8, not re-captured at 0.2 --
        # proves the second duck() call did nothing.
        assert ad._saved_volumes['inst-a'] == pytest.approx(0.8)

    def test_no_sessions_playing_is_safe(self, mocked_com):
        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)
        assert ad.is_ducked() is True
        assert ad._saved_volumes == {}

    def test_com_init_failure_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(ad, '_ensure_com_init', lambda: False)
        ad.duck(0.2)
        assert ad.is_ducked() is False

    def test_enumerator_failure_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(ad, '_ensure_com_init', lambda: True)
        monkeypatch.setattr(ad, '_get_session_enumerator', lambda: None)
        ad.duck(0.2)
        assert ad.is_ducked() is False

    def test_never_raises_on_unexpected_exception(self, monkeypatch):
        monkeypatch.setattr(ad, '_ensure_com_init', lambda: True)

        def boom():
            raise OSError("simulated COM failure")

        monkeypatch.setattr(ad, '_get_session_enumerator', boom)
        ad.duck(0.2)  # must not raise
        assert ad.is_ducked() is False


class TestRestore:
    def test_restores_saved_volume(self, mocked_com):
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)
        assert other.level == pytest.approx(0.2)

        ad.restore()
        assert other.level == pytest.approx(0.8)

    def test_clears_ducked_state(self, mocked_com):
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)
        ad.restore()

        assert ad.is_ducked() is False
        assert ad._saved_volumes == {}

    def test_is_a_no_op_when_not_ducked(self, mocked_com):
        # No duck() call first -- restore() must do nothing and not raise,
        # even with sessions present that it could (wrongly) touch.
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)

        ad.restore()

        assert other.level == pytest.approx(0.8)
        assert ad.is_ducked() is False

    def test_vanished_session_is_skipped_silently(self, mocked_com):
        """The session ducked in duck() is no longer present when
        restore() re-enumerates -- e.g. the owning app exited mid-hold.
        Must not raise, and ducked state must still clear cleanly."""
        vanishing = FakeSession('inst-vanish', pid=999, level=0.8)
        staying = FakeSession('inst-stay', pid=888, level=0.6)
        mocked_com.extend([vanishing, staying])

        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)

        # Simulate the vanishing session's process having exited: it's no
        # longer in the live enumeration restore() will see.
        mocked_com.remove(vanishing)

        ad.restore()

        assert staying.level == pytest.approx(0.6)  # still correctly restored
        assert ad.is_ducked() is False
        assert ad._saved_volumes == {}

    def test_session_started_mid_duck_is_left_alone(self, mocked_com):
        """A session with no entry in _saved_volumes (wasn't present at
        duck() time) must not be touched by restore() -- out of scope for
        v1, per the module's own docstring."""
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)
        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)

        new_session = FakeSession('inst-new', pid=777, level=0.55)
        mocked_com.append(new_session)

        ad.restore()

        assert new_session.level == pytest.approx(0.55)  # untouched

    def test_never_raises_on_unexpected_exception(self, mocked_com, monkeypatch):
        other = FakeSession('inst-a', pid=999, level=0.8)
        mocked_com.append(other)
        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)

        def boom(_enum):
            raise OSError("simulated COM failure")

        monkeypatch.setattr(ad, '_iter_sessions', boom)
        ad.restore()  # must not raise

        # Ducked state still clears even though the COM call blew up --
        # never leaves the module stuck thinking it's ducked when it
        # already tried and gave up.
        assert ad.is_ducked() is False


class TestFadeAll:
    def test_steps_from_source_to_target_over_fixed_steps(self):
        iface = FakeSession('x', pid=1, level=0.9)
        seen_levels = []
        with patch.object(ad, '_set_session_volume',
                           side_effect=lambda i, lvl: seen_levels.append(lvl) or True), \
                patch.object(ad, 'time') as mock_time:
            ad._fade_all([(iface, 0.9, 0.2)])
        assert len(seen_levels) == ad._FADE_STEPS
        assert seen_levels[0] > seen_levels[-1]  # ramping down
        assert seen_levels[-1] == pytest.approx(0.2)  # final step lands exactly on target
        assert mock_time.sleep.call_count == ad._FADE_STEPS - 1

    def test_empty_targets_does_nothing(self):
        with patch.object(ad, '_set_session_volume') as mock_set:
            ad._fade_all([])
        mock_set.assert_not_called()


class TestIsDucked:
    def test_reflects_state(self, mocked_com):
        assert ad.is_ducked() is False
        with patch.object(ad.os, 'getpid', return_value=1234):
            ad.duck(0.2)
        assert ad.is_ducked() is True
        ad.restore()
        assert ad.is_ducked() is False
