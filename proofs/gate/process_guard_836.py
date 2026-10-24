"""Narrow Python test-child permission for the maintained bootstrap adapter."""
from __future__ import annotations

import ast
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import subprocess


def _argv(value):
    if not isinstance(value, str):
        return [os.fsdecode(arg) for arg in value]
    if sys.platform != 'win32':
        return []  # POSIX Popen audit events supply an argv sequence.
    # Windows Popen audits its quoted command line, including multiline -c code.
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(value, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return list(pointer[:count.value])
    finally:
        kernel.LocalFree(pointer)


class TestChildPolicy:
    __test__ = False

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.tests = self.root / 'tests'
        self.python = Path(sys.executable).resolve()
        tree = ast.parse((self.tests / 'conftest.py').read_text(encoding='utf-8'))
        self.watchdog = next(ast.literal_eval(node.value) for node in tree.body
                             if isinstance(node, ast.Assign) and any(
                                 isinstance(target, ast.Name) and
                                 target.id == '_PROCESS_WATCHDOG_SOURCE_705'
                                 for target in node.targets))

    def _test_file(self, path):
        path = Path(path).resolve()
        return path.is_relative_to(self.tests) and path.suffix == '.py' and path.is_file()

    def _multiprocessing_target(self, code):
        # Windows multiprocessing bypasses subprocess.Popen. Admit only its
        # exact stdlib bootstrap, with a real target defined beneath tests/.
        try:
            tree = ast.parse(code)
            first, second = tree.body
            call = second.value
            if (not isinstance(first, ast.ImportFrom) or first.module != 'multiprocessing.spawn'
                    or len(first.names) != 1 or first.names[0].name != 'spawn_main'
                    or first.names[0].asname is not None or first.level
                    or not isinstance(second, ast.Expr) or not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Name) or call.func.id != 'spawn_main'
                    or call.args or {k.arg for k in call.keywords} != {'parent_pid', 'pipe_handle'}
                    or any(not isinstance(k.value, ast.Constant) or
                           type(k.value.value) is not int for k in call.keywords)):
                return False
        except (SyntaxError, ValueError, AttributeError, TypeError):
            return False
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_globals.get('__name__') == 'multiprocessing.popen_spawn_win32':
                target = getattr(frame.f_locals.get('process_obj'), '_target', None)
                filename = getattr(getattr(target, '__code__', None), 'co_filename', None)
                return bool(filename and self._test_file(filename))
            frame = frame.f_back
        return False

    def permits(self, executable, command, cwd=None):
        try:
            argv = _argv(command)
            directory = Path(cwd or Path.cwd()).resolve()
            if (not argv or getattr(sys, 'frozen', False)
                    or Path(os.fsdecode(executable or argv[0])).resolve() != self.python
                    or Path(argv[0]).resolve() != self.python):
                return False
            if any(Path(arg).name.lower() in {'dictation.py', 'main.py', 'samsara.exe'}
                   for arg in argv):
                return False
            index = 1
            while index < len(argv) and argv[index] in {'-B', '-u', '-E', '-s', '-I'}:
                index += 1
            if index >= len(argv):
                return False
            mode = argv[index]
            if mode == '-c':
                if index + 1 >= len(argv):
                    return False
                code = argv[index + 1]
                if code == self.watchdog:
                    # The watchdog only watches this worker and its own temp file.
                    return (len(argv) == index + 4 and argv[index + 2] == str(os.getpid())
                            and Path(argv[index + 3]).resolve().is_relative_to(
                                Path(tempfile.gettempdir()).resolve()))
                return (argv[index + 2:] == ['--multiprocessing-fork']
                        and self._multiprocessing_target(code))
            if mode == '-m':
                if index + 1 >= len(argv):
                    return False
                module = argv[index + 1].split('.')
                if module[0] != 'tests' or any(not part.isidentifier() for part in module):
                    return False
                path = self.root.joinpath(*module)
                # Resolve modules only from this tree, never an ambient package.
                return directory == self.root and (self._test_file(path.with_suffix('.py'))
                                                   or self._test_file(path / '__main__.py'))
            if mode == '--':
                index += 1
                if index >= len(argv):
                    return False
                mode = argv[index]
            return not mode.startswith('-') and self._test_file(directory / mode)
        except (OSError, ValueError, TypeError):
            return False

    def audit(self, event, args):
        if event not in {'subprocess.Popen', '_winapi.CreateProcess'}:
            return
        if event == 'subprocess.Popen':
            if self.permits(*args[:3]):
                return
        else:
            # Some Windows Python builds audit a corrupted command-line value.
            # Use only the real stdlib launch frame, never arbitrary -c text or
            # an unaudited direct CreateProcess call.
            spawn = sys.modules.get('multiprocessing.popen_spawn_win32')
            spawn_code = spawn.Popen.__init__.__code__ if spawn is not None else None
            frame = sys._getframe(1)
            while frame is not None:
                values = frame.f_locals
                if frame.f_code is subprocess.Popen._execute_child.__code__:
                    if self.permits(values['executable'], values['args'], values['cwd']):
                        return
                    break
                if frame.f_code is spawn_code:
                    if self.permits(values['python_exe'], values['cmd']):
                        return
                    break
                frame = frame.f_back
        raise AssertionError('gate forbids process launch/signal: ' + event)
