"""Focused tests for the Windows named-mutex single-instance helper."""

import sys

import pytest

from samsara import single_instance


def test_default_mutex_name_when_home_override_is_unset(monkeypatch):
    monkeypatch.delenv("SAMSARA_HOME_DIR", raising=False)

    assert (
        single_instance.mutex_name_for_profile()
        == single_instance.DEFAULT_MUTEX_NAME
    )


def test_empty_home_override_uses_default_mutex_name(monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", "")

    assert (
        single_instance.mutex_name_for_profile()
        == single_instance.DEFAULT_MUTEX_NAME
    )


def test_home_override_is_read_at_call_time(monkeypatch, tmp_path):
    profile = tmp_path / "preview-profile"
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(profile))

    assert single_instance.mutex_name_for_profile() == (
        single_instance.mutex_name_for_profile(profile)
    )


def test_explicit_none_selects_default_profile_despite_override(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "preview-profile"))

    assert (
        single_instance.mutex_name_for_profile(None)
        == single_instance.DEFAULT_MUTEX_NAME
    )


def test_distinct_home_overrides_get_distinct_mutexes(tmp_path):
    first = single_instance.mutex_name_for_profile(tmp_path / "preview-one")
    second = single_instance.mutex_name_for_profile(tmp_path / "preview-two")

    assert first != second


def test_equivalent_profile_paths_get_the_same_mutex(tmp_path):
    profile = tmp_path / "preview-profile"
    with_trailing_separator = str(profile) + "\\"

    assert single_instance.mutex_name_for_profile(profile) == (
        single_instance.mutex_name_for_profile(with_trailing_separator)
    )


def test_non_windows_acquire_is_a_noop(monkeypatch):
    monkeypatch.setattr(single_instance.sys, "platform", "linux")
    monkeypatch.setattr(
        single_instance,
        "_create_windows_mutex",
        lambda _name: pytest.fail("Win32 API should not be loaded"),
    )

    assert single_instance.acquire_single_instance_mutex() is None


def test_collision_closes_redundant_handle_and_raises(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(single_instance.sys, "platform", "win32")
    monkeypatch.setattr(
        single_instance, "_create_windows_mutex", lambda _name: (123, True)
    )
    monkeypatch.setattr(single_instance, "_close_windows_handle", closed.append)

    with pytest.raises(single_instance.AlreadyRunningError) as exc_info:
        single_instance.acquire_single_instance_mutex(tmp_path / "profile")

    assert closed == [123]
    assert exc_info.value.mutex_name.startswith(
        single_instance.DEFAULT_MUTEX_NAME + ".Profile."
    )


def test_unexpected_windows_failure_propagates_without_becoming_collision(
    monkeypatch,
):
    monkeypatch.setattr(single_instance.sys, "platform", "win32")

    def fail_to_create(_name):
        raise OSError("CreateMutexW failed")

    monkeypatch.setattr(single_instance, "_create_windows_mutex", fail_to_create)

    with pytest.raises(OSError, match="CreateMutexW failed"):
        single_instance.acquire_single_instance_mutex(None)


def test_mutex_close_is_idempotent(monkeypatch):
    closed = []
    monkeypatch.setattr(single_instance, "_close_windows_handle", closed.append)
    mutex = single_instance.WindowsMutex(456, "test-mutex")

    mutex.close()
    mutex.close()

    assert mutex.closed is True
    assert closed == [456]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex API")
def test_real_named_mutex_rejects_second_handle_until_closed(tmp_path):
    profile = tmp_path / "real-mutex-profile"
    first = single_instance.acquire_single_instance_mutex(profile)
    assert first is not None

    try:
        with pytest.raises(single_instance.AlreadyRunningError):
            single_instance.acquire_single_instance_mutex(profile)
    finally:
        first.close()

    replacement = single_instance.acquire_single_instance_mutex(profile)
    assert replacement is not None
    replacement.close()
