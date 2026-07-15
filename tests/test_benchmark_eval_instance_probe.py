"""Focused coverage for benchmark_eval's primary-instance probe."""

from unittest.mock import Mock

from tools import benchmark_eval


def test_probe_uses_default_profile_and_closes_temporary_mutex(monkeypatch):
    acquired = Mock()
    acquire = Mock(return_value=acquired)
    monkeypatch.setattr(
        benchmark_eval.single_instance,
        "acquire_single_instance_mutex",
        acquire,
    )

    assert benchmark_eval._main_samsara_running() is False
    acquire.assert_called_once_with(profile_dir=None)
    acquired.close.assert_called_once_with()


def test_probe_reports_mutex_collision(monkeypatch):
    def collision(*, profile_dir):
        assert profile_dir is None
        raise benchmark_eval.single_instance.AlreadyRunningError("primary")

    monkeypatch.setattr(
        benchmark_eval.single_instance,
        "acquire_single_instance_mutex",
        collision,
    )

    assert benchmark_eval._main_samsara_running() is True
