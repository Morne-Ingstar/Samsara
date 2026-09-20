"""Windows sign-in startup entry, with registry access kept behind a seam."""

from __future__ import annotations

import os
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Samsara"


def compose_run_command(
    *,
    frozen: bool,
    executable: str,
    script_path: str | None = None,
    pythonw_executable: str | None = None,
    home_dir: str | None = None,
    comspec: str = r"C:\Windows\System32\cmd.exe",
) -> str:
    """Compose the per-user Run value without touching the registry."""
    if frozen:
        app = str(executable)
        if not home_dir:
            return f'"{app}"'
        # The Run value has no environment field. Set the inherited home in a
        # short-lived command shell, then start the GUI executable without a
        # second console window.
        inner = f'set "SAMSARA_HOME_DIR={home_dir}" && start "" /b "{app}"'
        return f'"{comspec}" /d /s /c "{inner}"'

    if not pythonw_executable or not script_path:
        raise ValueError("source startup needs pythonw_executable and script_path")
    return f'"{pythonw_executable}" "{script_path}"'


def current_run_command() -> str:
    """Build the command for this process's source or frozen installation."""
    executable = sys.executable
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return compose_run_command(
            frozen=True,
            executable=executable,
            home_dir=os.environ.get("SAMSARA_HOME_DIR"),
            comspec=os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
        )

    executable_path = Path(executable)
    pythonw = executable_path.with_name("pythonw.exe")
    script = Path(__file__).resolve().parent.parent / "dictation.py"
    return compose_run_command(
        frozen=False,
        executable=executable,
        pythonw_executable=str(pythonw),
        script_path=str(script),
    )


def should_show_main_window(config) -> bool:
    """Keep the shell visible until the first-run wizard is complete."""
    wizard_pending = (
        not bool(config.get("first_run_complete", False))
        or config.get("microphone") is None
    )
    return wizard_pending or not bool(config.get("start_hidden", False))


def sync_run_entry(enabled: bool, *, command: str | None = None, registry=None) -> None:
    """Create/update or remove the current-user Run value.

    ``registry`` is an injectable winreg-compatible module for tests. With no
    module passed, this is the only function that imports the Windows API.
    """
    if registry is None:
        try:
            import winreg as registry
        except ImportError as exc:  # pragma: no cover - exercised on Windows
            raise OSError("Windows startup registry is unavailable") from exc

    if enabled:
        value = command if command is not None else current_run_command()
        with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
            registry.SetValueEx(key, RUN_VALUE_NAME, 0, registry.REG_SZ, value)
        return

    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE
        ) as key:
            registry.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        # Removing an already absent value is an idempotent success.
        return
