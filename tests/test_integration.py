"""
Integration tests for the full transcription pipeline.
Tests the flow from audio input to text output.
"""
import pytest
import json
import sys
import threading
import numpy as np
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.integration
class TestTranscriptionPipeline:
    """Integration tests for the transcription pipeline"""

    def test_transcription_to_command_flow(self, temp_commands_file):
        """Test full flow: transcription -> command detection -> execution"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        # Mock the keyboard controller
        mock_press = Mock()
        mock_release = Mock()
        executor.keyboard_controller.press = mock_press
        executor.keyboard_controller.release = mock_release

        # Simulate transcribed text
        transcribed = "copy"

        # Check if it's a command
        command = executor.find_command(transcribed)
        assert command == "copy"

        # Execute
        result = executor.execute_command(command)
        assert result is True
        # Verify keyboard was used (pynput pattern)
        assert mock_press.call_count >= 2  # ctrl + c
        assert mock_release.call_count >= 2

    def test_transcription_to_dictation_flow(self, sample_config, tmp_path):
        """Test full flow: transcription -> text processing -> paste"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)

        # Simulate transcribed text
        transcribed = "hello world. this is a test"

        # Process the transcription
        processed = app.process_transcription(transcribed)

        # Verify capitalization was applied
        assert processed.startswith("Hello")
        assert "This" in processed  # Capitalized after period

    def test_number_conversion_in_pipeline(self, sample_config, tmp_path):
        """Test number conversion in transcription pipeline"""
        sample_config['format_numbers'] = True
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)

        transcribed = "I have twenty three apples"
        processed = app.process_transcription(transcribed)

        assert "23" in processed

    def test_correction_in_pipeline(self, tmp_path):
        """Test corrections are applied in pipeline"""
        training_data = {
            'vocabulary': [],
            'corrections': {'teh': 'the', 'adn': 'and'}
        }
        training_file = tmp_path / 'training_data.json'
        with open(training_file, 'w') as f:
            json.dump(training_data, f)

        mock_app = Mock()
        mock_app.config_path = tmp_path / 'config.json'
        mock_app.config = {'initial_prompt': ''}

        from samsara.ui.voice_training_qt import VoiceTrainingQt
        vt = VoiceTrainingQt(mock_app)

        transcribed = "teh quick brown fox adn teh lazy dog"
        corrected = vt.apply_corrections(transcribed)

        assert corrected == "the quick brown fox and the lazy dog"


@pytest.mark.integration
class TestCommandModeToggle:
    """Tests for command mode toggle via voice"""

    def test_enable_command_mode_phrase(self, temp_commands_file):
        """Test 'enable command mode' phrase updates app state."""
        import threading
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        mock_app = Mock()
        mock_app.command_matching_enabled = False
        mock_app._config_lock = threading.Lock()
        mock_app.config = {}

        result, was_command = executor.process_text("enable command mode", mock_app)

        assert was_command is True
        assert mock_app.command_matching_enabled is True

    def test_disable_command_mode_phrase(self, temp_commands_file):
        """Test 'disable command mode' phrase updates app state."""
        import threading
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        mock_app = Mock()
        mock_app.command_matching_enabled = True
        mock_app._config_lock = threading.Lock()
        mock_app.config = {}

        result, was_command = executor.process_text("disable command mode", mock_app)

        assert was_command is True
        assert mock_app.command_matching_enabled is False


