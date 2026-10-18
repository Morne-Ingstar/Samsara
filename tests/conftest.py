"""
Shared fixtures and mocks for Samsara tests.
"""
import pytest
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _speech_presence_unknown_for_decoder_fixtures(request, monkeypatch):
    """Synthetic decoder inputs are not a speech-detector corpus (534)."""
    if request.path.name != 'test_speech_presence_534.py':
        from samsara import speech_presence
        monkeypatch.setattr(speech_presence._presence, 'measure',
                            lambda *args: speech_presence.Presence(False))


# ============================================================================
# Shared fake-app baseline (suite stabilization pass)
# ============================================================================
#
# Dozens of test files build their own lightweight `app` stand-in (a
# SimpleNamespace, a `DictationApp.__new__(DictationApp)`, or a small stub
# class) and bind real dictation.py/wake_consumer.py methods onto it. Every
# time production adds a new cross-cutting attribute those methods read off
# `self` -- a lock, a per-lane token owner, a confidence gate -- every one of
# those ad-hoc fakes goes stale at once. apply_fake_app_defaults() is the
# single place to add the next one.
#
# Call it right after constructing the fake object and BEFORE layering your
# test's own specific attributes on top, e.g.:
#
#     app = DictationApp.__new__(DictationApp)
#     apply_fake_app_defaults(app)
#     app.config = {...}          # your test's own overrides follow
#
# It works on any object that allows attribute assignment (SimpleNamespace,
# a real DictationApp.__new__() instance, or a plain custom class) and never
# overwrites an attribute the object already has, so it's safe to call
# before OR after your own setup.
def apply_fake_app_defaults(app):
    import threading

    from samsara.audio_engine.wake_dispatch import TranscriptionOwners
    from samsara.languages import LanguageConfidenceGate

    defaults = {
        # Per-lane (wake/toggle/ava) exactly-once completion token --
        # dictation.py's _handle_command_mode_utterance/_handle_ava_command_
        # utterance/process_wake_word_buffer all claim/release one.
        '_transcription_owners': TranscriptionOwners,
        # Guards wake-session start/expire/restart bookkeeping --
        # process_wake_word_buffer and the _*_wake_session methods.
        '_wake_session_lock': threading.RLock,
        # Serializes start_recording/stop_recording/cancel_recording against
        # concurrent release/cancel/shutdown.
        '_hold_capture_lifecycle_lock': threading.RLock,
        # Per-app rolling language-confidence expectations used by
        # _filter_dictation_language.
        '_language_confidence_gate': LanguageConfidenceGate,
    }
    for name, factory in defaults.items():
        if not hasattr(app, name):
            setattr(app, name, factory())
    return app

# ============================================================================
# Hermetic collection: force SAMSARA_HOME_DIR before any Samsara import
# ============================================================================
# This MUST be plain module-level code, not a fixture (even autouse) --
# pytest fully imports conftest.py (and every test module's own module-level
# imports, e.g. `import dictation`) during collection, before any fixture
# ever runs. samsara/paths.py's samsara_home_dir() -- and samsara/log.py,
# which binds SAMSARA_LOG_FILE from it at import time -- would otherwise
# read/create the real ~/.samsara on the machine running the suite just from
# `pytest --collect-only`. Force-assign (not setdefault) so a developer's
# real SAMSARA_HOME_DIR, if set in their shell, can't leak into the suite.
_SAMSARA_TEST_HOME = tempfile.mkdtemp(prefix="samsara_test_home_")
os.environ["SAMSARA_HOME_DIR"] = _SAMSARA_TEST_HOME


