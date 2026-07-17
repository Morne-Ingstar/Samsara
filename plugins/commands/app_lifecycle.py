"""Voice controls for Morne's local project servers.

The launch command stays in each project's existing batch/CMD file.  Samsara
only owns process discovery and stop/restart orchestration, which keeps the
voice commands useful without duplicating project-specific startup details.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

import psutil

from samsara.log import get_logger
from samsara.plugin_commands import command
from samsara.runtime import thread_registry


logger = get_logger(__name__)


class AppSpec(NamedTuple):
    display_name: str
    project_dir: Path
    launchers: tuple[Path, ...]
    process_scripts: tuple[Path, ...]
    restart_launchers: tuple[Path, ...] = ()
    process_executables: tuple[str, ...] = ()


_HOME = Path.home()
_PROJECTS = _HOME / "Projects"
_DESKTOP = _HOME / "Desktop"

_APPS = {
    "strata": AppSpec(
        display_name="Strata",
        project_dir=_PROJECTS / "Strata",
        launchers=(_PROJECTS / "Strata" / "start_strata.bat",),
        process_scripts=(_PROJECTS / "Strata" / "server.py",),
    ),
    "ariadne": AppSpec(
        display_name="Ariadne",
        project_dir=_PROJECTS / "Ariadne",
        launchers=(
            _DESKTOP / "Start Ariadne.cmd",
            _PROJECTS / "Ariadne" / "Start Ariadne.cmd",
        ),
        restart_launchers=(
            _DESKTOP / "Restart Ariadne.cmd",
            _PROJECTS / "Ariadne" / "Restart Ariadne.cmd",
        ),
        process_scripts=tuple(
            _PROJECTS / "Ariadne" / name
            for name in (
                "start.py",
                "media_server.py",
                "stremio_addon.py",
                "watch_monitor.py",
            )
        ),
    ),
    "sigil": AppSpec(
        display_name="Sigil server",
        project_dir=_PROJECTS / "Sigil",
        launchers=(_PROJECTS / "Sigil" / "sigil.bat",),
        process_scripts=(_PROJECTS / "Sigil" / "sigil_server.py",),
    ),
}

_operation_lock = threading.Lock()


def _speak(app, text: str) -> None:
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None:
        return
    try:
        coordinator.speak(text, category="agent_response", interruptible=False)
    except Exception:
        logger.debug("Could not speak app lifecycle status", exc_info=True)


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _normalize_command_arg(value) -> str:
    text = str(value).strip().strip('"')
    return os.path.normcase(os.path.normpath(text))


def _normalize_process_name(value: str) -> str:
    return os.path.normcase(str(value).strip().casefold())


def _is_python_process(proc) -> bool:
    try:
        name = _normalize_process_name(proc.name())
        stem, dot, ext = name.rpartition(".")
        executable = stem if dot else name
        return (
            executable in {"python", "pythonw", "py", "pyw"}
            or (executable.startswith("python") and dot == ".")
            or executable.startswith("python")
        )
    except (psutil.Error, OSError):
        return False


def _matches_spec(proc, spec: AppSpec) -> bool:
    """Match only Python commands owned by one configured project."""
    if proc.pid == os.getpid():
        return False
    try:
        process_name = _normalize_process_name(proc.name())
    except (psutil.Error, OSError):
        return False

    is_python = _is_python_process(proc)
    if not is_python:
        if not spec.process_executables:
            return False
        normalized_executables = {_normalize_process_name(name) for name in spec.process_executables}
        if process_name not in normalized_executables:
            return False
        try:
            cwd = Path(proc.cwd()).resolve()
            return cwd == spec.project_dir.resolve()
        except (psutil.Error, OSError):
            return False
        return False

    try:
        args = [_normalize_command_arg(part) for part in (proc.cmdline() or [])]
    except (psutil.Error, OSError):
        return False

    command_line = " ".join(args).casefold()
    for script in spec.process_scripts:
        if _normalize_command_arg(script) in command_line:
            return True

    # Strata's batch file deliberately invokes ``python server.py`` with a
    # relative path.  Require both the project cwd and an exact script token so
    # another unrelated server.py is never stopped.
    script_names = {path.name.casefold() for path in spec.process_scripts}
    arg_names = {Path(arg).name.casefold() for arg in args[1:]}
    if not (script_names & arg_names):
        return False
    try:
        cwd = Path(proc.cwd()).resolve()
        return cwd == spec.project_dir.resolve()
    except (psutil.Error, OSError):
        return False


def _find_processes(spec: AppSpec) -> list:
    matches = []
    for proc in psutil.process_iter():
        try:
            if _matches_spec(proc, spec):
                matches.append(proc)
        except (psutil.Error, OSError):
            continue
    return matches


def _stop(spec: AppSpec) -> int:
    processes = _find_processes(spec)
    for proc in processes:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass

    _, survivors = psutil.wait_procs(processes, timeout=3.0)
    for proc in survivors:
        try:
            proc.kill()
        except (psutil.Error, OSError):
            pass
    if survivors:
        psutil.wait_procs(survivors, timeout=2.0)
    return len(processes)


def _launch(spec: AppSpec, *, restart: bool = False) -> Path:
    candidates = spec.restart_launchers if restart and spec.restart_launchers else spec.launchers
    launcher = _first_existing(candidates)
    if launcher is None:
        raise FileNotFoundError(
            f"No launcher found for {spec.display_name}: "
            + ", ".join(str(path) for path in candidates)
        )

    flags = 0
    command = []
    if sys.platform == "win32":
        # Preserve the launchers' normal visible live-log window and detach it
        # from Samsara so it survives a later Samsara restart.
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP

    if launcher.suffix.lower() in {".bat", ".cmd"}:
        launcher_arg = subprocess.list2cmdline([str(launcher)])
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", launcher_arg]
    else:
        command = [str(launcher)]

    subprocess.Popen(
        command,
        cwd=str(spec.project_dir),
        creationflags=flags,
        close_fds=True,
    )
    return launcher


def _run_action(app, app_key: str, action: str) -> None:
    spec = _APPS[app_key]
    if not _operation_lock.acquire(blocking=False):
        _speak(app, "Another app command is still running.")
        return
    try:
        if action == "start":
            if _find_processes(spec):
                _speak(app, f"{spec.display_name} is already running.")
                return
            _launch(spec)
            _speak(app, f"Starting {spec.display_name}.")
            return

        if action == "stop":
            count = _stop(spec)
            if count:
                _speak(app, f"Stopped {spec.display_name}.")
            else:
                _speak(app, f"{spec.display_name} was not running.")
            return

        if spec.restart_launchers:
            _launch(spec, restart=True)
        else:
            _stop(spec)
            time.sleep(0.4)
            _launch(spec)
        _speak(app, f"Restarting {spec.display_name}.")
    except Exception:
        logger.exception("%s %s failed", action, spec.display_name)
        _speak(app, f"I could not {action} {spec.display_name}.")
    finally:
        _operation_lock.release()


def _dispatch(app, app_key: str, action: str, remainder: str) -> bool:
    # The plugin matcher can locate phrases inside a longer sentence.  Process
    # controls are intentionally exact-utterance-only so "I restarted Strata"
    # remains ordinary dictation.
    if remainder.strip():
        return False
    thread_registry.spawn(
        f"app_lifecycle.{action}_{app_key}",
        _run_action,
        args=(app, app_key, action),
        daemon=True,
    )
    return True


_COMMAND_META = {
    "pack": "utilities",
    "ai_visible": False,
    "risk_class": "reversible",
    "side_effects": ["launch", "system"],
    "reversible": True,
}


@command("start strata", aliases=["launch strata"], **_COMMAND_META)
def start_strata(app, remainder="", **kwargs):
    return _dispatch(app, "strata", "start", remainder)


@command("stop strata", **_COMMAND_META)
def stop_strata(app, remainder="", **kwargs):
    return _dispatch(app, "strata", "stop", remainder)


@command("restart strata", **_COMMAND_META)
def restart_strata(app, remainder="", **kwargs):
    return _dispatch(app, "strata", "restart", remainder)


@command("start ariadne", aliases=["launch ariadne"], **_COMMAND_META)
def start_ariadne(app, remainder="", **kwargs):
    return _dispatch(app, "ariadne", "start", remainder)


@command("stop ariadne", **_COMMAND_META)
def stop_ariadne(app, remainder="", **kwargs):
    return _dispatch(app, "ariadne", "stop", remainder)


@command("restart ariadne", **_COMMAND_META)
def restart_ariadne(app, remainder="", **kwargs):
    return _dispatch(app, "ariadne", "restart", remainder)


@command("start sigil server", aliases=["start sigil", "launch sigil server"], **_COMMAND_META)
def start_sigil(app, remainder="", **kwargs):
    return _dispatch(app, "sigil", "start", remainder)


@command("stop sigil server", aliases=["stop sigil"], **_COMMAND_META)
def stop_sigil(app, remainder="", **kwargs):
    return _dispatch(app, "sigil", "stop", remainder)


@command("restart sigil server", aliases=["restart sigil"], **_COMMAND_META)
def restart_sigil(app, remainder="", **kwargs):
    return _dispatch(app, "sigil", "restart", remainder)
