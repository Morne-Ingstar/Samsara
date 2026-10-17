"""Full display feedback: end truncation preserves the fixed Full header."""

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
    chip.resize(180, 26)
    chip.set_notice(Notice(NoticeKind.ERROR, message, None))
    chip.restyle()
    host.show(); qapp.processEvents()
    text = chip.label.text()
    assert text.endswith("…") and "…" not in text[:-1]
    assert message.startswith(text[:-1])
    assert text == chip.label.fontMetrics().elidedText(message, Qt.TextElideMode.ElideRight, chip.label.width())
    assert chip.height() == 26 and not chip.label.wordWrap()
    assert chip.accessibleName() == chip.label.accessibleName() == message
    assert chip.toolTip() == chip.label.toolTip() == message
    chip.resize(chip.desired_width(), 26); qapp.processEvents()
    assert chip.label.text() == message
    host.close()


def test_short_full_chip_text_is_unchanged(qapp):
    host = QWidget()
    chip = _FeedbackBand(host, "surfaceChip", chip=True)
    chip.resize(180, 26)
    chip.set_notice(Notice(NoticeKind.SUCCESS, "Ava: ready", None))
    chip.restyle()
    host.show(); qapp.processEvents()
    assert chip.label.text() == "Ava: ready"
    assert chip.height() == 26
    host.close()
