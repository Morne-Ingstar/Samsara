"""Keep the app parameter seam and capture ownership across concurrent releases."""
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.command_clip_trim import build_params


def make_app(command=False):
    app = SimpleNamespace(command_mode_recording=command)
    app.get_transcription_params = Mock(side_effect=lambda *, include_vocabulary: {
        'language': 'de', 'initial_prompt': 'vocabulary' if include_vocabulary else '',
    })
    app._build_hotkey_transcribe_params = MethodType(
        sys.modules['dictation'].DictationApp._build_hotkey_transcribe_params, app)
    return app


def test_existing_no_argument_builder_is_used():
    params = {'custom_parameter': object()}
    builder = Mock(return_value=params)
    app = SimpleNamespace(_build_hotkey_transcribe_params=builder)
    assert build_params(app, is_command=True) is params
    builder.assert_called_once_with()


@pytest.mark.parametrize('command', [False, True])
def test_real_builder_uses_capture_instead_of_mutable_flag(command):
    app = make_app(not command)
    params = build_params(app, is_command=command)
    assert params['language'] == ('en' if command else 'de')
    assert params['initial_prompt'] == ('vocabulary' if command else '')
    assert params['vad_filter'] is False
    assert params['condition_on_previous_text'] is False
    app.get_transcription_params.assert_called_once_with(include_vocabulary=command)
    assert app.command_mode_recording is (not command)
    # A later direct call must use the current flag, not the retired capture.
    app._build_hotkey_transcribe_params()
    assert app.get_transcription_params.call_args.kwargs['include_vocabulary'] is (not command)


def test_failed_builder_retires_capture_context():
    app = make_app()
    real_builder = app._build_hotkey_transcribe_params
    app._build_hotkey_transcribe_params = Mock(side_effect=RuntimeError('synthetic failure'))
    with pytest.raises(RuntimeError, match='synthetic failure'):
        build_params(app, is_command=True)
    app._build_hotkey_transcribe_params = real_builder
    assert real_builder()['language'] == 'de'
    app.get_transcription_params.assert_called_once_with(include_vocabulary=False)


def test_nested_other_app_does_not_inherit_command_context():
    app, other = make_app(), make_app()
    real_builder = app._build_hotkey_transcribe_params
    def builder():
        assert other._build_hotkey_transcribe_params()['language'] == 'de'
        return real_builder()
    app._build_hotkey_transcribe_params = builder
    assert build_params(app, is_command=True)['language'] == 'en'
    other.get_transcription_params.assert_called_once_with(include_vocabulary=False)


def test_overlapping_releases_keep_independent_ownership():
    app = make_app()
    barrier = threading.Barrier(2)
    real_builder = app._build_hotkey_transcribe_params
    def builder():
        barrier.wait(timeout=3)
        return real_builder()
    app._build_hotkey_transcribe_params = builder
    with ThreadPoolExecutor(max_workers=2) as workers:
        command = workers.submit(build_params, app, is_command=True)
        ordinary = workers.submit(build_params, app, is_command=False)
        assert command.result(timeout=5)['initial_prompt'] == 'vocabulary'
        assert ordinary.result(timeout=5)['initial_prompt'] == ''
    assert not app.command_mode_recording
