import logging

from samsara.runtime.exit_reason import identify_exit, read_previous_exit, record_exit


def test_exit_sources_map_to_fixed_reasons():
    def _restart_samsara():
        return identify_exit(logger=logging.getLogger("test"))

    reason, source = _restart_samsara()
    assert reason == "restart"
    assert source.endswith(":_restart_samsara")


def test_unknown_exit_logs_python_stack(caplog):
    def unexpected_entry():
        return identify_exit(logger=logging.getLogger("test.exit"))

    with caplog.at_level(logging.WARNING, logger="test.exit"):
        reason, source = unexpected_entry()
    assert (reason, source) == ("unknown", "tests.test_exit_reason_661:unexpected_entry")
    assert "Unknown shutdown request stack" in caplog.text
    assert "unexpected_entry" in caplog.text


def test_last_exit_is_consumed_and_unexpected_is_logged(tmp_path, caplog):
    marker = tmp_path / "last_exit.json"
    record_exit(marker, "unknown")
    with caplog.at_level(logging.WARNING):
        assert read_previous_exit(marker) == "unknown"
    assert "previous exit was unexpected: unknown" in caplog.text
    assert not marker.exists()
