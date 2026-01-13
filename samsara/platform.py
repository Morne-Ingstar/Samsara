"""
Samsara Platform Abstraction Module

Provides cross-platform compatibility for OS-specific operations.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Callable


# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MACOS = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')

PLATFORM_NAME = 'Windows' if IS_WINDOWS else ('macOS' if IS_MACOS else 'Linux')


def get_python_executable(venv_path: Path) -> Path:
    """
    Get the Python executable path for a virtual environment.

    Args:
        venv_path: Path to the virtual environment

    Returns:
        Path to the Python executable
    """
    if IS_WINDOWS:
        return venv_path / "Scripts" / "python.exe"
    else:
        return venv_path / "bin" / "python"


def get_path_separator() -> str:
    """Get the PATH environment variable separator for the current platform."""
    return ';' if IS_WINDOWS else ':'


def open_file_or_folder(path: Path) -> bool:
    """
    Open a file or folder with the system's default application.

    Args:
        path: Path to file or folder

    Returns:
        True if successful
    """
    try:
        path_str = str(path)
        if IS_WINDOWS:
            os.startfile(path_str)
        elif IS_MACOS:
            subprocess.run(['open', path_str], check=True)
        else:  # Linux
            subprocess.run(['xdg-open', path_str], check=True)
        return True
    except Exception:
        return False


def launch_application(target: str) -> bool:
    """
    Launch an application or open a file with the default handler.

    Args:
        target: Application path or file to open

    Returns:
        True if launch was initiated
    """
    try:
        if IS_WINDOWS:
            subprocess.Popen(f'start "" "{target}"', shell=True)
        elif IS_MACOS:
            subprocess.Popen(['open', target])
        else:  # Linux
            subprocess.Popen(['xdg-open', target])
        return True
    except Exception:
        return False


def hide_console_window() -> None:
    """Hide the console window (Windows only, no-op on other platforms)."""
    if not IS_WINDOWS:
        return

    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def show_message_box(title: str, message: str, error: bool = False) -> None:
    """
    Show a message box dialog.

    Args:
        title: Dialog title
        message: Dialog message
        error: If True, show as error dialog
    """
    try:
        if IS_WINDOWS:
            import ctypes
            flags = 0x10 if error else 0x40  # MB_ICONERROR or MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(0, message, title, flags)
        else:
            # Use tkinter as cross-platform fallback
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                if error:
                    messagebox.showerror(title, message)
                else:
                    messagebox.showinfo(title, message)
                root.destroy()
            except ImportError:
                # Last resort: print to console
                print(f"{title}: {message}")
    except Exception:
        print(f"{title}: {message}")


def get_startup_folder() -> Optional[Path]:
    """
    Get the platform-specific startup/autostart folder.

    Returns:
        Path to startup folder, or None if not available
    """
    if IS_WINDOWS:
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            return Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
    elif IS_MACOS:
        return Path.home() / 'Library' / 'LaunchAgents'
    else:  # Linux
        # XDG autostart directory
        config_home = os.environ.get('XDG_CONFIG_HOME', '')
        if config_home:
            return Path(config_home) / 'autostart'
        return Path.home() / '.config' / 'autostart'
    return None


def get_autostart_file_path(app_name: str = "Samsara") -> Optional[Path]:
    """
    Get the path for the autostart file.

    Args:
        app_name: Application name

    Returns:
        Path to autostart file
    """
    startup_folder = get_startup_folder()
    if not startup_folder:
        return None

    if IS_WINDOWS:
        return startup_folder / f'{app_name}.vbs'
    elif IS_MACOS:
        return startup_folder / f'com.{app_name.lower()}.plist'
    else:  # Linux
        return startup_folder / f'{app_name.lower()}.desktop'


def create_autostart_entry(script_path: Path, python_exe: Path, app_name: str = "Samsara") -> bool:
    """
    Create an autostart entry for the application.

    Args:
        script_path: Path to the main script
        python_exe: Path to Python executable
        app_name: Application name

    Returns:
        True if successful
    """
    autostart_file = get_autostart_file_path(app_name)
    if not autostart_file:
        return False

    try:
        autostart_file.parent.mkdir(parents=True, exist_ok=True)

        if IS_WINDOWS:
            # VBS script for Windows
            vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{script_path.parent}"
WshShell.Run """" & "{python_exe}" & """ """ & "{script_path}" & """", 0, False
Set WshShell = Nothing
'''
            autostart_file.write_text(vbs_content)

        elif IS_MACOS:
            # launchd plist for macOS
            plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.{app_name.lower()}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{script_path.parent}</string>
</dict>
</plist>
'''
            autostart_file.write_text(plist_content)

        else:  # Linux
            # .desktop file for Linux
            desktop_content = f'''[Desktop Entry]
Type=Application
Name={app_name}
Exec={python_exe} {script_path}
Path={script_path.parent}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
'''
            autostart_file.write_text(desktop_content)

        return True
    except Exception:
        return False


def remove_autostart_entry(app_name: str = "Samsara") -> bool:
    """
    Remove the autostart entry for the application.

    Args:
        app_name: Application name

    Returns:
        True if successful or file didn't exist
    """
    autostart_file = get_autostart_file_path(app_name)
    if not autostart_file:
        return True

    try:
        if autostart_file.exists():
            autostart_file.unlink()
        return True
    except Exception:
        return False


def is_autostart_enabled(app_name: str = "Samsara") -> bool:
    """
    Check if autostart is enabled for the application.

    Args:
        app_name: Application name

    Returns:
        True if autostart file exists
    """
    autostart_file = get_autostart_file_path(app_name)
    return autostart_file is not None and autostart_file.exists()


def play_sound_fallback(filepath: Path) -> bool:
    """
    Play a sound file using platform-specific fallback methods.

    This is used when sounddevice is not available.

    Args:
        filepath: Path to WAV file

    Returns:
        True if sound was played
    """
    try:
        if IS_WINDOWS:
            import winsound
            winsound.PlaySound(
                str(filepath),
                winsound.SND_FILENAME | winsound.SND_ASYNC
            )
            return True
        elif IS_MACOS:
            subprocess.Popen(['afplay', str(filepath)])
            return True
        else:  # Linux
            # Try various Linux audio players
            for player in ['paplay', 'aplay', 'play']:
                try:
                    subprocess.Popen([player, str(filepath)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    return True
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return False


def get_default_config_dir(app_name: str = "Samsara") -> Path:
    """
    Get the default configuration directory for the application.

    Args:
        app_name: Application name

    Returns:
        Path to configuration directory
    """
    if IS_WINDOWS:
        base = os.environ.get('APPDATA', '')
        if base:
            return Path(base) / app_name
        return Path.home() / app_name
    elif IS_MACOS:
        return Path.home() / 'Library' / 'Application Support' / app_name
    else:  # Linux
        config_home = os.environ.get('XDG_CONFIG_HOME', '')
        if config_home:
            return Path(config_home) / app_name.lower()
        return Path.home() / '.config' / app_name.lower()


def get_keyboard_modifier_key() -> str:
    """
    Get the primary modifier key name for the platform.

    Returns:
        'cmd' on macOS, 'ctrl' on Windows/Linux
    """
    return 'cmd' if IS_MACOS else 'ctrl'
