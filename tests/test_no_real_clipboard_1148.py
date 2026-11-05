"""Exercise the installed guard, including aliases captured during collection."""
import ctypes
import importlib
import sys

import pytest
import pyperclip

from tests.clipboard_guard import ClipboardAccessError, INPUT_APIS, NATIVE_APIS, guard


# This runs before fixtures: the historical collection-time hole must be closed.
from pyperclip import copy as collected_copy, paste as collected_paste
collected_copy('1148 collection sentinel')
COLLECTION_WAS_FAKE = guard.clipboard.text() == collected_paste() == '1148 collection sentinel'


def assert_blocked(call):
    start = len(guard.violations)
    try:
        with pytest.raises(ClipboardAccessError, match='1148: real clipboard/desktop input blocked'):
            call()
        assert len(guard.violations) > start
    finally:
        # Only expected denials are consumed; unexpected calls fail teardown.
        del guard.violations[start:]


def test_collection_and_imported_aliases_are_memory_only(fake_clipboard):
    assert COLLECTION_WAS_FAKE
    assert not fake_clipboard.formats  # per-test reset
    collected_copy('1148 alias')
    assert collected_paste() == fake_clipboard.text() == '1148 alias'


@pytest.mark.parametrize('factory', [
    'determine_clipboard', 'init_windows_clipboard', 'init_qt_clipboard',
    'init_wsl_clipboard', 'init_xclip_clipboard', 'init_osx_pbcopy_clipboard',
])
def test_pyperclip_backend_selection_stays_fake(factory, fake_clipboard):
    copy, paste = getattr(pyperclip, factory)()
    copy('1148 backend')
    assert paste() == fake_clipboard.text() == '1148 backend'
    pyperclip.set_clipboard('windows')
    assert pyperclip.paste() == '1148 backend'


def test_pyperclip_reload_cannot_restore_real_backend(fake_clipboard):
    importlib.reload(pyperclip).copy('1148 reloaded')
    assert fake_clipboard.text() == '1148 reloaded'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows API loaders')
@pytest.mark.parametrize('name', sorted(NATIVE_APIS))
def test_native_entry_points_fail_loudly(name):
    assert_blocked(lambda: getattr(ctypes.windll.user32, name)())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows API loaders')
@pytest.mark.parametrize('loader', ['WinDLL', 'CDLL', 'OleDLL', 'windll'])
def test_fresh_loaders_and_index_lookup_cannot_escape(loader):
    api = (ctypes.windll.LoadLibrary('user32') if loader == 'windll'
           else getattr(ctypes, loader)('user32'))
    assert_blocked(lambda: api['OpenClipboard'](None))
    assert_blocked(lambda: api[1]())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows symbol resolution')
def test_direct_function_pointer_resolution_is_blocked():
    api = ctypes.WinDLL('user32')
    assert_blocked(lambda: api._FuncPtr(('OpenClipboard', api)))


@pytest.mark.parametrize('module_name, name', [
    (module, attr) for module, names in INPUT_APIS.items() for attr in names
])
def test_python_input_and_ole_entry_points_are_blocked(module_name, name):
    module = pytest.importorskip(module_name)
    if not hasattr(module, name):
        pytest.skip('API not exported on this platform')
    assert_blocked(lambda: getattr(module, name)())


def test_pynput_controller_cannot_inject():
    keyboard = pytest.importorskip('pynput.keyboard')
    controller = keyboard.Controller()
    for name in ('press', 'release', 'tap', 'type'):
        assert_blocked(lambda: getattr(controller, name)('v'))


def test_exception_fallback_cannot_swallow_denial():
    def fallback():
        try:
            guard.block('regression sentinel')()
        except Exception:
            pytest.fail('product fallback swallowed a safety violation')
    assert_blocked(fallback)


@pytest.mark.parametrize('command', ['clip.exe', 'powershell Set-Clipboard marker',
                                    'powershell Get-Clipboard', 'pbcopy', 'xclip'])
def test_shell_clipboard_access_is_blocked_before_spawn(command):
    # 1148: call our hook directly; other audit hooks may reject this event first.
    assert_blocked(lambda: guard.audit('subprocess.Popen', (command, [command], None, None)))