# BEGIN SILENT AUDIO 446
def _install_silent_audio_446():
    import ctypes
    import importlib.abc
    import importlib.machinery
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tempfile
    import threading
    import traceback
    import types

    name = '_samsara_test_audio_446'
    if name in sys.modules:
        return sys.modules[name]
    state = types.ModuleType(name)
    state.calls = []
    state.nodeid = '<collection>'
    state.source = str(Path(__file__).resolve())
    state.real = (os.environ.get('SAMSARA_TEST_AUDIO_ALLOW_REAL') == '1'
                  and os.environ.get('SAMSARA_TEST_REAL_AUDIO') == '1')
    state.originals = {}
    state.lock = threading.RLock()
    sys.modules[name] = state

    def describe(value):
        # Do not persist speech, dictation, raw audio or potentially secret strings.
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if value in ('SystemHand', 'SystemAsterisk', 'SystemExclamation', 'SystemDefault'):
                return value
            return {'type': 'str', 'length': len(value)}
        result = {'type': type(value).__name__}
        if hasattr(value, 'shape'):
            result['shape'] = list(value.shape)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            result['length'] = len(value)
        return result

    def record(call, args=(), kwargs=None, test_id=None):
        frames = traceback.extract_stack(limit=10)[:-2]
        event = {'test': test_id or (state.nodeid if state.nodeid != '<collection>' else
                 os.environ.get('SAMSARA_TEST_AUDIO_NODE', '<collection>')),
                 'pid': os.getpid(), 'call': call,
                 'args': [describe(arg) for arg in args],
                 'kwargs': {key: describe(value) for key, value in (kwargs or {}).items()},
                 'stack': [f'{frame.filename}:{frame.lineno}:{frame.name}' for frame in frames[-5:]],
                 'blocked': True, 'guard': name}
        with state.lock:
            state.calls.append(event)
            path = os.environ.get('SAMSARA_TEST_AUDIO_LOG')
            if path:
                # Separate files avoid interleaving parent/child/thread writes on Windows.
                with open(f'{path}.{os.getpid()}.jsonl', 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(event) + '\n')
        return event

    state.record = record

    class SilentStream:
        """PortAudio-shaped sink; callbacks run against disposable memory only."""
        def __init__(self, call, *args, **kwargs):
            self._nodeid = record(call, args, kwargs)['test']
            self._call = call
            self.callback = kwargs.get('callback')
            self.samplerate = kwargs.get('samplerate') or 44100
            self.channels = kwargs.get('channels') or 1
            if isinstance(self.channels, (tuple, list)):
                self.channels = self.channels[-1]
            self.blocksize = kwargs.get('blocksize') or 4096
            self.dtype = kwargs.get('dtype', 'float32')
            self.device = kwargs.get('device')
            self.latency = 0
            self.active = False
            self.closed = False
            self._stop = threading.Event()
            self._thread = None

        @property
        def stopped(self):
            return not self.active

        @property
        def write_available(self):
            return self.blocksize

        def start(self):
            record(self._call + '.start')
            self.active = True
            self._stop.clear()
            if self.callback and not self._thread:
                def drain():
                    import numpy as np
                    while not self._stop.wait(self.blocksize / self.samplerate):
                        output = np.zeros((self.blocksize, self.channels), dtype=self.dtype)
                        try:
                            self.callback(output, self.blocksize, None, None)
                            if np.any(output):
                                record(self._call + '.callback', (output,), test_id=self._nodeid)
                        except Exception:
                            self.active = False
                            break
                self._thread = threading.Thread(target=drain, daemon=True)
                self._thread.start()
            return self

        def write(self, data):
            record(self._call + '.write', (data,))
            return False

        def stop(self, *args, **kwargs):
            self.active = False
            self._stop.set()
            thread = self._thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
            self._thread = None

        abort = stop

        def close(self, *args, **kwargs):
            self.stop()
            self.closed = True

        def __enter__(self):
            return self.start()

        def __exit__(self, *args):
            self.close()

    state.SilentStream = SilentStream

    def replace(target, attribute, call, stream=False, result=None):
        key = (id(target), attribute)
        if not hasattr(target, attribute):
            return
        original = getattr(target, attribute)
        if getattr(original, '_silent_audio_446', None) is state:
            return
        state.originals[key] = original

        def silent(*args, **kwargs):
            if state.real:
                return original(*args, **kwargs)
            if stream:
                return SilentStream(call, *args, **kwargs)
            record(call, args, kwargs)
            return result

        silent._silent_audio_446 = state
        setattr(target, attribute, silent)

    def patch_module(module):
        if module.__name__ == 'winsound':
            for attribute in ('PlaySound', 'Beep', 'MessageBeep'):
                replace(module, attribute, 'winsound.' + attribute)
        elif module.__name__ == 'sounddevice':
            for attribute in ('play', 'playrec'):
                replace(module, attribute, 'sounddevice.' + attribute)
            for attribute in ('OutputStream', 'RawOutputStream', 'Stream', 'RawStream'):
                replace(module, attribute, 'sounddevice.' + attribute, stream=True)
        elif module.__name__ == 'PySide6.QtWidgets':
            replace(module.QApplication, 'beep', 'QApplication.beep')

    targets = {'winsound', 'sounddevice', 'PySide6.QtWidgets'}

    class Loader:
        def __init__(self, original):
            self.original = original

        def create_module(self, spec):
            return self.original.create_module(spec)

        def exec_module(self, module):
            self.original.exec_module(module)
            patch_module(module)

        def __getattr__(self, attribute):
            return getattr(self.original, attribute)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in targets:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.loader:
                    spec.loader = Loader(spec.loader)
                return spec
            return None

    sys.meta_path.insert(0, Finder())
    for target in targets:
        module = sys.modules.get(target)
        if module is not None:
            patch_module(module)
    if sys.platform == 'win32':
        replace(ctypes.windll.user32, 'MessageBeep', 'ctypes.windll.user32.MessageBeep', result=1)

    # Python children normally replace PYTHONPATH (436 does). Restore the guard
    # at the spawn boundary, not just in the parent's environment.
    guard_dir = os.environ.get('SAMSARA_TEST_AUDIO_GUARD')
    if not guard_dir:
        guard_dir = tempfile.mkdtemp(prefix='samsara_silent_audio_446_')
        source = Path(__file__).read_text(encoding='utf-8')
        block = source.split('# BEGIN SILENT AUDIO 446\n', 1)[1].split('\n# END SILENT AUDIO 446\n', 1)[0]
        Path(guard_dir, 'sitecustomize.py').write_text(
            block + '\n_install_silent_audio_446()\n', encoding='utf-8')
        os.environ['SAMSARA_TEST_AUDIO_GUARD'] = guard_dir
    state.guard_dir = guard_dir
    os.environ['PYTHONPATH'] = guard_dir + os.pathsep + os.environ.get('PYTHONPATH', '')
    original_popen = subprocess.Popen

    class SilentPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if not state.real:
                supplied_env = kwargs.get('env')
                env = dict(os.environ if supplied_env is None else supplied_env)
                env['SAMSARA_TEST_AUDIO_GUARD'] = guard_dir
                env['SAMSARA_TEST_AUDIO_NODE'] = state.nodeid if state.nodeid != '<collection>' else os.environ.get('SAMSARA_TEST_AUDIO_NODE', '<collection>')
                if os.environ.get('SAMSARA_TEST_AUDIO_LOG'):
                    env['SAMSARA_TEST_AUDIO_LOG'] = os.environ['SAMSARA_TEST_AUDIO_LOG']
                env['PYTHONPATH'] = guard_dir + os.pathsep + env.get('PYTHONPATH', '')
                kwargs['env'] = env
                if isinstance(args, (list, tuple)) and args:
                    executable = Path(str(args[0])).name.lower()
                    if executable.startswith(('python', 'pypy')):
                        flags = list(args[1:])
                        flags = flags[:flags.index('-c')] if '-c' in flags else flags
                        if any(flag in ('-I', '-E', '-S') for flag in flags):
                            raise RuntimeError('silent audio guard requires Python site initialization and PYTHONPATH')
            super().__init__(args, *positional, **kwargs)

    subprocess.Popen = SilentPopen
    return state
