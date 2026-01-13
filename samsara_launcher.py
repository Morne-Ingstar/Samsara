"""
Samsara Voice Dictation Launcher
A cross-platform launcher that finds Python automatically.
"""
import subprocess
import sys
import os
from pathlib import Path
import shutil

# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MACOS = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')


def show_msg(message, title="Samsara", error=False):
    """Show a message dialog (cross-platform)."""
    if IS_WINDOWS:
        try:
            import ctypes
            flags = 0x10 if error else 0x40
            ctypes.windll.user32.MessageBoxW(0, message, title, flags)
            return
        except Exception:
            pass

    # Try tkinter as cross-platform fallback
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
        return
    except ImportError:
        pass

    # Last resort: print to console
    prefix = "ERROR: " if error else ""
    print(f"{prefix}{title}: {message}")


def show_error(message):
    """Show an error dialog."""
    show_msg(message, "Samsara Launcher Error", error=True)


DEBUG = False  # Set to True for debugging


def get_python_exe_name():
    """Get the Python executable name for the current platform."""
    return "python.exe" if IS_WINDOWS else "python"


def get_venv_bin_dir(venv_path):
    """Get the bin/Scripts directory for a virtual environment."""
    if IS_WINDOWS:
        return venv_path / "Scripts"
    return venv_path / "bin"


def get_path_separator():
    """Get the PATH separator for the current platform."""
    return ";" if IS_WINDOWS else ":"


def find_python(app_dir):
    """
    Find a suitable Python installation. Checks in order:
    1. Local venv in app directory (venv/ or .venv/)
    2. Conda environment named 'sami' or 'samsara'
    3. Python in system PATH

    Returns (python_exe_path, env_dict) or (None, None) if not found.
    """
    path_sep = get_path_separator()
    python_name = get_python_exe_name()

    # 1. Check for local virtual environment in app directory
    local_venv_paths = [
        app_dir / "venv",
        app_dir / ".venv",
        app_dir / "env",
    ]
    for venv_path in local_venv_paths:
        bin_dir = get_venv_bin_dir(venv_path)
        python_exe = bin_dir / python_name
        if python_exe.exists():
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{path_sep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(venv_path)
            if DEBUG:
                show_msg(f"Found local venv:\n{venv_path}")
            return python_exe, env

    # 2. Check for conda environments
    conda_env_names = ["sami", "samsara", "dictation"]
    conda_locations = [
        Path.home() / "miniconda3" / "envs",
        Path.home() / "anaconda3" / "envs",
        Path.home() / "conda" / "envs",
        Path.home() / "miniforge3" / "envs",
        Path.home() / "mambaforge" / "envs",
    ]

    # Add CONDA_PREFIX parent if available
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        conda_locations.append(Path(conda_prefix).parent)

    for conda_base in conda_locations:
        if conda_base is None or not conda_base.exists():
            continue
        for env_name in conda_env_names:
            conda_env = conda_base / env_name
            python_exe = conda_env / python_name
            # On Windows, conda python is in the root of the env
            if not python_exe.exists() and IS_WINDOWS:
                python_exe = conda_env / "python.exe"
            if python_exe.exists():
                env = os.environ.copy()
                if IS_WINDOWS:
                    env["PATH"] = f"{conda_env}{path_sep}{conda_env / 'Scripts'}{path_sep}{conda_env / 'Library' / 'bin'}{path_sep}{env.get('PATH', '')}"
                else:
                    env["PATH"] = f"{conda_env / 'bin'}{path_sep}{env.get('PATH', '')}"
                env["CONDA_PREFIX"] = str(conda_env)
                if DEBUG:
                    show_msg(f"Found conda env:\n{conda_env}")
                return python_exe, env

    # 3. Check system PATH for Python
    python_in_path = shutil.which("python3") or shutil.which("python")
    if python_in_path:
        python_exe = Path(python_in_path)
        env = os.environ.copy()
        if DEBUG:
            show_msg(f"Found Python in PATH:\n{python_exe}")
        return python_exe, env

    return None, None


def check_dependencies(python_exe):
    """Check if required packages are installed."""
    try:
        result = subprocess.run(
            [str(python_exe), "-c", "import faster_whisper; import pystray; import sounddevice"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def launch_app(python_exe, script, env, app_dir):
    """Launch the application (cross-platform)."""
    if IS_WINDOWS:
        # On Windows, use a batch file for better process handling
        if DEBUG:
            batch_file = app_dir / "_run_debug.bat"
            batch_content = f'''@echo off
cd /d "{app_dir}"
echo Starting Samsara...
echo Python: {python_exe}
echo.
"{python_exe}" "{script}"
echo.
echo Process exited with code %ERRORLEVEL%
pause
'''
            batch_file.write_text(batch_content)
            os.startfile(str(batch_file))
            show_msg("Launched in debug mode.\nCheck the console window.")
        else:
            # Launch minimized without console
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            subprocess.Popen(
                [str(python_exe), str(script)],
                env=env,
                cwd=str(app_dir),
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
    else:
        # On macOS/Linux, use nohup for background execution
        if DEBUG:
            # Run in terminal for debugging
            if IS_MACOS:
                subprocess.Popen([
                    'osascript', '-e',
                    f'tell app "Terminal" to do script "cd {app_dir} && {python_exe} {script}"'
                ])
            else:
                # Try common Linux terminals
                terminals = ['gnome-terminal', 'xterm', 'konsole', 'xfce4-terminal']
                for term in terminals:
                    if shutil.which(term):
                        if term == 'gnome-terminal':
                            subprocess.Popen([term, '--', str(python_exe), str(script)], cwd=str(app_dir))
                        else:
                            subprocess.Popen([term, '-e', f'{python_exe} {script}'], cwd=str(app_dir))
                        break
                else:
                    # Fallback: just run in background
                    subprocess.Popen([str(python_exe), str(script)], env=env, cwd=str(app_dir))
            show_msg("Launched in debug mode.")
        else:
            # Background launch
            subprocess.Popen(
                [str(python_exe), str(script)],
                env=env,
                cwd=str(app_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )


def main():
    try:
        # Get app directory
        if getattr(sys, 'frozen', False):
            exe_path = Path(sys.executable).resolve()
            app_dir = exe_path.parent.parent
        else:
            app_dir = Path(__file__).parent.resolve()

        if DEBUG:
            show_msg(f"App directory:\n{app_dir}")

        script = app_dir / "dictation.py"

        if not script.exists():
            show_error(f"Cannot find dictation.py at:\n{script}")
            return 1

        # Find Python
        python_exe, env = find_python(app_dir)

        if python_exe is None:
            if IS_WINDOWS:
                instructions = (
                    "Python not found!\n\n"
                    "Please do one of the following:\n\n"
                    "1. Run install.bat to set up automatically\n\n"
                    "2. Create a virtual environment:\n"
                    "   python -m venv venv\n"
                    "   venv\\Scripts\\pip install -r requirements.txt\n\n"
                    "3. Add Python to your system PATH"
                )
            else:
                instructions = (
                    "Python not found!\n\n"
                    "Please do one of the following:\n\n"
                    "1. Create a virtual environment:\n"
                    "   python3 -m venv venv\n"
                    "   source venv/bin/activate\n"
                    "   pip install -r requirements.txt\n\n"
                    "2. Install Python and add to PATH"
                )
            show_error(instructions)
            return 1

        if DEBUG:
            show_msg(f"Using Python:\n{python_exe}")

        # Launch the app
        launch_app(python_exe, script, env, app_dir)

        return 0

    except Exception as e:
        show_error(f"Unexpected error:\n{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
