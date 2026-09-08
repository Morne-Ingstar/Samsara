"""Transport tests for the out-of-process ducking host.

These drive a REAL subprocess (tests/_fake_ducking_child.py) over real
pipes -- the point of the tier-2 ruling is that a wedged COM call must be
killable, and you cannot prove killability against an in-process mock. The
fake child speaks ducking_host's protocol but touches no COM, so hang and
death are deterministic instead of hardware-dependent.

    python -m pytest tests/test_ducking_transport.py -v
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from samsara import audio_ducking as ad

FAKE_CHILD = Path(__file__).parent / "_fake_ducking_child.py"
# Short deadline: these tests assert on timing, and the production 2.0s
# default would make the suite needlessly slow.
TEST_DEADLINE = 0.5


def _transport(mode: str) -> ad.DuckingHostTransport:
    def spawn():
        return subprocess.Popen(
            [sys.executable, "-u", str(FAKE_CHILD), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    return ad.DuckingHostTransport(spawn_fn=spawn)


@pytest.fixture
def recorded(monkeypatch):
    """Capture flight-recorder events -- the soak-time evidence trail is
    part of this feature's contract, not incidental logging."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ad.flight_recorder,
        "record",
        lambda event, **fields: events.append((event, fields)),
    )
    return events


class TestHappyPath:
    def test_round_trip_carries_elapsed_ms(self):
        transport = _transport("normal")
        try:
            reply = transport.request("ping", deadline=TEST_DEADLINE)
            assert reply["ok"] is True
            assert "elapsed_ms" in reply
        finally:
            transport.shutdown()

    def test_list_get_set_round_trip(self):
        transport = _transport("normal")
        try:
            listed = transport.request("list_sessions", deadline=TEST_DEADLINE)
            assert listed["ok"] is True
            sid = listed["sessions"][0]["sid"]

            assert transport.request("get_volume", sid=sid,
                                     deadline=TEST_DEADLINE)["level"] == 0.5
            written = transport.request("set_volume", sid=sid, level=0.1,
                                        deadline=TEST_DEADLINE)
            assert written["ok"] is True
            assert written["prev_level"] == 0.5
            assert transport.request("get_volume", sid=sid,
                                     deadline=TEST_DEADLINE)["level"] == 0.1
        finally:
            transport.shutdown()

    def test_child_is_not_spawned_until_first_command(self):
        """`ducking.enabled=false` means nothing ever issues a duck op, so
        this laziness is what guarantees the child never exists at all."""
        transport = _transport("normal")
        try:
            assert transport._proc is None
            assert transport.generation == 0
            transport.request("ping", deadline=TEST_DEADLINE)
            assert transport._proc is not None
            assert transport.generation == 1
        finally:
            transport.shutdown()


class TestHangMidOp:
    def test_hang_returns_failure_within_deadline_and_kills_child(self, recorded):
        transport = _transport("hang")
        try:
            # First command answers normally: the child is healthy here.
            assert transport.request("ping", deadline=TEST_DEADLINE)["ok"] is True
            proc = transport._proc
            assert proc.poll() is None

            started = time.monotonic()
            reply = transport.request("get_volume", sid="fake-1",
                                      deadline=TEST_DEADLINE)
            elapsed = time.monotonic() - started

            assert reply["ok"] is False
            assert "timeout" in reply["err"]
            # Bounded: the caller is released at the deadline, not held by
            # the wedged child.
            assert elapsed < TEST_DEADLINE * 3

            # The wedged child is actually dead, not merely abandoned --
            # this is the whole difference from an in-process timeout.
            proc.wait(timeout=5)
            assert proc.poll() is not None

            restarts = [f for name, f in recorded if name == "ducker_child_restart"]
            assert restarts, f"expected ducker_child_restart, got {recorded}"
            assert restarts[-1]["pending_op"] == "get_volume"
            assert restarts[-1]["reason"] == "deadline"
        finally:
            transport.shutdown()

    def test_respawns_lazily_on_next_op_with_new_generation(self, recorded):
        transport = _transport("hang")
        try:
            transport.request("ping", deadline=TEST_DEADLINE)
            first_generation = transport.generation

            transport.request("get_volume", sid="fake-1", deadline=TEST_DEADLINE)
            assert transport._proc is None  # killed, not respawned yet

            # Next op transparently brings up a fresh child.
            assert transport.request("ping", deadline=TEST_DEADLINE)["ok"] is True
            assert transport.generation > first_generation
        finally:
            transport.shutdown()

    def test_second_caller_is_not_made_to_wait_a_second_full_deadline(self):
        """A hung op holds the transport; a concurrent caller must fail
        fast rather than stack another deadline on top."""
        import threading

        transport = _transport("hang")
        try:
            transport.request("ping", deadline=TEST_DEADLINE)

            def hang():
                transport.request("get_volume", sid="fake-1", deadline=TEST_DEADLINE)

            hanger = threading.Thread(target=hang)
            hanger.start()
            time.sleep(0.05)  # let it get inside the hanging op

            started = time.monotonic()
            reply = transport.request("ping", deadline=TEST_DEADLINE)
            elapsed = time.monotonic() - started

            assert reply["ok"] is False
            assert elapsed < TEST_DEADLINE * 2
            hanger.join(timeout=5)
        finally:
            transport.shutdown()