# END SILENT AUDIO 446


# Install before test collection can bind output aliases. The one autouse
# fixture below provides the per-test recorder and explicit hardware policy.
_silent_audio_446 = _install_silent_audio_446()


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_audio: requires actual audio hardware; opt in with SAMSARA_TEST_REAL_AUDIO=1')


def pytest_collection_modifyitems(config, items):
    if os.environ.get('SAMSARA_TEST_REAL_AUDIO') != '1':
        skip = pytest.mark.skip(reason='real audio disabled; set SAMSARA_TEST_REAL_AUDIO=1 to opt in')
        for item in items:
            if item.get_closest_marker('real_audio'):
                item.add_marker(skip)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _silent_audio_446.calls.clear()
    _silent_audio_446.nodeid = item.nodeid


@pytest.fixture(autouse=True)
def silent_audio(request, monkeypatch):
    """Record output requests without devices, including inherited Python children."""
    real = request.node.get_closest_marker('real_audio') is not None
    if real and os.environ.get('SAMSARA_TEST_REAL_AUDIO') != '1':
        pytest.skip('real audio disabled; set SAMSARA_TEST_REAL_AUDIO=1 to opt in')
    _silent_audio_446.real = real
    monkeypatch.setenv('SAMSARA_TEST_AUDIO_NODE', request.node.nodeid)
    if real:
        monkeypatch.setenv('SAMSARA_TEST_AUDIO_ALLOW_REAL', '1')
    else:
        monkeypatch.delenv('SAMSARA_TEST_AUDIO_ALLOW_REAL', raising=False)
    try:
        yield _silent_audio_446
    finally:
        _silent_audio_446.real = False


