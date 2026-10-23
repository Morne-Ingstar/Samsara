"""Cheap, once-per-fingerprint integration checks; no inference or playback.

Call schedule_on_startup(app) after startup completes. Importing this module
does no I/O. Services without version discovery use their client/API contract
as a fingerprint: a server-only change is not detectable in that case.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)
_lock = threading.Lock()
STATE_FILE = "integration_health.json"
TIMEOUT = 4


class CheckFailure(Exception):
    """A fixed, public diagnostic, never arbitrary subprocess/server output."""


@dataclass(frozen=True)
class Integration:
    key: str
    name: str
    fingerprint: Callable[[], object]
    probe: Callable[[], None]


@dataclass(frozen=True)
class Notice:
    integration: str
    reason: str

    @property
    def text(self) -> str:
        return f"{self.integration} changed and Samsara can't talk to it: {self.reason}"


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def run_checks(home: Path, integrations: list[Integration]) -> list[Notice]:
    """Probe first install/changed versions, including a changed failure state.

    Persist only hashes and success booleans, never tokens, server replies,
    paths, model names or exception text. A failed fingerprint is re-read on
    the next boot so recovery triggers a fresh probe, without repeating alerts.
    """
    notices = []
    with _lock:
        path = Path(home) / STATE_FILE
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema") != 1:
                state = {}
        except (OSError, ValueError):
            state = {}
        previous = state.get("integrations", {})
        if not isinstance(previous, dict):
            previous = {}
        current = {}
        for item in integrations:
            if item.key in current:
                continue
            reason = None
            try:
                fingerprint = _hash(item.fingerprint())
            except Exception as exc:
                fingerprint = _hash(["unavailable", type(exc).__name__])
                reason = "version check unavailable"
            old = previous.get(item.key)
            if isinstance(old, dict) and old.get("fingerprint") == fingerprint:
                current[item.key] = old
                logger.info("[INTEGRATION-HEALTH] %s unchanged", item.key)
                continue
            if reason is None:
                try:
                    item.probe()
                except CheckFailure as exc:
                    reason = str(exc)
                except Exception:
                    reason = "compatibility check unavailable"
            current[item.key] = {"fingerprint": fingerprint, "ok": reason is None}
            logger.info("[INTEGRATION-HEALTH] %s %s", item.key,
                        "failed" if reason else "ok")
            if reason:
                notices.append(Notice(item.name, reason))
        if current != previous or not path.is_file():
            temporary = path.with_suffix(".tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps({"schema": 1, "integrations": current}),
                                     encoding="utf-8")
                os.replace(temporary, path)
            except OSError:
                logger.warning("[INTEGRATION-HEALTH] state could not be saved")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return notices


def _cli(argv: list[str]) -> str:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=TIMEOUT, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise CheckFailure("CLI metadata check failed")
    return result.stdout[:65536]


def _require_flags(help_text: str, flags) -> None:
    listed = set(re.findall(r"(?<![\w-])--?[A-Za-z][\w-]*", help_text))
    missing = sorted(set(flags) - listed)
    if missing:
        raise CheckFailure("CLI help is missing " + ", ".join(missing))


def hermes_integration(config: dict) -> Integration:
    from samsara.agent_channel.hermes import hermes_argv

    settings = config.get("hermes") or {}

    def executable():
        exe = shutil.which("hermes")
        if not exe:
            raise CheckFailure("Hermes CLI is not installed")
        return [exe]

    def shape():
        # Include resume, model, profile and effort even on the first turn.
        # Derive flags from Samsara's actual builder, including future changes.
        values = dict(settings)
        for key, fallback in (("profile", "default"), ("model", "default"), ("effort", "low")):
            values[key] = values.get(key) or fallback
        return hermes_argv(executable(), "health804", values, TIMEOUT)

    def fingerprint():
        return [_cli([*executable(), "--version"]), shape()]

    def probe():
        argv = shape()
        split = argv.index("chat")
        _require_flags(_cli([*executable(), "--help"]),
                       [arg for arg in argv[len(executable()):split] if arg.startswith("-")])
        _require_flags(_cli([*executable(), "chat", "--help"]),
                       [arg for arg in argv[split + 1:] if arg.startswith("-") and arg != "-"])

    return Integration("hermes", "Hermes", fingerprint, probe)


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _ollama_json(host: str, endpoint: str):
    from samsara import ai_transport
    response = ai_transport.get_local(host.rstrip("/") + endpoint, TIMEOUT, ollama_host=host)
    try:
        response.raise_for_status()
        return response.json()
    finally:
        response.close()


def ollama_integration(host: str, models: list[str]) -> Integration:
    models = sorted(set(models))

    def fingerprint():
        result = _ollama_json(host, "/api/version")
        version = result.get("version")
        if not isinstance(version, str) or not version:
            raise CheckFailure("Ollama returned no version")
        return [host, version, models]

    def probe():
        result = _ollama_json(host, "/api/tags")
        available = {row.get("name") for row in result.get("models", []) if isinstance(row, dict)}
        if any(model not in available and model + ":latest" not in available for model in models):
            raise CheckFailure("a configured Ollama model is not installed")

    return Integration("ollama", "Ollama", fingerprint, probe)


def edge_integration(voice: str) -> Integration:
    def probe():
        import edge_tts

        async def check():
            voices = await asyncio.wait_for(edge_tts.list_voices(), TIMEOUT)
            if not any(row.get("ShortName") == voice for row in voices):
                raise CheckFailure("the configured online voice is unavailable")
        asyncio.run(check())

    return Integration("edge", "Online voice", lambda: [_package("edge-tts"), voice], probe)


def todoist_integration(token: str) -> Integration:
    def probe():
        # The same read-only API path as Samsara's Today command. Do not call
        # today(), which returns task text; inspect the response shape only.
        from samsara import todoist_client
        result = todoist_client._request(token, todoist_client.FILTER_URL + "?query=today")
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise CheckFailure("Todoist's task-list response is incompatible")

    return Integration("todoist", "Todoist",
                       lambda: ["api/v1/tasks/filter", sys.version_info[:3], _hash(token)], probe)


def cli_integration(name: str) -> Integration:
    from samsara.agent_channel import cli_agents

    def executable():
        return cli_agents.resolve_executable(name)

    def shape():
        if name == "gpt":
            return cli_agents.codex_argv(executable(), "", None, Path("."), False)
        return cli_agents.claude_argv(executable(), "", None, Path("."), False)

    def probe():
        argv = shape()
        if name == "gpt":
            root = _cli([*executable(), "exec", "--help"])
            resumed = cli_agents.codex_argv(executable(), "", "health804", Path("."), False)
            _require_flags(root, [arg for arg in argv[len(executable()) + 1:] if arg.startswith("-") and arg != "-"])
            _require_flags(_cli([*executable(), "exec", "resume", "--help"]),
                           [arg for arg in resumed[len(executable()) + 2:] if arg.startswith("-") and arg != "-"])
        else:
            resumed = cli_agents.claude_argv(executable(), "", "health804", Path("."), False)
            _require_flags(_cli([*executable(), "--help"]),
                           [arg for arg in resumed[len(executable()):] if arg.startswith("-")])

    return Integration(name, "Codex" if name == "gpt" else "Claude",
                       lambda: [_cli([*executable(), "--version"]), shape()], probe)


def spotify_integration(executable: str) -> Integration:
    def fingerprint():
        stat = Path(executable).stat()
        return [stat.st_size, stat.st_mtime_ns, _package("winsdk")]

    def probe():
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        async def check():
            manager = await asyncio.wait_for(
                GlobalSystemMediaTransportControlsSessionManager.request_async(), TIMEOUT)
            for session in manager.get_sessions():
                if "spotify" in session.source_app_user_model_id.casefold():
                    session.get_playback_info()  # Read only; never play/pause.
        asyncio.run(check())

    return Integration("spotify", "Spotify", fingerprint, probe)


def enabled_integrations(config: dict) -> list[Integration]:
    from samsara import ai_preferences
    from samsara.config_defaults import cfg_get
    from samsara.agent_channel.hermes import permitted

    result = []
    if permitted(config):
        result.append(hermes_integration(config))
    models = []
    for feature, section, key in (("ava", "ollama", "model"),
                                  ("ava", "ava_command_session", "model"),
                                  ("editing", "ava_edit", "model")):
        state = ai_preferences.runtime_state(config, feature, provider_route="ollama")
        if state.allowed:
            models.append(cfg_get(config, section + "." + key) or cfg_get(config, "ollama.model"))
    if ((config.get("smart_corrections") or {}).get("enabled")
            and ai_preferences.runtime_state(config, "editing", provider_route="ollama").allowed):
        models.append(cfg_get(config, "smart_corrections.ollama_model"))
    if models:
        result.append(ollama_integration(cfg_get(config, "ollama.host"), models))
    if (cfg_get(config, "tts.enabled") and cfg_get(config, "tts.engine") == "edge"
            and cfg_get(config, "tts.network_voice_accepted")):
        result.append(edge_integration(cfg_get(config, "tts.voice_id")))
    tasks = (config.get("tasks") or {}).get("todoist") or {}
    if tasks.get("enabled") and tasks.get("api_token"):
        result.append(todoist_integration(tasks["api_token"]))
    if (config.get("command_packs") or {}).get("core", True):
        for name, command in (("gpt", "codex.cmd"), ("claude", "claude.cmd")):
            if shutil.which(command):
                result.append(cli_integration(name))
    if (config.get("command_packs") or {}).get("music", True):
        try:
            import psutil
            for process in psutil.process_iter(["name", "exe"]):
                if (process.info.get("name") or "").casefold() == "spotify.exe" and process.info.get("exe"):
                    result.append(spotify_integration(process.info["exe"]))
                    break
        except ImportError:
            logger.debug("[INTEGRATION-HEALTH] Spotify discovery unavailable")
        except (OSError, psutil.Error):
            logger.debug("[INTEGRATION-HEALTH] Spotify discovery unavailable")
    return result


def schedule_on_startup(app):
    """Schedule once after startup; all metadata I/O stays on a worker.

    The caller owns the startup seam. Normal outcome chips reach both Compact
    and Full. 803 is absent at this base, so there is no Report action here.
    """
    if getattr(app, "_integration_health_scheduled", False):
        return
    app._integration_health_scheduled = True
    config = deepcopy(app.config)

    def work():
        try:
            from samsara.paths import samsara_home_dir
            notices = run_checks(samsara_home_dir(), enabled_integrations(config))
            if not notices:
                return

            def deliver(index=0):
                from PySide6.QtCore import QTimer
                app._show_outcome_chip(notices[index].text, "warning")
                if index + 1 < len(notices):
                    QTimer.singleShot(8500, lambda: deliver(index + 1))
            app._schedule_ui(deliver)
        except Exception:
            logger.warning("[INTEGRATION-HEALTH] startup checks unavailable")

    from samsara.runtime import thread_registry
    return thread_registry.spawn("integration_health.startup", work, daemon=True)