def _hotkey_app(mode, *, wake_word_enabled=False):
    """A minimal DictationApp carrying only what on_key_press/on_key_release
    read, so the real bound methods -- not a copy of their branching --
    decide hold/toggle behaviour. get_key_name and check_hotkey_state are
    stubbed (they poll real OS key state / pynput key objects, out of
    scope for this unit); everything downstream of "which mode branch
    fires" is the genuine dictation.py code path."""
    from dictation import DictationApp

    app = DictationApp.__new__(DictationApp)
    app.config = {
        'mode': mode,
        'hotkey': 'ctrl+shift',
        'continuous_hotkey': 'ctrl+alt+d',
        'wake_word_hotkey': 'ctrl+alt+w',
        'command_hotkey': 'ctrl+alt+c',
        'cancel_hotkey': 'escape',
        'memo_hotkey': 'ctrl+alt+m',
        'wake_word_enabled': wake_word_enabled,
    }
    app.current_keys = {'ctrl', 'shift'}
    app.key_press_times = {}
    app.hotkey_pressed = False
    app.recording = False
    app.snoozed = False
    app._stop_in_flight = False
    app._streaming_session = None
    app._memo_recording = False
    app.command_mode_recording = False
    app.toggle_active = False
    app.command_mode_active = False
    app._session_mode_manager = None
    app.get_key_name = Mock(return_value='ctrl')
    app._check_command_mode_key = Mock()
    return app


@pytest.mark.integration
class TestRecordingModes:
    """Tests for different recording modes, driven through dictation.py's
    real DictationApp.on_key_press/on_key_release -- not a local
    reimplementation of the hold/toggle branching (see
    docs/reviews/test_suite_audit.md fix-first #7)."""

    def test_hold_mode_flow(self, sample_config):
        """Hold mode: press -> record starts, release -> stop is scheduled."""
        app = _hotkey_app('hold')
        app.check_hotkey_state = Mock(side_effect=lambda combo: combo == 'ctrl+shift')
        app.start_recording = Mock()

        app.on_key_press('ctrl')

        app.start_recording.assert_called_once_with(streaming=False)
        assert app.hotkey_pressed is True

        # Capture completed (start_recording is mocked away, so simulate
        # its real effect) and the hotkey is now physically released.
        app.recording = True
        app.check_hotkey_state = Mock(return_value=False)
        spawned = {}
        with patch(
            'dictation.thread_registry.spawn',
            side_effect=lambda name, target, **kw: spawned.setdefault(name, target),
        ):
            app.on_key_release('ctrl')

        assert 'stop-rec' in spawned, 'main hotkey release did not schedule a stop'
        app.stop_recording = Mock(side_effect=lambda: setattr(app, 'recording', False))
        spawned['stop-rec']()  # run the deferred stop closure for real

        app.stop_recording.assert_called_once_with()
        assert app.recording is False
        assert app._stop_in_flight is False

    def test_toggle_mode_flow(self, sample_config):
        """Toggle mode: press -> start (toggle on); a later press while
        toggle is active -> stop (toggle off)."""
        app = _hotkey_app('toggle')
        app.check_hotkey_state = Mock(side_effect=lambda combo: combo == 'ctrl+shift')
        app.start_recording = Mock()
        app.stop_recording = Mock()

        app.on_key_press('ctrl')

        app.start_recording.assert_called_once_with(streaming=False)
        app.stop_recording.assert_not_called()
        assert app.toggle_active is True
        assert app.hotkey_pressed is True

        # A later press-release cycle resets the edge-trigger latch; the
        # next press with toggle already active must stop, not start.
        app.hotkey_pressed = False
        app.on_key_press('ctrl')

        app.stop_recording.assert_called_once_with()
        app.start_recording.assert_called_once_with(streaming=False)  # still just the once
        assert app.toggle_active is False

    def test_hold_with_wake_word_flow(self, sample_config):
        """Hold mode's hotkey behaves identically whether wake_word_enabled
        is on or off, and pressing/releasing the main hotkey never touches
        that config key."""
        app = _hotkey_app('hold', wake_word_enabled=True)
        app.check_hotkey_state = Mock(side_effect=lambda combo: combo == 'ctrl+shift')
        app.start_recording = Mock()

        app.on_key_press('ctrl')

        app.start_recording.assert_called_once_with(streaming=False)
        assert app.config['wake_word_enabled'] is True

        app.recording = True
        app.check_hotkey_state = Mock(return_value=False)
        spawned = {}
        with patch(
            'dictation.thread_registry.spawn',
            side_effect=lambda name, target, **kw: spawned.setdefault(name, target),
        ):
            app.on_key_release('ctrl')
        app.stop_recording = Mock(side_effect=lambda: setattr(app, 'recording', False))
        spawned['stop-rec']()

        assert app.recording is False
        assert app.config['wake_word_enabled'] is True  # untouched by the hold flow