def _cleanup_samsara_test_home():
    """Close tagged Samsara log handlers before removing the temp home dir --
    Windows refuses to delete a file that still has an open handle, and the
    RotatingFileHandler samsara/log.py's fallback config (or dictation.py's
    own bootstrap) attaches to the root logger keeps samsara.log open for the
    life of the process."""
    import logging
    from samsara.log import SAMSARA_LOG_HANDLER_TAG

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, SAMSARA_LOG_HANDLER_TAG, False):
            handler.close()
            root.removeHandler(handler)

    shutil.rmtree(_SAMSARA_TEST_HOME, ignore_errors=True)


atexit.register(_cleanup_samsara_test_home)


@pytest.fixture(scope="session", autouse=True)
def _shutdown_live_tts_engines():
    """Retire surviving native speech engines without importing/starting TTS."""
    yield
    try:
        module = sys.modules.get('samsara.tts.winrt_engine')
        engines = tuple(getattr(module, '_live_engines', ()))
    except BaseException:
        return
    for engine in engines:
        try:
            engine.shutdown()
        except BaseException:
            pass


@pytest.fixture(autouse=True)
def _isolate_plugin_registry(tmp_path, monkeypatch):
    """Keep the real plugins/ directory from leaking into tests.

    Tests that want plugin behavior can register via the @command decorator
    after this fixture clears the global registry.
    """
    from samsara import plugin_commands as _plugin_commands
    saved = dict(_plugin_commands._REGISTRY)
    saved_matcher = _plugin_commands._shared_matcher
    _plugin_commands._REGISTRY.clear()
    _plugin_commands._shared_matcher = None

    original_load = _plugin_commands.load_plugins

    def _scoped_load(plugins_dir):
        plugins_dir = Path(plugins_dir)
        project_plugins = Path(__file__).parent.parent / "plugins" / "commands"
        if plugins_dir.resolve() == project_plugins.resolve():
            return 0
        return original_load(plugins_dir)

    monkeypatch.setattr(_plugin_commands, "load_plugins", _scoped_load)
    try:
        yield
    finally:
        _plugin_commands._REGISTRY.clear()
        _plugin_commands._REGISTRY.update(saved)
        _plugin_commands._shared_matcher = saved_matcher


# ============================================================================
# Mock Classes
# ============================================================================

class MockWhisperModel:
    """Mock for faster_whisper.WhisperModel"""

    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type

    def transcribe(self, audio, language="en", beam_size=5, vad_filter=True, initial_prompt=None):
        """Return mock transcription segments"""
        mock_segment = Mock()
        mock_segment.text = "hello world"
        mock_info = Mock()
        mock_info.language = language
        return [mock_segment], mock_info


class MockAudioStream:
    """Mock for sounddevice.InputStream"""

    def __init__(self, **kwargs):
        self.callback = kwargs.get('callback')
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def close(self):
        self.running = False


class MockKeyboardListener:
    """Mock for pynput.keyboard.Listener"""

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class MockTrayIcon:
    """Mock for pystray.Icon"""

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False

    def run(self):
        self.visible = True

    def stop(self):
        self.visible = False


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_commands():
    """Sample commands for testing"""
    return {
        "commands": {
            "open chrome": {
                "type": "launch",
                "target": "chrome.exe",
                "description": "Open Google Chrome"
            },
            "close window": {
                "type": "hotkey",
                "keys": ["alt", "f4"],
                "description": "Close active window"
            },
            "copy": {
                "type": "hotkey",
                "keys": ["ctrl", "c"],
                "description": "Copy selection"
            },
            "period": {
                "type": "text",
                "text": ".",
                "description": "Insert period"
            },
            "new line": {
                "type": "press",
                "key": "enter",
                "description": "Insert new line"
            },
            "double click": {
                "type": "mouse",
                "action": "double_click",
                "description": "Double click mouse"
            },
            "hold shift": {
                "type": "key_down",
                "key": "shift",
                "description": "Hold Shift key"
            },
            "release shift": {
                "type": "key_up",
                "key": "shift",
                "description": "Release Shift key"
            },
            "release all": {
                "type": "release_all",
                "description": "Release all held keys"
            }
        }
    }


