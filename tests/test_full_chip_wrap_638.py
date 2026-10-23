"""Full feedback wraps to two lines before end truncation; hover stays complete."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from samsara.live_surface.model import Notice, NoticeKind
from samsara.ui.live_surface_qt import _FeedbackBand


@pytest.mark.parametrize("message", [
    "Ava: needs attention: out of balance",
    "Ava: needs attention: model or route unavailable",
    "Ava: needs attention " + "long-word" * 30,
])
def test_long_full_chip_truncates_only_at_end_and_keeps_full_accessible_text(qapp, message):
    host = QWidget()
    chip = _FeedbackBand(host, "surfaceChip", chip=True)
    chip.resize(180, 60)
    chip.set_notice(Notice(NoticeKind.ERROR, message, None))
    chip.restyle()
    host.show(); qapp.processEvents()
    text = chip.label.text()
    from samsara.ui.indicator_message_layout import message_lines
    lines, elided = message_lines(message, chip.label.font(), chip.label.width())
    assert text == "\n".join(lines) and len(lines) <= 2
    assert chip.elided == elided
    assert text.endswith("…") if elided else " ".join(text.split()) == message
    assert chip.accessibleName() == chip.label.accessibleName() == message
    assert chip.toolTip() == chip.label.toolTip() == message
    chip.resize(chip.desired_width(), chip.required_height(chip.desired_width())); qapp.processEvents()
    assert chip.label.text() == message
    host.close()


def test_short_full_chip_text_is_unchanged(qapp):
    host = QWidget()
    chip = _FeedbackBand(host, "surfaceChip", chip=True)
    chip.resize(180, 60)
    chip.set_notice(Notice(NoticeKind.SUCCESS, "Ava: ready", None))
    chip.restyle()
    host.show(); qapp.processEvents()
    assert chip.label.text() == "Ava: ready"
    assert not chip.elided
    host.close()