def _streaming_consumer(frames):
    """A DictationSessionConsumer with only the streaming-accumulator
    state _snapshot_streaming_audio needs -- bypasses __init__ (which
    wants a live ACE engine) to unit-test the real concatenation method."""
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer

    consumer = DictationSessionConsumer.__new__(DictationSessionConsumer)
    consumer._streaming_lock = threading.Lock()
    consumer._streaming_frames = list(frames)
    return consumer


@pytest.mark.integration
class TestAudioProcessing:
    """Tests for audio buffer processing, against the real ACE streaming
    accumulator (samsara/audio_engine/dictation_consumer.py) instead of a
    reimplementation of np.concatenate (see fix-first #7)."""

    def test_audio_buffer_concatenation(self):
        """Chunks fed into the real streaming accumulator come back
        concatenated in order by the real snapshot method."""
        chunks = [
            np.array([0.1, 0.2, 0.3], dtype=np.float32),
            np.array([0.4, 0.5, 0.6], dtype=np.float32),
            np.array([0.7, 0.8, 0.9], dtype=np.float32),
        ]
        consumer = _streaming_consumer(chunks)

        audio = consumer.snapshot_streaming_audio()

        assert len(audio) == 9
        assert audio[0] == pytest.approx(0.1)
        assert audio[-1] == pytest.approx(0.9)

    def test_empty_audio_buffer(self):
        """The real accumulator's own empty-buffer guard returns None,
        not a locally reimplemented one."""
        consumer = _streaming_consumer([])

        assert consumer.snapshot_streaming_audio() is None

    def test_audio_sample_rate(self):
        """One second of real 100ms frames (FRAME_SIZE samples each, the
        production frame-sizing constant) concatenates through the real
        accumulator to exactly SAMPLE_RATE samples."""
        from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE

        assert SAMPLE_RATE == 16000
        one_second_of_frames = [
            np.zeros(FRAME_SIZE, dtype=np.float32) for _ in range(1000 // 100)
        ]
        consumer = _streaming_consumer(one_second_of_frames)

        audio = consumer.snapshot_streaming_audio()

        assert len(audio) == SAMPLE_RATE


@pytest.mark.integration
class TestHistoryIntegration:
    """Tests for history with the full pipeline"""

    def test_dictation_added_to_history(self, sample_config, tmp_path):
        """Test that dictations are added to history"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("hello world", is_command=False)

        assert len(app.history) == 1
        assert app.history[0][1] == "hello world"
        assert app.history[0][2] is False

    def test_command_added_to_history(self, sample_config, tmp_path):
        """Test that commands are added to history"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("copy", is_command=True)

        assert len(app.history) == 1
        assert app.history[0][1] == "copy"
        assert app.history[0][2] is True


@pytest.mark.integration
class TestFullCommandExecution:
    """Full integration tests for command execution"""

    def test_hotkey_command_full_flow(self, temp_commands_file):
        """Test complete hotkey command flow"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        executor.keyboard_controller.press = Mock()
        executor.keyboard_controller.release = Mock()

        result, was_command = executor.process_text("close window", force_commands=True)

        assert was_command is True

    def test_text_command_full_flow(self, temp_commands_file):
        """Test complete text insertion command flow"""
        from samsara.commands import CommandExecutor

        with patch('time.sleep'):
            executor = CommandExecutor(temp_commands_file)

            result, was_command = executor.process_text("period", force_commands=True)

            assert was_command is True

    def test_launch_command_full_flow(self, temp_commands_file, mock_subprocess):
        """Test complete launch command flow"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        result, was_command = executor.process_text("open chrome", force_commands=True)

        assert was_command is True
        mock_subprocess.assert_called_once()
