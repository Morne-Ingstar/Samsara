"""Process-lifetime clipboard isolation, installed BEFORE test collection (1148).

Normal clipboard APIs share memory. Native escapes and desktop input fail even
inside product ``except Exception`` handlers. No opt-out and no real clipboard
snapshot: preserving the desktop means never opening it in the first place.
"""
import ctypes
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.clipboard_fake import FakeClipboard


class ClipboardAccessError(pytest.fail.Exception):
    """A test tried to use the desktop instead of its injected fake."""


NATIVE_APIS = frozenset({
    'OpenClipboard', 'CloseClipboard', 'EmptyClipboard', 'GetClipboardData',
    'SetClipboardData', 'EnumClipboardFormats', 'CountClipboardFormats',
    'GetClipboardSequenceNumber', 'GetClipboardOwner', 'GetOpenClipboardWindow',
    'IsClipboardFormatAvailable', 'GetPriorityClipboardFormat',
    'GetClipboardFormatNameA', 'GetClipboardFormatNameW',
    'RegisterClipboardFormatA', 'RegisterClipboardFormatW',
    'AddClipboardFormatListener', 'RemoveClipboardFormatListener',
    'SetClipboardViewer', 'ChangeClipboardChain', 'GetClipboardViewer',
    'OleGetClipboard', 'OleSetClipboard', 'OleFlushClipboard', 'OleIsCurrentClipboard',
    'SendInput', 'keybd_event', 'mouse_event',
    'SendMessageA', 'SendMessageW', 'SendMessageTimeoutA', 'SendMessageTimeoutW',
    'PostMessageA', 'PostMessageW', 'SendNotifyMessageA', 'SendNotifyMessageW',
    'GetProcAddress',
})
INPUT_APIS = {
    'pyautogui': ('hotkey', 'press', 'keyDown', 'keyUp', 'write', 'typewrite',
                  'hold', 'click', 'doubleClick', 'tripleClick', 'mouseDown',
                  'mouseUp', 'moveTo', 'moveRel', 'dragTo', 'dragRel', 'scroll',
                  'hscroll', 'vscroll'),
    'keyboard': ('send', 'press', 'release', 'press_and_release', 'write',
                 'play', 'restore_state', 'restore_modifiers'),
    'win32api': ('keybd_event', 'mouse_event'),
    'win32gui': ('SendMessage', 'PostMessage', 'SendMessageTimeout'),
    'pythoncom': ('OleGetClipboard', 'OleSetClipboard', 'OleFlushClipboard',
                  'OleIsCurrentClipboard'),
}


