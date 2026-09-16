"""Queue 154 regression tests for command identity in the outcome ring."""

from samsara import outcome_ring
from samsara.session_modes import outcome_chip


def test_command_chip_identity_survives_recording_without_changing_visible_label():
    """The writer still receives its old string label plus one new record field."""
    label, kind = outcome_chip("command_executed", {"phrase": "switch audio to"})

    record = outcome_ring.record(label, kind, 123.0, "command_executed")

    assert str(label).endswith("switch audio")
    assert record.label == str(label)
    assert record.canonical_id == "audio_switch.switch_audio_to"
    assert outcome_ring.canonical_command_label(record.canonical_id, record.label) == "switch audio to"


def test_legacy_positional_record_has_an_empty_identity_and_still_reads():
    record = outcome_ring.as_record(("typed", "success", 123.0, ""))

    assert record is not None
    assert record.canonical_id == ""
