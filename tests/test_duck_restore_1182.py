"""User mixer choices and hold boundaries; fake sessions only."""
import json
import threading

import pytest

from samsara import audio_ducking as ad
from tests.test_audio_ducking import (
    FakeSession, NamedSession, _hard_kill_and_restart,
    journal, no_threads, use_sessions,
)


@pytest.mark.parametrize("release_old_first", [False, True])
def test_user_raises_twenty_to_hundred_before_next_hold(journal, release_old_first):
    app = NamedSession("brave", 333, 0.2, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    old, new = ad.SessionDucker(0.8), ad.SessionDucker(0.2)
    old.start()
    app.volume = 1.0
    new.start()
    assert app.volume == pytest.approx(0.2)
    entry, = json.loads(journal.read_text())["ducked"]
    assert entry["original"] == 1.0
    first, second = (old, new) if release_old_first else (new, old)
    first.stop()
    second.stop()
    assert app.volume == 1.0
    assert json.loads(journal.read_text())["ducked"] == []


@pytest.mark.parametrize("user_level", [0.0, 0.01, 0.15, 0.2, 0.8])
def test_deliberate_volume_before_duck_wins_even_at_known_duck_target(journal, user_level):
    app = NamedSession("brave", 333, 1.0, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    duck = ad.SessionDucker(0.2)
    duck.start()
    duck.stop()
    app.volume = user_level
    duck.start()
    assert app.volume == pytest.approx(user_level * 0.2)
    duck.stop()
    assert app.volume == pytest.approx(user_level)


@pytest.mark.parametrize("release_old_first", [False, True])
def test_manual_change_during_nested_duck_survives_all_releases(journal, release_old_first):
    app = NamedSession("brave", 333, 1.0, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    old, new = ad.SessionDucker(0.2), ad.SessionDucker(0.2)
    old.start()
    new.start()
    # This equals the old remaining-lease target. Previously the second
    # release mistook it for its own write and restored the stale 100.
    app.volume = 0.2
    first, second = (old, new) if release_old_first else (new, old)
    first.stop()
    assert app.volume == 0.2
    second.stop()
    assert app.volume == 0.2


@pytest.mark.parametrize("pid", [333, 444])
def test_new_brave_tab_mid_duck_is_never_enrolled(pid):
    first = NamedSession("old", 333, 1.0, "brave.exe", "tab-a")
    later = NamedSession("new", pid, 0.2, "brave.exe", "tab-b")
    sessions = [first]
    use_sessions(sessions)
    duck = ad.SessionDucker(0.2)
    duck.start()
    sessions.append(later)
    for _ in range(3):
        duck._run_sweep()
    duck.stop()
    assert later.volume == 0.2
    assert later.volume_calls == []
    assert first.volume == 1.0


@pytest.mark.parametrize("reuse_ducker", [False, True])
def test_next_hold_waits_for_pending_restore(monkeypatch, reuse_ducker):
    # Bypass the fake RPC's per-command lock to pause exactly between the
    # old release's ownership update and its volume read, as real RPCs can.
    app = FakeSession("brave", 333, 1.0)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [app])
    old = ad.SessionDucker(0.2)
    new = old if reuse_ducker else ad.SessionDucker(0.2)
    old.start()
    entered, proceed = threading.Event(), threading.Event()
    starting, finished = threading.Event(), threading.Event()
    errors = []
    get_volume = app.get_master_volume

    def gated_read():
        if threading.current_thread().name == "old-release":
            entered.set()
            if not proceed.wait(2):
                raise TimeoutError("test did not release old restore")
        return get_volume()

    def release():
        try:
            old.stop()
        except Exception as exc:
            errors.append(exc)

    def start():
        starting.set()
        try:
            new.start()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(app, "get_master_volume", gated_read)
    release_thread = threading.Thread(target=release, name="old-release")
    next_thread = threading.Thread(target=start)
    release_thread.start()
    try:
        assert entered.wait(1)
        next_thread.start()
        assert starting.wait(1)
        assert not finished.wait(0.05)
    finally:
        proceed.set()
        release_thread.join(2)
        if next_thread.ident is not None:
            next_thread.join(2)
    assert not release_thread.is_alive()
    assert not next_thread.is_alive()
    assert not errors
    assert app.volume == pytest.approx(0.2)
    new.stop()
    assert app.volume == 1.0
    assert app.volume_calls == pytest.approx([0.2, 1.0, 0.2, 1.0])


def test_rebased_user_level_survives_crash(journal):
    app = NamedSession("brave", 333, 0.2, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ad.SessionDucker(0.8).start()
    app.volume = 1.0
    ad.SessionDucker(0.2).start()
    app.session_id = "new-process-sid"
    _hard_kill_and_restart(journal, [app])
    assert ad.recover_leftover_ducks() == 1
    assert app.volume == 1.0


@pytest.mark.parametrize("user_level", [0.0, 0.01])
def test_verified_low_user_level_survives_crash(journal, user_level):
    app = NamedSession("brave", 333, 1.0, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    duck = ad.SessionDucker(0.2)
    duck.start()
    duck.stop()
    app.volume = user_level
    duck.start()
    app.session_id = "new-process-sid"
    _hard_kill_and_restart(journal, [app])
    assert ad.recover_leftover_ducks() == 1
    assert app.volume == user_level


def test_unreadable_existing_session_is_not_reducked(journal, monkeypatch):
    app = NamedSession("brave", 333, 1.0, "brave.exe", "tab-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    old, new = ad.SessionDucker(0.8), ad.SessionDucker(0.2)
    old.start()
    writes = list(app.volume_calls)
    with monkeypatch.context() as patch:
        def unreadable():
            raise OSError("fake session unavailable")
        patch.setattr(app, "get_master_volume", unreadable)
        new.start()
    assert new.sessions_failed == 1
    assert app.volume_calls == writes
    new.stop()
    old.stop()
    assert app.volume == 1.0
