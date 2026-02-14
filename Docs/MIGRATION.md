# Samsara Module Migration Guide

This document describes the new modular architecture introduced to improve code organization, testability, and maintainability.

## New Package Structure

```
samsara/
    __init__.py         # Package exports
    config.py           # Configuration management
    audio.py            # Audio capture and playback
    speech.py           # Speech recognition and text processing
    commands.py         # Voice command execution
    ui/
        __init__.py     # UI exports
        splash.py       # Splash screen component
```

## Module Overview

### Config Module (`samsara.config`)

The `Config` class handles all configuration loading, saving, and management.

```python
from samsara import Config

# Load config (creates defaults if file doesn't exist)
config = Config()

# Get values
hotkey = config.get('hotkey', 'ctrl+shift')
model_size = config['model_size']

# Set values (auto-saves)
config.set('hotkey', 'ctrl+alt')
config['model_size'] = 'small'

# Update multiple values
config.update({
    'auto_capitalize': True,
    'format_numbers': True,
})

# Check if first run
if config.needs_first_run:
    # Show setup wizard
    pass
```

### Audio Module (`samsara.audio`)

#### AudioCapture

Handles microphone input with various recording modes.

```python
from samsara import AudioCapture

# Create capture instance
capture = AudioCapture(sample_rate=16000, device=None)

# Get available devices
devices = AudioCapture.get_devices(show_all=False)

# Test a device
rms_level = AudioCapture.test_device(device_id=0, duration=2.0)

# Start/stop recording
capture.start()
# ... record audio ...
audio_data = capture.stop()  # Returns numpy array

# Streaming mode with callback
def on_audio(data, frames, time_info, status):
    # Process audio chunk
    pass

capture.start(callback=on_audio, blocksize=1600)
```

#### AudioPlayer

Handles audio feedback sounds with volume control.

```python
from samsara import AudioPlayer

# Create player
player = AudioPlayer(volume=0.5, enabled=True)

# Play feedback sounds
player.play('start')
player.play('stop')
player.play('success')
player.play('error')

# Adjust settings
player.set_volume(0.8)
player.set_enabled(False)

# Custom sounds
player.set_custom_sound('start', Path('/path/to/custom.wav'))
player.reset_sound('start')  # Restore default
```

### Speech Module (`samsara.speech`)

#### SpeechRecognizer

Wrapper for the Whisper speech-to-text model.

```python
from samsara import SpeechRecognizer

# Create recognizer
recognizer = SpeechRecognizer(
    model_size='base',
    device='auto',
    language='en',
)

# Load model (blocking)
recognizer.load()

# Or load async
recognizer.load_async(callback=on_loaded)

# Check status
if recognizer.is_loaded:
    # Transcribe audio
    text, info = recognizer.transcribe(
        audio_array,
        initial_prompt="Technical vocabulary: Python, API...",
    )
    print(text)
    print(f"Duration: {info['duration']}s")
```

#### TextProcessor

Processes transcribed text with formatting and corrections.

```python
from samsara import TextProcessor

# Create processor
processor = TextProcessor(
    auto_capitalize=True,
    format_numbers=True,
    corrections={'teh': 'the', 'adn': 'and'},
)

# Process text (applies all transformations)
processed = processor.process("teh answer is twenty one")
# Result: "The answer is 21"

# Individual operations
text = processor.capitalize("hello world")
text = processor.convert_numbers("twenty one")
text = processor.apply_corrections("teh cat")

# Update corrections
processor.add_correction('misheard', 'correct')
processor.remove_correction('misheard')
```

### Commands Module (`samsara.commands`)

Handles voice command loading, matching, and execution.

```python
from samsara import CommandExecutor

# Create executor
executor = CommandExecutor()

# Find matching command
cmd = executor.find_command("copy that please")
if cmd:
    executor.execute_command(cmd)

# Process text with command mode
def on_mode_change(enabled):
    print(f"Command mode: {enabled}")

executor.set_command_mode_callback(on_mode_change)

result, was_command = executor.process_text(
    "copy this",
    command_mode_enabled=True,
)

if was_command:
    print(f"Executed command: {result}")
else:
    print(f"Dictation text: {result}")

# Manage commands
executor.add_command('test', 'hotkey', keys=['ctrl', 't'])
executor.remove_command('test')
executor.save_commands()
```

### UI Module (`samsara.ui`)

#### SplashScreen

Startup splash screen component.

```python
from samsara import SplashScreen

# Show splash
splash = SplashScreen(min_display_time=3.0)

# Update status
splash.set_status("Loading configuration...")
splash.set_status("Loading speech model...")

# Close and get root window
root = splash.get_root()
splash.close()

# Or fully destroy
splash.destroy()
```

## Dependency Injection Pattern

The new modules support dependency injection for testing:

```python
from samsara import Config, AudioCapture, SpeechRecognizer, CommandExecutor, TextProcessor

class DictationService:
    def __init__(
        self,
        config: Config,
        audio: AudioCapture,
        recognizer: SpeechRecognizer,
        commands: CommandExecutor,
        processor: TextProcessor,
    ):
        self.config = config
        self.audio = audio
        self.recognizer = recognizer
        self.commands = commands
        self.processor = processor

    def transcribe(self):
        audio_data = self.audio.stop()
        text, info = self.recognizer.transcribe(audio_data)
        return self.processor.process(text)
```

## Testing

Each module can be tested independently:

```python
def test_config():
    config = Config(tmp_path / "config.json")
    config.set('hotkey', 'ctrl+alt')
    assert config.get('hotkey') == 'ctrl+alt'

def test_text_processor():
    processor = TextProcessor(auto_capitalize=True)
    assert processor.process("hello") == "Hello"

def test_command_executor(mock_keyboard):
    executor = CommandExecutor()
    assert executor.find_command("copy") is not None
```

## Breaking Changes

1. **Import paths**: Components should now be imported from `samsara` package:
   ```python
   # Old (from dictation.py)
   from dictation import CommandExecutor

   # New
   from samsara import CommandExecutor
   ```

2. **Config access**: Configuration is now handled through the `Config` class:
   ```python
   # Old
   app.config['hotkey']

   # New (with Config instance)
   config.get('hotkey')
   ```

3. **Audio handling**: Audio is now handled through dedicated classes:
   ```python
   # Old (in DictationApp)
   self.stream = sd.InputStream(...)

   # New
   capture = AudioCapture(sample_rate=16000)
   capture.start()
   ```

## Backward Compatibility

The existing `dictation.py` continues to work as the main application entry point. The new modules can be gradually adopted:

1. Import and use `Config` for configuration management
2. Use `TextProcessor` for text processing
3. Use `AudioCapture` and `AudioPlayer` for audio handling
4. Use `SpeechRecognizer` for model management
5. Use `CommandExecutor` for command handling

## Future Work

- Extract remaining UI components (SettingsWindow, HistoryWindow, FirstRunWizard)
- Create a unified `DictationService` that coordinates all modules
- Add async/await support for modern Python patterns
- Add plugin system for extending functionality