class ClipboardGuard:
    def __init__(self):
        self.clipboard = FakeClipboard()
        self.violations = []
        self.qt_clipboard = None

    def block(self, name):
        def denied(*_args, **_kwargs):
            # API names only: never retain arguments or clipboard contents.
            self.violations.append(name)
            raise ClipboardAccessError(
                f'1148: real clipboard/desktop input blocked: {name}; inject a fake')
        return denied

    def reset(self):
        self.clipboard.__init__()
        if self.qt_clipboard is not None:
            self.qt_clipboard.reset()

    def protect_children(self, directory, source_root):
        """Reuse the audio guard's sitecustomize, including explicit child envs.

        Helpers launched by a test must not get a fresh, unguarded clipboard.
        This wrapper does not launch anything; normal fake Popen seams still work.
        """
        if getattr(self, 'children_protected', False):
            return
        directory = str(directory)
        bootstrap = Path(directory, 'sitecustomize.py')
        source = bootstrap.read_text(encoding='utf-8')
        if '# Clipboard guard 1148' not in source:
            source += (
                '\n# Clipboard guard 1148\n'
                'try:\n'
                '    import sys\n'
                f'    sys.path.insert(0, {str(source_root)!r})\n'
                '    from tests.clipboard_guard import install as _clipboard_install\n'
                f'    _clipboard_install().protect_children({directory!r}, {str(source_root)!r})\n'
                'except BaseException:\n'
                '    raise SystemExit("1148: child clipboard guard could not initialize")\n')
            bootstrap.write_text(source, encoding='utf-8')
        original_popen = subprocess.Popen
        guard = self

        class GuardedPopen(original_popen):
            def __init__(self, args, *positional, **kwargs):
                env = dict(os.environ if kwargs.get('env') is None else kwargs['env'])
                env['SAMSARA_TEST_AUDIO_GUARD'] = directory
                env['PYTHONPATH'] = directory + os.pathsep + env.get('PYTHONPATH', '')
                kwargs['env'] = env
                if isinstance(args, (list, tuple)) and args:
                    executable = Path(str(args[0])).name.lower()
                    flags = list(args[1:])
                    flags = flags[:flags.index('-c')] if '-c' in flags else flags
                    if executable.startswith(('python', 'pypy')) and any(
                            flag in ('-I', '-E', '-S') for flag in flags):
                        guard.block('Python child disabled clipboard guard startup')()
                super().__init__(args, *positional, **kwargs)

        subprocess.Popen = GuardedPopen
        self.children_protected = True

    def patch_module(self, module):
        name = module.__name__
        clip = self.clipboard
        if name == 'pyperclip':
            module.copy = clip.user_copies_text
            module.paste = clip.text
            module.lazy_load_stub_copy = module.copy
            module.lazy_load_stub_paste = module.paste
            module.determine_clipboard = lambda: (module.copy, module.paste)
            for attr in tuple(vars(module)):
                if attr.startswith('init_'):
                    setattr(module, attr, module.determine_clipboard)
            module.set_clipboard = lambda *_: None
        elif name == 'win32clipboard':
            def read(fmt=13):
                raw = clip.formats[fmt]
                return raw.decode('utf-16-le').rstrip('\0') if fmt == 13 else raw

            def write(fmt, data):
                assert clip.is_open
                clip.formats[fmt] = ((data + '\0').encode('utf-16-le')
                                     if fmt == 13 and isinstance(data, str) else bytes(data))
                clip.seq += 1

            exports = {
                'OpenClipboard': lambda owner=None: clip.OpenClipboard(owner),
                'CloseClipboard': clip.CloseClipboard,
                'EmptyClipboard': clip.EmptyClipboard,
                'GetClipboardData': read, 'SetClipboardData': write,
                'SetClipboardText': lambda text, fmt=13: write(fmt, text),
                'EnumClipboardFormats': clip.EnumClipboardFormats,
                'CountClipboardFormats': clip.CountClipboardFormats,
                'IsClipboardFormatAvailable': clip.IsClipboardFormatAvailable,
                'GetClipboardSequenceNumber': clip.GetClipboardSequenceNumber,
                'GetClipboardOwner': clip.GetClipboardOwner,
                'GetOpenClipboardWindow': clip.GetOpenClipboardWindow,
            }
            for attr in tuple(vars(module)):
                if 'Clipboard' in attr and callable(getattr(module, attr)):
                    setattr(module, attr, exports.get(attr, self.block(name + '.' + attr)))
            for attr, value in exports.items():
                setattr(module, attr, value)
        elif name == 'samsara.clipboard':
            # Keep the real snapshot/restore algorithms, including binary data,
            # allocation failures and sequence races. Only the backend is fake.
            module._user32 = SimpleNamespace(**{
                key: getattr(clip, key) for key in dir(clip)
                if key[:1].isupper() and not key.startswith('Global')})
            module._kernel32 = SimpleNamespace(**{
                key: getattr(clip, key) for key in dir(clip) if key.startswith('Global')})
            module.ctypes = SimpleNamespace(**vars(ctypes))
            module.ctypes.string_at = clip.string_at
            module.ctypes.memmove = clip.memmove
        elif name in ('PySide6.QtGui', 'PySide6.QtWidgets'):
            self.patch_qt(module)
        for attr in INPUT_APIS.get(name, ()):
            if hasattr(module, attr):
                setattr(module, attr, self.block(name + '.' + attr))
        if name in ('pynput.keyboard._base', 'pynput.keyboard._win32',
                    'pynput.keyboard._xorg', 'pynput.keyboard._darwin'):
            controller = getattr(module, 'Controller', None)
            for attr in ('press', 'release', 'tap', 'type', '_handle'):
                if controller is not None and hasattr(controller, attr):
                    setattr(controller, attr, self.block(name + '.Controller.' + attr))
        if name.startswith('pyautogui._pyautogui_'):
            for attr in ('_keyDown', '_keyUp', '_click', '_mouseDown', '_mouseUp',
                         '_moveTo', '_dragTo', '_scroll', '_hscroll', '_vscroll'):
                if hasattr(module, attr):
                    setattr(module, attr, self.block(name + '.' + attr))
        if name.startswith('keyboard._'):
            for attr in ('press', 'release', 'type_unicode'):
                if hasattr(module, attr):
                    setattr(module, attr, self.block(name + '.' + attr))

    def patch_qt(self, module):
        application = getattr(module, 'QGuiApplication', None)
        if application is not None:
            instance = application.instance()
            if instance is not None and instance.platformName() != 'offscreen':
                raise pytest.UsageError('1148: Qt was initialized before the offscreen clipboard guard')
        if self.qt_clipboard is None:
            from PySide6.QtCore import QMimeData, QObject, Signal
            from PySide6.QtGui import QClipboard, QImage, QPixmap
            guard = self

            class MemoryQClipboard(QObject):
                dataChanged = Signal()
                selectionChanged = Signal()
                findBufferChanged = Signal()
                changed = Signal(QClipboard.Mode)

                def __init__(self):
                    super().__init__()
                    self.reset()

                def reset(self):
                    self._mime = QMimeData()
                    self._seq = guard.clipboard.seq

                def mimeData(self, mode=QClipboard.Clipboard):
                    if self._seq != guard.clipboard.seq:
                        self.reset()
                        self._mime.setText(guard.clipboard.text())
                    return self._mime

                def setMimeData(self, data, mode=QClipboard.Clipboard):
                    guard.clipboard.user_copies_text(data.text())
                    self._mime, self._seq = data, guard.clipboard.seq
                    self.dataChanged.emit()
                    self.changed.emit(mode)

                def text(self, mode=QClipboard.Clipboard):
                    return self.mimeData(mode).text()

                def setText(self, text, mode=QClipboard.Clipboard):
                    data = QMimeData()
                    data.setText(text)
                    self.setMimeData(data, mode)

                def clear(self, mode=QClipboard.Clipboard):
                    self.setMimeData(QMimeData(), mode)

                def image(self, mode=QClipboard.Clipboard):
                    return self.mimeData(mode).imageData() or QImage()

                def setImage(self, image, mode=QClipboard.Clipboard):
                    data = QMimeData()
                    data.setImageData(image)
                    self.setMimeData(data, mode)

                def pixmap(self, mode=QClipboard.Clipboard):
                    return QPixmap.fromImage(self.image(mode))

                def setPixmap(self, pixmap, mode=QClipboard.Clipboard):
                    self.setImage(pixmap.toImage(), mode)

                def supportsSelection(self):
                    return False

                supportsFindBuffer = supportsSelection
                ownsSelection = supportsSelection
                ownsFindBuffer = supportsSelection

                def ownsClipboard(self):
                    return True

            self.qt_clipboard = MemoryQClipboard()
        for attr in ('QGuiApplication', 'QApplication'):
            application = getattr(module, attr, None)
            if application is not None:
                application.clipboard = staticmethod(lambda: self.qt_clipboard)

    def install(self):
        # Qt's C++ widget copy/paste bypasses Python methods. Offscreen is also
        # required so those operations use Qt's private in-process clipboard.
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        original_getitem = ctypes.CDLL.__getitem__
        guard = self

        def guarded_getitem(dll, name):
            if name in NATIVE_APIS or isinstance(name, int):
                return guard.block('ctypes.' + str(name))
            return original_getitem(dll, name)

        ctypes.CDLL.__getitem__ = guarded_getitem
        # Cover DLL attributes cached before conftest, as well as fresh loads.
        for loader_name in ('windll', 'oledll', 'cdll', 'pydll'):
            loader = getattr(ctypes, loader_name, None)
            if loader is not None:
                for dll in tuple(vars(loader).values()):
                    if isinstance(dll, ctypes.CDLL):
                        for name in NATIVE_APIS.intersection(vars(dll)):
                            setattr(dll, name, self.block('ctypes.' + name))

        def audit(event, args):
            if event.startswith('ctypes.dlsym'):
                name = args[1]
                if name in NATIVE_APIS or isinstance(name, int):
                    guard.block('ctypes.dlsym.' + str(name))()
            elif event in ('subprocess.Popen', 'os.system'):
                command = str(args[:2]).lower()
                if any(token in command for token in (
                    'set-clipboard', 'get-clipboard', 'clip.exe', 'pbcopy',
                    'pbpaste', 'xclip', 'xsel', 'wl-copy', 'wl-paste', 'xdotool')):
                    guard.block('clipboard/input shell command')()

        # Expose the registered hook for side-effect-free policy tests (1148).
        self.audit = audit
        sys.addaudithook(self.audit)

        class Loader:
            def __init__(self, original):
                self.original = original

            def create_module(self, spec):
                create = getattr(self.original, 'create_module', None)
                return create(spec) if create else None

            def exec_module(self, module):
                self.original.exec_module(module)
                guard.patch_module(module)

            def __getattr__(self, name):
                return getattr(self.original, name)

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if (fullname in INPUT_APIS or fullname in (
                        'pyperclip', 'win32clipboard', 'samsara.clipboard',
                        'PySide6.QtGui', 'PySide6.QtWidgets') or
                        fullname.startswith(('pynput.keyboard._', 'pyautogui._pyautogui_',
                                             'keyboard._'))):
                    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                    if spec is not None and spec.loader is not None:
                        spec.loader = Loader(spec.loader)
                    return spec

        sys.meta_path.insert(0, Finder())
        for module in tuple(sys.modules.values()):
            if module is not None:
                self.patch_module(module)


guard = ClipboardGuard()


def install():
    # Conftest can also be loaded by source-inspection tests. Never stack guards.
    if not getattr(guard, 'installed', False):
        guard.installed = True
        guard.install()
    return guard
