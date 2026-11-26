"""Volume restoration through synthetic WASAPI sessions and transport failures."""

import json

import pytest

from samsara import audio_ducking as ad
from tests.test_audio_ducking import (
    NamedSession,
    _fake,
    _hard_kill_and_restart,
    _vanish,
    journal,
    no_threads,
    use_sessions,
)


def _app(sid="browser", volume=0.73453, instance="browser-a", pid=333):
    return NamedSession(sid, pid, volume, "browser.exe", instance)


def _entries(journal):
    return json.loads(journal.read_text(encoding="utf-8"))["ducked"]


@pytest.mark.parametrize("nested", [False, True])
def test_release_restores_each_exact_snapshot(journal, nested):
    apps = [_app(), _app("tab-b", 0.01, "browser-b"), _app("tab-c", 0.0, "browser-c")]
    use_sessions(apps)
    ad.configure_duck_journal(journal)
    first, second = ad.SessionDucker(0.2), ad.SessionDucker(0.5)
    first.start()
    first.start()
    if nested:
        second.start()
        assert apps[0].volume == pytest.approx(0.073453)
        first.stop()
        assert apps[0].volume == pytest.approx(0.367265)
    assert [entry["original"] for entry in _entries(journal)] == [0.73453, 0.01, 0.0]
    first.stop()
    second.stop()
    assert [app.volume for app in apps] == [0.73453, 0.01, 0.0]
    assert _entries(journal) == []


def test_recording_error_restores_unchanged_app_and_keeps_mixer_edit(journal):
    app, other = _app(), _app("other", 0.5, "other-b")
    use_sessions([app, other])
    ad.configure_duck_journal(journal)
    with pytest.raises(RuntimeError, match="capture failed"):
        with ad.SessionDucker(0.2):
            app.set_master_volume(app.volume + 0.0005)
            manual = app.volume
            raise RuntimeError("capture failed")
    assert app.volume == manual
    assert other.volume == 0.5
    assert _entries(journal) == []


def test_partial_start_error_keeps_manual_change_and_unwinds_other_sessions(journal, monkeypatch):
    app, other = _app(), _app("other", 0.5, "other-b")
    use_sessions([app, other])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(0.2)

    def fail_timer():
        app.set_master_volume(0.47)
        raise RuntimeError("timer unavailable")

    monkeypatch.setattr(ducker, "_schedule_sweep_locked", fail_timer)
    ducker.start()
    assert ducker.sessions_failed == 1
    assert [app.volume, other.volume] == [0.47, 0.5]
    assert _entries(journal) == []


@pytest.mark.parametrize("landed", [False, True])
def test_failed_duck_write_only_rolls_back_its_own_change(journal, monkeypatch, landed):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)
    setter = app.set_master_volume

    def lose_reply(level):
        if level < 0.2:
            setter(level if landed else 0.47)
            raise OSError("failed duck write")
        setter(level)

    monkeypatch.setattr(app, "set_master_volume", lose_reply)
    ducker = ad.SessionDucker(0.2)
    ducker.start()
    assert ducker.sessions_failed == 1
    assert app.volume == (0.73453 if landed else 0.47)
    assert _entries(journal) == []


def test_failed_release_retries_the_same_instance(journal, monkeypatch):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(0.2)
    ducker.start()
    setter = app.set_master_volume

    def fail_once(level):
        monkeypatch.setattr(app, "set_master_volume", setter)
        raise OSError("transient restore failure")

    monkeypatch.setattr(app, "set_master_volume", fail_once)
    ducker.stop()
    assert app.volume == 0.73453
    assert _entries(journal) == []


class _PowerCut(BaseException):
    """Simulate a hard kill between the journal write and the mixer reply."""


@pytest.mark.parametrize("transition", ["engage", "release"])
@pytest.mark.parametrize("landed", [False, True])
def test_crash_during_nested_transition_restores_either_mixer_level(journal, monkeypatch, transition, landed):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)
    outer, inner = ad.SessionDucker(0.8), ad.SessionDucker(0.2)
    outer.start()
    if transition == "release":
        inner.start()
    setter = app.set_master_volume

    def hard_kill(level):
        if landed:
            setter(level)
        raise _PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(app, "set_master_volume", hard_kill)
        with pytest.raises(_PowerCut):
            (inner.start if transition == "engage" else inner.stop)()

    app.session_id = "restarted-browser"
    assert _hard_kill_and_restart(journal, [app]) == 1
    assert ad.recover_leftover_ducks() == 1
    assert app.volume == 0.73453
    assert _entries(journal) == []


@pytest.mark.parametrize("failure", ["get_master_volume", "set_master_volume"])
def test_failed_startup_recovery_is_kept_and_never_becomes_a_new_original(journal, monkeypatch, failure):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ad.SessionDucker(0.2).start()
    written = app.volume
    assert _hard_kill_and_restart(journal, [app]) == 1

    def unavailable(*args):
        raise OSError("temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app, failure, unavailable)
        assert ad.recover_leftover_ducks() == 0
        assert _entries(journal)[0]["original"] == 0.73453
        second = ad.SessionDucker(0.2)
        second.start()
        assert second.sessions_ducked == 0
        assert app.volume == written
    assert ad.recover_leftover_ducks() == 1
    second.stop()
    assert app.volume == 0.73453
    assert _entries(journal) == []


def test_no_duck_when_the_journal_cannot_save_the_original(journal, monkeypatch):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)

    def fail_replace(*args):
        raise PermissionError("fake read-only journal")

    monkeypatch.setattr(ad.os, "replace", fail_replace)
    ducker = ad.SessionDucker(0.2)
    ducker.start()
    ducker.stop()
    assert ducker.sessions_failed == 1
    assert app.volume_calls == []
    assert app.volume == 0.73453


def test_vanished_app_does_not_restore_into_ambiguous_same_name_sessions(journal):
    app, peer = _app(), _app("peer", 0.30, "peer-b")
    sessions = [app, peer]
    use_sessions(sessions)
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(0.2)
    ducker.start()
    _vanish(sessions, app)
    same_level = app.volume
    first, second = _app("new-a", same_level, "new-a"), _app("new-b", same_level, "new-b")
    sessions.extend([first, second])
    ducker.stop()
    assert peer.volume == 0.30
    assert ad.recover_leftover_ducks() == 0
    assert first.volume_calls == second.volume_calls == []
    assert _entries(journal)[0]["key"] == "instance:browser-a"


def test_existing_duck_survives_a_changed_transport_session_id(journal):
    app = _app()
    use_sessions([app])
    ad.configure_duck_journal(journal)
    first, second = ad.SessionDucker(0.2), ad.SessionDucker(0.5)
    first.start()
    _fake().generation += 1
    app.session_id = "new-host-browser"
    second.start()
    assert second.sessions_ducked == 0
    first.stop()
    second.stop()
    assert app.volume == 0.73453
    assert _entries(journal) == []


def test_recovery_keeps_every_saved_session(journal):
    apps = [_app(f"tab-{i}", 0.5 + i / 1000, f"instance-{i}") for i in range(65)]
    use_sessions(apps)
    ad.configure_duck_journal(journal)
    originals = [app.volume for app in apps]
    ad.SessionDucker(0.2).start()
    assert _hard_kill_and_restart(journal, apps) == len(apps)
    assert ad.recover_leftover_ducks() == len(apps)
    assert [app.volume for app in apps] == originals