@pytest.fixture
def sample_config():
    """Sample configuration for testing"""
    return {
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "cancel_hotkey": "escape",
        "mode": "hold",
        "model_size": "base",
        "language": "en",
        "auto_paste": True,
        "add_trailing_space": True,
        "auto_capitalize": True,
        "format_numbers": True,
        "device": "auto",
        "microphone": None,
        "silence_threshold": 2.0,
        "min_speech_duration": 0.3,
        "command_mode_enabled": True,
        "wake_word": "hey samsara",
        "wake_word_timeout": 5.0,
        "audio_feedback": True,
        "sound_volume": 0.5
    }


@pytest.fixture
def temp_commands_file(tmp_path, sample_commands):
    """Create a temporary commands.json file"""
    commands_file = tmp_path / "commands.json"
    with open(commands_file, 'w') as f:
        json.dump(sample_commands, f)
    return commands_file


@pytest.fixture
def temp_config_file(tmp_path, sample_config):
    """Create a temporary config.json file"""
    config_file = tmp_path / "config.json"
    with open(config_file, 'w') as f:
        json.dump(sample_config, f)
    return config_file


@pytest.fixture
def mock_whisper():
    """Mock the WhisperModel"""
    with patch('faster_whisper.WhisperModel', MockWhisperModel):
        yield MockWhisperModel


@pytest.fixture
def mock_audio():
    """Mock sounddevice"""
    with patch('sounddevice.InputStream', MockAudioStream):
        with patch('sounddevice.query_devices', return_value=[
            {'name': 'Test Microphone', 'max_input_channels': 2, 'index': 0},
            {'name': 'Another Mic', 'max_input_channels': 1, 'index': 1},
        ]):
            yield


@pytest.fixture
def mock_keyboard():
    """Mock pynput keyboard"""
    with patch('pynput.keyboard.Listener', MockKeyboardListener):
        with patch('pynput.keyboard.Controller'):
            yield


@pytest.fixture
def mock_tray():
    """Mock pystray"""
    with patch('pystray.Icon', MockTrayIcon):
        yield


@pytest.fixture
def mock_pyautogui():
    """Mock pyautogui"""
    with patch('pyautogui.hotkey') as mock_hotkey:
        with patch('pyautogui.press') as mock_press:
            with patch('pyautogui.click') as mock_click:
                with patch('pyautogui.doubleClick') as mock_double:
                    with patch('pyautogui.keyDown') as mock_keydown:
                        with patch('pyautogui.keyUp') as mock_keyup:
                            yield {
                                'hotkey': mock_hotkey,
                                'press': mock_press,
                                'click': mock_click,
                                'doubleClick': mock_double,
                                'keyDown': mock_keydown,
                                'keyUp': mock_keyup
                            }


@pytest.fixture
def mock_pyperclip():
    """Mock pyperclip"""
    clipboard = {'content': ''}

    def mock_copy(text):
        clipboard['content'] = text

    def mock_paste():
        return clipboard['content']

    with patch('pyperclip.copy', mock_copy):
        with patch('pyperclip.paste', mock_paste):
            yield clipboard


@pytest.fixture
def mock_subprocess():
    """Mock subprocess for launch commands"""
    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = Mock()
        yield mock_popen


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication for tests that instantiate Qt widgets."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(scope="session")
def qt_deferred_delete(qapp):
    """Deliver owned deletes, or pump global deletes within a finite budget."""
    from time import monotonic
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop

    def drain(receiver=None):
        # A fixture with an owner must not delete another fixture's objects.
        # Do not dispatch unrelated timers while the owner is being destroyed.
        if receiver is not None:
            QCoreApplication.sendPostedEvents(receiver, QEvent.Type.DeferredDelete)
            return
        deadline = monotonic() + 0.050
        for _ in range(8):
            if monotonic() >= deadline:
                break
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, min(5, remaining_ms))
    return drain
