"""Bounded Windows drivers for named players; never send global media keys.

VLC protocol: VideoLAN 3.0.x main_interface_win32.cpp/globalhotkeys/win32.c.
AIMP protocol: AIMP SDK Remote Access, also used by NimiGames68/aimp-smtc.
iTunes: its COM automation interface, as used by thewizrd/iTunes-SMTC.
"""
import os
from pathlib import Path
import shutil


def find_executable(player):
    """Only PATH, App Paths and this player's known install locations."""
    for exe in player.exes:
        path = shutil.which(exe)
        if path:
            return path
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(hive, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}',
                                            0, winreg.KEY_READ | view) as key:
                            value = str(winreg.QueryValueEx(key, '')[0]).strip('"')
                            if Path(value).is_file():
                                return value
                    except OSError:
                        pass
        except ImportError:
            pass
    for root in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA', 'APPDATA'):
        if not os.environ.get(root):
            continue
        for relative in player.install_paths:
            path = Path(os.environ[root]) / relative
            if path.is_file():
                return str(path)
    return None


def package_installed(prefix):
    if not prefix:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages') as key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                if winreg.EnumKey(key, index).casefold().startswith(prefix.casefold() + '_'):
                    return True
    except (ImportError, OSError):
        pass
    return False


def installed(player):
    return bool(find_executable(player) or package_installed(player.package))


def player_windows(exes, *, hidden_class=None, title_prefix=None):
    """Resolve by process identity, then window class; never activate a window."""
    import psutil
    import win32gui
    import win32process
    matches = []
    expected = {exe.casefold() for exe in exes}

    def visit(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().casefold() not in expected:
                return True
            if hidden_class:
                if win32gui.GetClassName(hwnd) != hidden_class:
                    return True
                if title_prefix and not win32gui.GetWindowText(hwnd).startswith(title_prefix):
                    return True
            elif not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, 4):
                return True
            matches.append((hwnd, pid))
        except (psutil.Error, OSError):
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return matches


def unique_window(exes, **kwargs):
    windows = player_windows(exes, **kwargs)
    if len(windows) != 1:
        raise RuntimeError('Open one player window before using this command')
    return windows[0][0]


def send_message(hwnd, message, wparam=0, lparam=0):
    """A timed, directed message. Timeout/failure is never retried as a toggle."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    send = user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    if not send(hwnd, message, wparam, lparam, 0x2 | 0x20, 500, ctypes.byref(result)):
        raise RuntimeError('The player did not respond')
    return result.value


def native_transport(player, action):
    if player.driver == 'vlc':
        hwnd = unique_window(player.exes, hidden_class='Static', title_prefix='VLC ghk ')
        vlc_hotkey(hwnd, {'toggle': 'play-pause', 'previous': 'prev'}.get(action, action))
    elif player.driver == 'aimp':
        hwnd = unique_window(player.exes, hidden_class='AIMP2_RemoteInfo')
        if action in ('pause', 'play'):
            state = send_message(hwnd, 0x477, 0x40)
            if action == 'pause' and state != 2 or action == 'play' and state == 2:
                return
        send_message(hwnd, 0x475, {'play': 13, 'toggle': 14, 'pause': 15,
                                  'next': 17, 'previous': 18}[action])
    elif player.driver == 'itunes':
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        client = None
        try:
            # GetActiveObject never creates/launches an iTunes instance.
            client = win32com.client.GetActiveObject('iTunes.Application')
            if action == 'pause' and client.PlayerState == 0:
                return
            if action == 'play' and client.PlayerState == 1:
                return
            method = {'play': 'Play', 'pause': 'Pause', 'toggle': 'PlayPause',
                      'next': 'NextTrack', 'previous': 'PreviousTrack'}[action]
            getattr(client, method)()
        finally:
            client = None
            pythoncom.CoUninitialize()
    else:
        raise RuntimeError('This player has no supported transport driver')


def vlc_hotkey(hwnd, action):
    """VLC's hidden receiver resolves an atom to key-<action> (no focus/keys).

    Use a synchronous message so the receiver consumes the atom before release.
    The native pause action checks the actual input state inside VLC.
    """
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GlobalAddAtomW.argtypes = [ctypes.c_wchar_p]
    kernel32.GlobalAddAtomW.restype = ctypes.c_ushort
    kernel32.GlobalDeleteAtom.argtypes = [ctypes.c_ushort]
    kernel32.GlobalDeleteAtom.restype = ctypes.c_ushort
    atom = kernel32.GlobalAddAtomW(action)
    if not atom:
        raise RuntimeError('VLC command unavailable')
    try:
        if send_message(hwnd, 0x312, atom) != 1:
            raise RuntimeError('VLC did not accept this action')
    finally:
        kernel32.GlobalDeleteAtom(atom)


def launch(player):
    import subprocess
    executable = find_executable(player)
    if executable:
        subprocess.Popen([executable])
    elif player.aumid and package_installed(player.package):
        subprocess.Popen(['explorer.exe', 'shell:AppsFolder\\' + player.aumid])
    else:
        raise RuntimeError('Player installation not found')


def close(player):
    # Graceful close only; never kill a player or all processes sharing a name.
    hwnd = unique_window(player.exes)
    import win32gui
    win32gui.PostMessage(hwnd, 0x10, 0, 0)