def test_qt_text_and_mime_data_are_memory_only(qapp, fake_clipboard):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    assert qapp.platformName() == 'offscreen'
    clipboard = QGuiApplication.clipboard()
    assert clipboard is QApplication.clipboard() is qapp.clipboard()
    clipboard.setText('1148 Qt')
    assert pyperclip.paste() == fake_clipboard.text() == '1148 Qt'
    pyperclip.copy('1148 Python')
    assert clipboard.text() == '1148 Python'
    mime = QMimeData()
    mime.setHtml('<b>1148</b>')
    mime.setText('1148 mime')
    clipboard.setMimeData(mime)
    assert clipboard.mimeData().html() == '<b>1148</b>'
    clipboard.clear()
    assert clipboard.text() == pyperclip.paste() == ''


def test_qt_image_and_pixmap_stay_in_memory(qapp):
    from PySide6.QtGui import QImage, QPixmap
    clipboard = qapp.clipboard()
    image = QImage(2, 2, QImage.Format_RGB32)
    image.fill(0xff123456)
    clipboard.setImage(image)
    assert clipboard.image() == image
    clipboard.setPixmap(QPixmap.fromImage(image))
    assert clipboard.pixmap().toImage() == image


@pytest.mark.skipif(sys.platform != 'win32', reason='Win32 adapter contract')
def test_win32_and_product_share_exact_binary_formats(fake_clipboard):
    import win32clipboard as win
    from samsara import clipboard
    win.OpenClipboard()
    try:
        win.EmptyClipboard()
        win.SetClipboardData(win.CF_DIB, b'1148 synthetic binary\0\xff')
        win.SetClipboardText('1148 original', win.CF_UNICODETEXT)
    finally:
        win.CloseClipboard()
    saved = clipboard.save_clipboard()
    assert saved[win.CF_DIB] == b'1148 synthetic binary\0\xff'
    pyperclip.copy('1148 replacement')
    assert clipboard.restore_clipboard(saved)
    assert pyperclip.paste() == '1148 original'
    assert fake_clipboard.formats[win.CF_DIB] == b'1148 synthetic binary\0\xff'


def test_test_patch_restores_safe_backend(monkeypatch, fake_clipboard):
    with monkeypatch.context() as patch:
        patch.setattr(pyperclip, 'copy', lambda _: None)
        pyperclip.copy('ignored')
    pyperclip.copy('1148 restored')
    assert fake_clipboard.text() == '1148 restored'


def test_python_children_inherit_guard_even_with_replaced_environment(monkeypatch, tmp_path):
    import subprocess
    from pathlib import Path
    from tests.clipboard_guard import ClipboardGuard
    launched = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            launched.append((args, kwargs))

    monkeypatch.setattr(subprocess, 'Popen', FakePopen)
    (tmp_path / 'sitecustomize.py').write_text('# existing audio guard\n', encoding='utf-8')
    local = ClipboardGuard()
    root = Path(__file__).resolve().parents[1]
    local.protect_children(tmp_path, root)
    subprocess.Popen([sys.executable, '-c', 'pass'], env={'PYTHONPATH': 'replaced'})
    environment = launched[0][1]['env']
    assert environment['PYTHONPATH'].split(__import__('os').pathsep)[0] == str(tmp_path)
    assert environment['SAMSARA_TEST_AUDIO_GUARD'] == str(tmp_path)
    source = (tmp_path / 'sitecustomize.py').read_text(encoding='utf-8')
    assert source.startswith('# existing audio guard\n')
    assert 'from tests.clipboard_guard import install' in source
    assert 'raise SystemExit' in source
    with pytest.raises(ClipboardAccessError, match='Python child'):
        subprocess.Popen([sys.executable, '-I', '-c', 'pass'])
    assert len(launched) == 1


def test_native_qt_app_created_before_guard_is_rejected():
    from types import SimpleNamespace
    application = SimpleNamespace(instance=lambda: SimpleNamespace(platformName=lambda: 'windows'))
    with pytest.raises(pytest.UsageError, match='before the offscreen clipboard guard'):
        guard.patch_qt(SimpleNamespace(QGuiApplication=application))
