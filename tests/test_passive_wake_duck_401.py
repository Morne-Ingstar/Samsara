"""Passive duck boundaries with AST-bound app methods and real dispatch FIFOs."""

import ast
import os
import subprocess
import tempfile
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

# Establish a temporary home before importing any Samsara module.
os.environ['SAMSARA_HOME_DIR'] = tempfile.mkdtemp(prefix='passive_wake_401_')

import pytest

from samsara.audio_engine import wake_consumer as consumer_module
from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
from samsara.stage_timing import (
    StageTiming, reset_current_stage_timing, set_current_stage_timing,
)
from tests.test_wake_dispatch_lane import audio, drain, rig as dispatch_rig


@pytest.fixture
def rig(dispatch_rig):
    # Supply current production globals only to this file's own AST rig.
    # The original gate's baseline failures remain visible and unchanged.
    dispatch_rig.namespace.update(
        StageTiming=StageTiming,
        set_current_stage_timing=set_current_stage_timing,
        reset_current_stage_timing=reset_current_stage_timing,
    )
    return dispatch_rig


@pytest.fixture(scope='module')
def head_flush():
    source = subprocess.run(
        ['git', 'show', 'HEAD:samsara/audio_engine/wake_consumer.py'],
        check=True, capture_output=True, text=True, encoding='utf-8',
    ).stdout
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'WakeConsumer')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == '_flush')
    namespace = dict(vars(consumer_module))
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'HEAD:_flush', 'exec'),
         namespace)
    return namespace['_flush']


def make(rig, *, primary=False, profile=False, available=True):
    app = rig.make({'wake_profiles': [{'id': 'hermes', 'enabled': True}]}
                   if profile else {})
    app._wake_detector = SimpleNamespace(is_available=available, reset=Mock())
    app._oww_primary_hit = primary
    app._oww_profile_hits = {'hermes'} if profile else set()
    app._oww_wake_detected = primary or profile
    app._open_hands_free_capture_duck = Mock(wraps=app._open_hands_free_capture_duck)
    app._close_hands_free_capture_duck = Mock(wraps=app._close_hands_free_capture_duck)
    app._decode_wake_word_buffer = Mock()
    app.process_wake_word_buffer = Mock(wraps=app.process_wake_word_buffer)
    return app


def no_owners(app):
    app._open_hands_free_capture_duck.assert_not_called()
    app._close_hands_free_capture_duck.assert_not_called()
    assert not app._hands_free_capture_duck_owners
    assert app._wake_consumer._hands_free_capture_duck_token is None


@pytest.mark.parametrize('kind', ['primary', 'profile', 'whisper_only', 'no_hit'])
def test_passive_admission_and_audio_match_head_without_opening_duck(rig, head_flush, kind):
    kwargs = dict(primary=kind == 'primary', profile=kind == 'profile',
                  available=kind != 'whisper_only')
    app, reference = make(rig, **kwargs), make(rig, **kwargs)
    reference._open_hands_free_capture_duck = Mock(return_value=99)
    reference.process_wake_word_buffer = Mock()
    reference._wake_consumer._flush = MethodType(head_flush, reference._wake_consumer)
    samples = audio()
    original_bytes = samples[0].tobytes()
    endpoints = dict(last_voice_ns=20_000_000_000, speech_end_ns=21_000_000_000,
                     speech_start_ns=19_000_000_000)
    reference._wake_consumer._flush(samples, **endpoints)
    app._wake_consumer._flush(samples, **endpoints)
    drain(rig)
    no_owners(app)
    assert samples[0].tobytes() == original_bytes
    assert app._wake_detector.reset.call_args_list == reference._wake_detector.reset.call_args_list
    assert app._oww_primary_hit is reference._oww_primary_hit is False
    assert app._oww_profile_hits == reference._oww_profile_hits == set()
    if kind == 'no_hit':
        app.process_wake_word_buffer.assert_not_called()
        reference.process_wake_word_buffer.assert_not_called()
        app._decode_wake_word_buffer.assert_not_called()
        return
    app.process_wake_word_buffer.assert_called_once()
    actual = app.process_wake_word_buffer.call_args
    expected = reference.process_wake_word_buffer.call_args
    assert actual.kwargs == dict(expected.kwargs, owner_token=None)
    assert actual.kwargs['oww_confirmed'] is (kind in ('primary', 'profile'))
    assert actual.args[1] == expected.args[1] == consumer_module.SAMPLE_RATE
    submitted, old_submitted = actual.args[0], expected.args[0]
    assert submitted[0] is old_submitted[0] is samples[0]
    if kind == 'profile':
        assert isinstance(submitted, AdmittedWakeBuffer)
        assert submitted.primary_hit == old_submitted.primary_hit is False
        assert submitted.profile_hits == old_submitted.profile_hits == frozenset({'hermes'})
    else:
        assert submitted is old_submitted is samples
    app._decode_wake_word_buffer.assert_called_once()
    assert app._decode_wake_word_buffer.call_args.args[0] is submitted


