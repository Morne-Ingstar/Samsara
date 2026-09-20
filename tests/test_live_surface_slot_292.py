"""Queue 292: the real streaming update signal must match its Qt slot."""

from samsara import streaming


def test_three_string_update_signal_connects_and_delivers_to_real_slot(qapp):
    widget = streaming._StreamingWidget(False)
    try:
        widget._w._update_sig.emit(
            "slot delivery succeeded", streaming.StreamingOverlayQt.STATE_LISTENING, "plain",
        )
        qapp.processEvents()
        assert widget._w._label.text() == "slot delivery succeeded"
    finally:
        widget._w.hide()
        widget._w.deleteLater()
        qapp.processEvents()
