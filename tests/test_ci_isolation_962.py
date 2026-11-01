"""CI regressions: collection preserves native modules and missing scope is safe."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('module', ['tests.test_app_verbs', 'tests.test_window_destination'])
def test_window_test_collection_keeps_native_win32_modules(module):
    # Start without preimporting Win32: that is the collection order which
    # previously installed process-wide MagicMocks on the Windows CI runner.
    script = '''
import importlib
import sys
import types
import pytest

assert 'win32api' not in sys.modules
importlib.import_module(sys.argv[1])
for name in ('win32api', 'win32con', 'win32gui', 'win32process'):
    module = importlib.import_module(name)
    assert isinstance(module, types.ModuleType), (name, type(module))
assert sys.modules['win32con'].WM_CLOSE == 0x10
for name in ('keybd_event', 'GetFileVersionInfo', 'EnumDisplayMonitors'):
    api = sys.modules['win32api']
    original = getattr(api, name)
    for _ in range(2):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(api, name, lambda *args: None)
        assert getattr(api, name) is original
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, module],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('text,expected', [
    ('show numbers', None), ('pause', None), ('resume', None),
    ('skip forward', None), ('skip forward ten', None),
    ('postremia', 'pause stremio'), ('resume strami out', 'resume stremio'),
])
def test_stremio_repairs_without_foreground_context(text, expected):
    from plugins.commands.stremio import repair_couch_command
    assert repair_couch_command(text, None) == expected