@pytest.mark.parametrize('state, triggered', [
    ('wake_session', False), ('long_dictation', False),
    ('command_window', True), ('asleep', True), ('quick_dictation', False),
])
def test_non_passive_dispatch_opens_hands_over_and_closes_once(rig, state, triggered):
    app = make(rig)
    app.app_state, app.wake_word_triggered = state, triggered
    app._wake_consumer._flush(audio())
    drain(rig)
    app._open_hands_free_capture_duck.assert_called_once_with()
    token = app.process_wake_word_buffer.call_args.kwargs['owner_token']
    assert token is not None
    app._close_hands_free_capture_duck.assert_called_once_with(token)
    assert not app._hands_free_capture_duck_owners
    assert app.process_wake_word_buffer.call_args.kwargs['tracked'] is (state == 'long_dictation')
    assert app.process_wake_word_buffer.call_args.kwargs['oww_confirmed'] is False
    assert app._pending_transcriptions == 0


@pytest.mark.parametrize('passive', [True, False])
def test_existing_wake_owner_passes_through_and_closes_once(rig, passive):
    app = make(rig, primary=True)
    if not passive:
        app.app_state = 'long_dictation'
    token = app._open_hands_free_capture_duck()
    app._open_hands_free_capture_duck.reset_mock()
    app._wake_consumer._hands_free_capture_duck_token = token
    app._wake_consumer._flush(audio(), token)
    app._wake_consumer.abort_utterance()
    drain(rig)
    app._open_hands_free_capture_duck.assert_not_called()
    assert app.process_wake_word_buffer.call_args.kwargs['owner_token'] == token
    app._close_hands_free_capture_duck.assert_called_once_with(token)
    assert not app._hands_free_capture_duck_owners
    assert app._wake_consumer._hands_free_capture_duck_token is None


def test_existing_toggle_capture_owner_is_not_reopened_or_closed_twice(rig):
    app = make(rig)
    app.config['command_mode'] = {'mode': 'toggle'}
    app.command_mode_active = True
    app._handle_command_mode_utterance = Mock()
    token = app._open_hands_free_capture_duck()
    app._open_hands_free_capture_duck.reset_mock()
    app._wake_consumer._hands_free_capture_duck_token = token
    samples = audio()
    app._wake_consumer._flush(samples, token)
    drain(rig)
    app._wake_consumer.abort_utterance()
    app._open_hands_free_capture_duck.assert_not_called()
    app.process_wake_word_buffer.assert_not_called()
    app._handle_command_mode_utterance.assert_called_once_with(samples, consumer_module.SAMPLE_RATE)
    app._close_hands_free_capture_duck.assert_called_once_with(token)
    assert not app._hands_free_capture_duck_owners


def test_passive_decode_exception_creates_no_owner(rig):
    app = make(rig, primary=True)
    app._decode_wake_word_buffer.side_effect = RuntimeError('decoder failed')
    app._wake_consumer._flush(audio())
    drain(rig)
    app._decode_wake_word_buffer.assert_called_once()
    no_owners(app)
    assert not app._wake_dispatch_queue._active


@pytest.mark.parametrize('outcome', ['overflow', 'listener_stopped'])
def test_passive_waiting_jobs_never_own_or_close_a_duck(rig, outcome):
    app = make(rig, primary=True)
    entered, release = threading.Event(), threading.Event()
    seen = []

    def decode(samples, *_args, **_kwargs):
        seen.append(int(samples[0][0]))
        if seen == [1]:
            entered.set()
            assert release.wait(3)

    app._decode_wake_word_buffer.side_effect = decode
    try:
        app._wake_consumer._flush(audio(1))
        assert entered.wait(3)
        for value in range(2, 7 if outcome == 'overflow' else 3):
            app._oww_primary_hit = True
            app._wake_consumer._flush(audio(value))
        if outcome == 'listener_stopped':
            app.wake_word_active = False
    finally:
        release.set()
    drain(rig)
    no_owners(app)
    assert seen == ([1, 3, 4, 5, 6] if outcome == 'overflow' else [1])
    assert any(name == 'wake.dispatch_dropped' and fields['reason'] == outcome
               for name, fields in rig.events)
    assert not app._wake_dispatch_queue._active