class TestDeathMidOp:
    def test_death_mid_op_returns_failure_and_records_restart(self, recorded):
        transport = _transport("die")
        try:
            assert transport.request("ping", deadline=TEST_DEADLINE)["ok"] is True

            reply = transport.request("get_volume", sid="fake-1",
                                      deadline=TEST_DEADLINE)

            assert reply["ok"] is False
            assert "died" in reply["err"]

            restarts = [f for name, f in recorded if name == "ducker_child_restart"]
            assert restarts, f"expected ducker_child_restart, got {recorded}"
            assert restarts[-1]["pending_op"] == "get_volume"
            assert restarts[-1]["reason"] == "pipe closed"
        finally:
            transport.shutdown()

    def test_recovers_after_death(self):
        transport = _transport("die")
        try:
            transport.request("ping", deadline=TEST_DEADLINE)
            transport.request("get_volume", sid="fake-1", deadline=TEST_DEADLINE)
            assert transport.request("ping", deadline=TEST_DEADLINE)["ok"] is True
        finally:
            transport.shutdown()

    def test_spawn_failure_is_reported_not_raised(self, recorded):
        def boom():
            raise OSError("cannot spawn")

        transport = ad.DuckingHostTransport(spawn_fn=boom)
        reply = transport.request("ping", deadline=TEST_DEADLINE)
        assert reply["ok"] is False
        assert "spawn failed" in reply["err"]
        assert any(name == "ducker_child_restart" for name, _ in recorded)


class TestStaleSessionIds:
    def test_handle_from_a_dead_child_refuses_to_act(self, monkeypatch):
        """After a restart the old sids address nothing. A handle minted by
        the dead child must fail rather than silently hit whatever session
        the replacement happened to assign that id to."""
        transport = _transport("normal")
        monkeypatch.setattr(ad, "_get_transport", lambda: transport)
        try:
            sessions = ad._iter_audio_sessions()
            assert sessions and sessions[0].get_master_volume() == 0.5

            transport._restart_locked("test", reason="forced")
            transport.request("ping", deadline=TEST_DEADLINE)  # new generation

            with pytest.raises(OSError, match="stale"):
                sessions[0].get_master_volume()
            with pytest.raises(OSError, match="stale"):
                sessions[0].set_master_volume(0.2)
        finally:
            transport.shutdown()

    def test_failed_enumeration_surfaces_as_oserror(self, monkeypatch):
        transport = ad.DuckingHostTransport(spawn_fn=lambda: (_ for _ in ()).throw(
            OSError("no child")))
        monkeypatch.setattr(ad, "_get_transport", lambda: transport)
        with pytest.raises(OSError):
            ad._iter_audio_sessions()


class TestShutdown:
    def test_shutdown_terminates_the_child(self):
        transport = _transport("normal")
        transport.request("ping", deadline=TEST_DEADLINE)
        proc = transport._proc

        transport.shutdown()

        proc.wait(timeout=5)
        assert proc.poll() is not None
        assert transport._proc is None

    def test_shutdown_does_not_wait_on_a_wedged_child(self):
        """App exit must never hang on audio: a child that answers nothing
        is killed rather than waited on."""
        transport = _transport("hang_startup")
        try:
            transport.request("ping", deadline=TEST_DEADLINE)  # times out, kills
            transport.request("ping", deadline=TEST_DEADLINE)  # respawn, times out
            proc = transport._proc

            started = time.monotonic()
            transport.shutdown()
            elapsed = time.monotonic() - started

            assert elapsed < ad._CHILD_SHUTDOWN_DEADLINE_S + 5
            if proc is not None:
                proc.wait(timeout=5)
                assert proc.poll() is not None
        finally:
            transport.shutdown()

    def test_shutdown_is_idempotent(self):
        transport = _transport("normal")
        transport.request("ping", deadline=TEST_DEADLINE)
        transport.shutdown()
        transport.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Core Audio is Windows-only")
def test_real_child_round_trip_smoke():
    """One end-to-end pass against the REAL ducking_host and real Core Audio.

    Skips when no audio endpoint is reachable (CI containers, headless
    boxes). Deliberately writes each session's CURRENT level back to
    itself: a genuine SetMasterVolume round trip that cannot disturb
    whatever the user is listening to while the suite runs.
    """
    transport = ad.DuckingHostTransport()
    try:
        ping = transport.request("ping")
        if not ping.get("ok"):
            pytest.skip(f"ducking host unavailable: {ping.get('err')}")
        assert "elapsed_ms" in ping

        listed = transport.request("list_sessions")
        if not listed.get("ok"):
            pytest.skip(f"no audio endpoint: {listed.get('err')}")
        assert "elapsed_ms" in listed

        sessions = listed["sessions"]
        if not sessions:
            pytest.skip("no active audio sessions to exercise")

        sid = sessions[0]["sid"]
        current = transport.request("get_volume", sid=sid)
        assert current["ok"] is True
        assert "elapsed_ms" in current

        # Same-value write: exercises SetMasterVolume without changing what
        # the user hears.
        written = transport.request("set_volume", sid=sid, level=current["level"])
        assert written["ok"] is True
        assert written["prev_level"] == pytest.approx(current["level"], abs=1e-3)

        after = transport.request("get_volume", sid=sid)
        assert after["level"] == pytest.approx(current["level"], abs=1e-3)

        # sids are stable across enumerations, which is what lets the
        # parent's sweep keep tracking sessions it ducked earlier.
        again = transport.request("list_sessions")
        assert sid in [entry["sid"] for entry in again["sessions"]]
    finally:
        transport.shutdown()
