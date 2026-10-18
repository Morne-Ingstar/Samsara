"""Recorded capture-end sequences against real displays; gate adapter, no app boot."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QAbstractAnimation, QEventLoop, QTimer

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import CaptureState, LiveSurfaceModel, NoticeKind


def _run_callbacks(callbacks):
    # Fail a self-rescheduling test callback instead of hanging the worker.
    for _ in range(100):
        if not callbacks:
            return
        fn, args = callbacks.pop(0)
        fn(*args)
    assert not callbacks, 'capture callbacks did not settle within 100 calls'


def _close_display(controller, indicator, qt_deferred_delete):
    widget = controller.widget if isinstance(indicator, LiveSurfaceIndicatorAdapter) else indicator
    if widget is None:
        return
    for animation in widget.findChildren(QAbstractAnimation):
        animation.stop()
    for timer in widget.findChildren(QTimer):
        timer.stop()
    if controller.widget is not None:
        controller.close()
    else:
        indicator.destroy()
        indicator.deleteLater()
    # Deliver only this widget's deletion, while monkeypatches still exist.
    qt_deferred_delete(widget)


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('key', ['primary', 'command', 'ava'])
@pytest.mark.parametrize('ending', ['ghost', 'silent', 'short', 'self_hear', 'no_audio'])
def test_capture_returns_to_rest(qapp, qt_deferred_delete, monkeypatch, display, key, ending):
    from dictation import DictationApp
    import dictation as d
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import LiveSurfaceWidget

    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    callbacks, workers = [], []
    app = object.__new__(DictationApp)
    app.config = {'mode': 'hold', 'recording_tail_ms': 0,
                  'ui': {'live_surface': {'enabled': display == 'Full'}}}
    app.model_rate = 16000
    app.recording = True
    app.hotkey_pressed = False
    app._hotkey_recording = True
    app._ace_dictation_active = True
    app.command_mode_recording = key == 'command'
    app.ava_mode_recording = key == 'ava'
    app._command_mode_ghost_tap = key == 'command' and ending == 'ghost'
    app._ava_mode_ghost_tap = key == 'ava' and ending == 'ghost'
    app._hold_park_finalizing = False
    app._hold_park_requested = display == 'Full' and key == 'primary'
    app._last_recording_earcon_ms = 0
    app._schedule_ui = lambda fn, *args: callbacks.append((fn, args))
    for name in ('play_sound', '_release_icon_chase', '_update_tray_tooltip'):
        setattr(app, name, Mock())
    app._build_hotkey_transcribe_params = lambda: {}
    app._buffer_should_skip_decode = lambda *_args, **_kwargs: True
    audio = None if ending == 'no_audio' else np.zeros(
        1600 if ending in ('ghost', 'short') else 16000, dtype=np.float32)
    app._dictation_consumer = SimpleNamespace(drain=lambda: audio,
                                              drain_after_release=lambda **_: audio)
    monkeypatch.setattr(d, 'is_self_hear_capture', lambda *_: ending == 'self_hear')
    monkeypatch.setattr(d.thread_registry, 'spawn',
                        lambda name, target, daemon: workers.append(target))
    clock = [0.0]
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda model: LiveSurfaceWidget(model, reduced_motion=True))
    app.live_surface = controller
    indicator = LiveSurfaceIndicatorAdapter(controller) if display == 'Full' else ListeningIndicator()
    if display == 'Compact':
        monkeypatch.setattr(indicator, '_now_ms', lambda: clock[0] * 1000)
    app.listening_indicator = indicator

    def flush():
        _run_callbacks(callbacks)
        controller.drain()
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)

    try:
        if display == 'Full':
            controller.start()
        indicator.set_mode('Hold')
        indicator.set_listening(True)
        flush()
        app._stop_recording_impl()
        for _ in range(10):
            if not workers:
                break
            workers.pop(0)()
        assert not workers, 'capture workers did not settle within 10 calls'
        flush()
        proof = os.environ.get('SAMSARA_481_PROOF_DIR')
        if proof and key == 'ava' and ending == 'ghost':
            widget = controller.widget if display == 'Full' else indicator
            widget.grab().save(str(Path(proof) / f'{display}_ava_ghost.png'))
        if ending == 'ghost' and key != 'primary':
            assert getattr(app, '_hold_chip_seq', 0) == 0  # no pending-band flash
        assert not app._hold_park_finalizing
        assert not app._hold_park_requested
        if display == 'Full':
            assert controller.view().capture is CaptureState.OFF
            assert controller.view().notice.kind is NoticeKind.NONE
        else:
            assert not indicator._chip_label
        # Next ordinary capture uses the same real indicator, with normal feedback.
        indicator.set_listening(True)
        flush()
        if display == 'Full':
            assert controller.view().capture is CaptureState.RECORDING
        indicator.set_listening(False)
        seq = app._show_hold_pending_chip()
        app._resolve_hold_chip(seq)
        flush()
        if display == 'Full':
            assert controller.view().capture is CaptureState.OFF
            assert controller.view().notice.kind is NoticeKind.NONE
        else:
            assert not indicator._chip_label
        app._show_outcome_chip('typed', 'success')
        flush()
        if display == 'Full':
            assert controller.view().notice.message == 'typed'
            clock[0] = controller.view().notice.expires_at + .01
            controller.refresh_expired()
            assert controller.view().notice.kind is NoticeKind.NONE
        else:
            assert indicator._chip_label == 'typed'
            assert indicator._chip_timer.isActive()
            # Exercise the connected expiry callback with a deterministic
            # clock instead of pumping unrelated animation timers for 3.5s.
            clock[0] = indicator._chip_deadline + .01
            indicator._chip_timer.timeout.emit()
            assert not indicator._chip_label
    finally:
        _close_display(controller, indicator, qt_deferred_delete)
