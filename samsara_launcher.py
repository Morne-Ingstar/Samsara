"""
Samsara Voice Dictation Launcher
A portable launcher that finds Python automatically.
"""
import subprocess
import sys
import os
from pathlib import Path
import shutil


def show_msg(message, title="Samsara", error=False):
    """Show a message dialog."""
    import ctypes
    flags = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(0, message, title, flags)


def show_error(message):
    """Show an error dialog."""
    show_msg(message, "Samsara Launcher Error", error=True)


DEBUG = False  # Set to True for debugging


def find_python(app_dir):
    """
    Find a suitable Python installation. Checks in order:
    1. Local venv in app directory (venv/ or .venv/)
    2. Conda environment named 'sami' or 'samsara'
    3. Python in system PATH

    Returns (python_exe_path, env_dict) or (None, None) if not found.
    """

    # 1. Check for local virtual environment in app directory
    local_venv_paths = [
        app_dir / "venv",
        app_dir / ".venv",
        app_dir / "env",
    ]
    for venv_path in local_venv_paths:
        python_exe = venv_path / "Scripts" / "python.exe"
        if python_exe.exists():
            env = os.environ.copy()
            env["PATH"] = f"{venv_path / 'Scripts'};{env.get('PATH', '')}"
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
        Path(os.environ.get("CONDA_PREFIX", "")).parent if os.environ.get("CONDA_PREFIX") else None,
    ]

    for conda_base in conda_locations:
        if conda_base is None or not conda_base.exists():
            continue
        for env_name in conda_env_names:
            conda_env = conda_base / env_name
            python_exe = conda_env / "python.exe"
            if python_exe.exists():
                env = os.environ.copy()
                env["PATH"] = f"{conda_env};{conda_env / 'Scripts'};{conda_env / 'Library' / 'bin'};{env.get('PATH', '')}"
                env["CONDA_PREFIX"] = str(conda_env)
                if DEBUG:
                    show_msg(f"Found conda env:\n{conda_env}")
                return python_exe, env

    # 3. Check system PATH for Python
    python_in_path = shutil.which("python")
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
    except:
        return False


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
            show_error(
                "Python not found!\n\n"
                "Please do one of the following:\n\n"
                "1. Run install.bat to set up automatically\n\n"
                "2. Create a virtual environment:\n"
                "   python -m venv venv\n"
                "   venv\\Scripts\\pip install -r requirements.txt\n\n"
                "3. Add Python to your system PATH"
            )
            return 1

        if DEBUG:
            show_msg(f"Using Python:\n{python_exe}")

        # Optional: Check dependencies (can be slow, so disabled by default)
        # if not check_dependencies(python_exe):
        #     show_error("Missing dependencies. Run:\npip install -r requirements.txt")
        #     return 1

        # Launch the app
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
            batch_file = app_dir / "_launch.bat"
            batch_content = f'''@echo off
cd /d "{app_dir}"
start /min "" "{python_exe}" "{script}"
'''
            batch_file.write_text(batch_content)
            os.startfile(str(batch_file))

        return 0

    except Exception as e:
        show_error(f"Unexpected error:\n{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
