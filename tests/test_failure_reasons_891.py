"""891: real message handoff and capture exits, with synthetic PCM only."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import failure_reasons as reasons
from samsara.message_durations import message_duration_ms


def host(level='brief'):
    return SimpleNamespace(config={'hotkey': 'ctrl+alt+k', 'tts': {'feedback_level': level}},
                           model_rate=16000, _schedule_ui=lambda fn, *a: fn(*a),
                           _speak_session_notice=Mock(), _log_history=Mock(),
                           listening_indicator=SimpleNamespace(show_outcome=Mock()))


@pytest.mark.parametrize('kind,words', [
    ('short_tap', 'Too short'), ('near_silent', 'Nothing heard'),
    ('long_failure', "Couldn't transcribe that")])
def test_each_failure_has_specific_actionable_copy(kind, words):
    text, hint = reasons.reason_for(kind, {'config': {'hotkey': 'ctrl+alt+k'}})
    assert text.startswith(words)
    assert 'Report a problem' == hint if kind == 'long_failure' else not hint


def test_short_tap_reuses_the_home_key_accessor():
    from samsara.ui.home_qt import hotkey_label
    assert hotkey_label('ctrl+alt+k') in reasons.reason_for(
        'short_tap', {'config': {'hotkey': 'ctrl+alt+k'}})[0]
    text, _ = reasons.reason_for('short_tap', {'handsfree': True})
    assert text == 'Say more - that was too short to hear'
    assert 'hold' not in text.lower()


@pytest.mark.parametrize('peak,shown', [(0.8, False), (.989, False), (.99, True), (1., True)])
def test_clipping_uses_pcm_amplitude_at_the_boundary(peak, shown):
    app = host()
    reasons.post_clipping(app, np.array([0., -peak], dtype=np.float64))
    assert app.listening_indicator.show_outcome.called == shown
    if shown:
        label, kind, duration = app.listening_indicator.show_outcome.call_args.args
        assert label == 'Captured\nMic is too loud - turn the mic level down'
        assert kind == 'accent' and duration == message_duration_ms(label, kind)  # 899: shared formula.


@pytest.mark.parametrize('level,count', [('silent', 0), ('brief', 0), ('full', 1)])
def test_headline_spoken_once_only_at_full(level, count):
    app = host(level)
    reasons.post_reason(app, 'near_silent')
    assert app._speak_session_notice.call_count == count
    if count:
        app._speak_session_notice.assert_called_once_with(reasons.reason_for('near_silent', {})[0])


@pytest.mark.parametrize('name', ['Full', 'Compact'])
@pytest.mark.parametrize('kind', ['short_tap', 'near_silent', 'long_failure', 'clipping'])
def test_real_display_receives_reason_and_shared_reading_time(qapp, monkeypatch, name, kind):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator
    app = host()
    app.config['ui'] = {'live_surface': {'enabled': True}}
    controller = None
    if name == 'Full':
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: 0.))
        app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        app.listening_indicator = ListeningIndicator()
        monkeypatch.setattr(app.listening_indicator, '_now_ms', lambda: 0)
    try:
        reasons.post_reason(app, kind, peak=.99)
        if controller:
            controller.drain()
            notice = controller.view().notice
            label, deadline = notice.message, notice.expires_at
            assert notice.owner == 'capture_' + kind
        else:
            label, deadline = app.listening_indicator._chip_label, app.listening_indicator._chip_deadline
        assert reasons.reason_for(kind, {'config': app.config, 'peak': .99})[0] in label
        # 899: reason receipts use the same formula on both displays.
        assert deadline == message_duration_ms(label, 'accent' if kind == 'clipping' else 'error') / 1000
    finally:
        if controller:
            controller.close()
        else:
            app.listening_indicator.destroy()
            app.listening_indicator.deleteLater()


def test_home_message_uses_existing_statusbar_timer_and_does_not_show_hidden_window(qapp):
    from PySide6.QtWidgets import QMainWindow
    app = host()
    window = QMainWindow()
    app.main_window = SimpleNamespace(_window=window)
    reasons.post_reason(app, 'near_silent')
    assert not window.isVisible() and not window.statusBar().currentMessage()
    window.show()
    reasons.post_reason(app, 'near_silent')
    assert window.statusBar().currentMessage() == reasons.reason_for('near_silent', {})[0]
    window.close()


@pytest.mark.parametrize('duration,rejected,word', [(.1, False, 'Say more'), (2., True, 'Nothing heard'), (2., False, None)])
def test_handsfree_detector_keeps_its_verdict_and_has_no_hold_key_instruction(monkeypatch, duration, rejected, word):
    detector = Mock(return_value=rejected)
    monkeypatch.setattr('samsara.speech_presence.skip_decode', detector)
    app = host()
    assert reasons.skip_decode(np.zeros(int(duration*16000)), app, path='handsfree') is rejected
    # 899 / 481: rejected/short pre-decode captures settle without a receipt.
    assert not app.listening_indicator.show_outcome.called


def test_batch_exit_seams_use_reasons_without_changing_detection():
    source = (Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    batch = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'transcribe'
                 and 'Audio too short' in ast.get_source_segment(source, n))
    calls = [n for n in ast.walk(batch) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == 'failure_reasons']
    kinds = [n.args[1].value for n in calls if n.func.attr == 'post_reason' and len(n.args) > 1]
    # 899 / 481: only attempted decodes report failure; taps remain silent.
    assert set(kinds) == {'long_failure'}
    assert any(n.func.attr == 'post_delivery_clipping' for n in calls)


@pytest.mark.parametrize('text,duration,existing,expected', [
    (None, .1, None, 'Too short'), (None, .4, None, 'Nothing heard'),
    (None, 25., None, "Couldn't transcribe"),
    (None, 25., 'Words unclear. Say try again.', 'Words unclear'),
    ('Synthetic speech', 2., None, 'Captured')])
def test_streaming_final_receipt_preserves_existing_specific_guidance(monkeypatch, text, duration, existing, expected):
    monkeypatch.setattr('samsara.tutorial_capture.final_current', lambda _: True)
    session = SimpleNamespace(app=host(), cancel_event=SimpleNamespace(is_set=lambda: False),
                              _deliver_final=Mock(), _decode_failure=existing, _capture_peak_891=1.)
    reasons.deliver_stream_final(session, text, None, duration, 20)
    session._deliver_final.assert_called_once_with(text, None, duration, 20)
    assert session.app.listening_indicator.show_outcome.call_args.args[0].startswith(expected)


def test_cancelled_streaming_result_has_no_new_notice(monkeypatch):
    session = SimpleNamespace(app=host(), cancel_event=SimpleNamespace(is_set=lambda: True), _deliver_final=Mock())
    reasons.deliver_stream_final(session, None, None, 2., 20)
    assert not session.app.listening_indicator.show_outcome.called


def test_only_error_floor_changes():
    assert message_duration_ms('x', 'error', 500) == 2500  # 899: ordinary errors follow 591.
    assert message_duration_ms('x', 'success', 500) == 2500
    assert message_duration_ms('x', 'warning', 500) == 2500


@pytest.mark.parametrize('duration,finished,expected', [(.1, True, 'short_tap'), (2., True, 'near_silent'), (2., False, None)])
def test_streaming_gate_keeps_silent_speech_distinct_from_decode_failure(monkeypatch, duration, finished, expected):
    monkeypatch.setattr('samsara.speech_presence.finish_streaming_if_empty', lambda *_: finished)
    worker = SimpleNamespace(_session=SimpleNamespace(app=host()))
    assert reasons.finish_streaming_if_empty(worker, np.zeros(int(duration*16000))) is finished
    assert worker._session._empty_capture_kind_891 == expected
    session = worker._session
    session.cancel_event = SimpleNamespace(is_set=lambda: False)
    session._deliver_final = Mock()
    monkeypatch.setattr('samsara.tutorial_capture.final_current', lambda _: True)
    if finished:
        reasons.deliver_stream_final(session, None, None, duration, 0)
        # 899 / 481: pre-decode silent/short streaming exits show no failure.
        session.app.listening_indicator.show_outcome.assert_not_called()


def test_stream_exception_keeps_its_failure_kind_without_a_duration():
    session = SimpleNamespace(on_final=Mock())
    reasons.stream_decode_failed(session)
    assert session._empty_capture_kind_891 == 'long_failure'
    session.on_final.assert_called_once_with(None)


def test_existing_recovery_words_stay_readable_and_failed_delivery_has_no_success_notice():
    app = host()
    reasons.report_retained_failure(app)
    assert app.listening_indicator.show_outcome.call_args.args[0] == 'Words unclear. Say try again.'
    app.listening_indicator.show_outcome.reset_mock()
    reasons.post_delivery_clipping(app, np.ones(10), False)
    assert not app.listening_indicator.show_outcome.called


@pytest.mark.parametrize('drain_between', [False, True])
@pytest.mark.parametrize('receipt', ['typed', 'Captured'])
def test_full_clipping_survives_routine_receipts_in_queue_order(monkeypatch, drain_between, receipt):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    app = host()
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: 0.))
    app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    reasons.post_clipping(app, np.ones(2))
    if drain_between:
        controller.drain()
    app.listening_indicator.show_outcome(receipt, 'success')
    controller.drain()
    assert controller.view().notice.owner == 'capture_clipping'
    assert 'Mic is too loud' in controller.view().notice.message
    app.listening_indicator.show_outcome('Ava is off. Turn her on in Settings > Ava.', 'warning', 4000)
    controller.drain()
    assert controller.view().notice.message == 'Ava is off. Turn her on in Settings > Ava.'


def test_full_routine_receipt_is_accepted_after_clipping_expires(monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    now = [0.]
    app = host()
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: now[0]))
    app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    reasons.post_clipping(app, np.ones(2))
    controller.drain()
    now[0] = controller.view().notice.expires_at
    app.listening_indicator.show_outcome('typed', 'success')
    controller.drain()
    assert controller.view().notice.message == 'typed'
