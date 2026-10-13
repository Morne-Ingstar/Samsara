"""Optional Hermes JSONL channel. Only fixed/validated arguments enter argv."""
from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cli_agents as cli
from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
SESSION = re.compile(r"[0-9A-Za-z_\-]{6,64}\Z")
SETTING = re.compile(r"[\w./:\-]{1,80}\Z", re.ASCII)
# PLACEHOLDER COPY (539): reviewed together with Docs/hermes/samsara-voice/SKILL.md and notice.
PREAMBLE = (
    "The user is speaking through Samsara voice dictation; words may be misheard. "
    "If a name or a destructive instruction is ambiguous, ask before acting. "
    "Your reply may be read aloud: lead with the answer in one or two short sentences, "
    "with no tables or code blocks in the first paragraph. Never ask the user to type or click."
)
logger = logging.getLogger(__name__)


def installed() -> bool:
    return bool(shutil.which("hermes"))


def permitted(config: dict) -> bool:
    return ((config.get("command_packs") or {}).get("hermes", False) is True
            and (config.get("hermes") or {}).get("notice_accepted", False) is True)


def validated_settings(config: dict) -> dict:
    values = {key: str(config.get(key) or "").strip() for key in ("profile", "model", "effort")}
    for key in ("profile", "model"):
        if values[key] and (values[key].startswith("-") or not SETTING.fullmatch(values[key])):
            raise ValueError(f"Hermes {key} has invalid characters.")
    if values["effort"] and values["effort"] not in EFFORTS:
        raise ValueError("Hermes effort must be one of the listed levels.")
    return values


def hermes_argv(exe: list[str], session: str | None, settings: dict, timeout: float) -> list[str]:
    values = validated_settings(settings)
    argv = list(exe)
    if values["profile"]:
        argv += ["-p", values["profile"]]
    argv += ["chat", "--source", "samsara", "--format", "stream-json", "--query-file", "-",
             "--run-budget", str(max(1, int(timeout)))]
    if session:
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise ValueError("Hermes session ID is invalid. Start a new Hermes session.")
        argv += ["--resume", session]
    if values["model"]:
        argv += ["-m", values["model"]]
    if values["effort"]:
        argv += ["--reasoning", values["effort"]]
    return argv


def parse_hermes(lines: list[str], errors: list[str] = ()) -> tuple[str, str | None]:
    texts, system_id, result = [], None, None
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system":
            system_id = event.get("session_id") or system_id
        elif event.get("type") == "text" and isinstance(event.get("text"), str):
            texts.append(event["text"])
        elif event.get("type") == "result":
            result = event
    # A result event is the completion contract; text chunks alone are incomplete.
    if result is None or result.get("exit_code", 0):
        raise RuntimeError(first_error(errors, "Hermes returned no completed result."))
    session = result.get("session_id") or system_id
    if not session:
        session = next((line.partition(":")[2].strip() for line in errors
                        if line.strip().startswith("session_id:")), None)
    if session and (not isinstance(session, str) or not SESSION.fullmatch(session)):
        raise RuntimeError("Hermes returned an invalid session ID.")
    answer = result.get("text")
    if not isinstance(answer, str) or not answer.strip():
        answer = "".join(texts)
    if not answer.strip():
        raise RuntimeError(first_error(errors, "Hermes returned no answer."))
    return answer, session


def first_error(errors, fallback):
    return next((line.strip()[:180] for line in errors if line.strip()
                 and not line.strip().startswith("session_id:")
                 and "Resumed session " not in line), fallback)


class BusyError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


@dataclass
class Turn:
    generation: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    process: object = None


class HermesAgent(cli.CliAgent):
    def __init__(self, executable=None):
        super().__init__("hermes", hermes_argv, parse_hermes, executable)
        self._state_lock = threading.RLock()
        self._generation = 0
        self._turn = None
        self._closed = False
        thread_registry.on_shutdown(self.shutdown)

    def begin(self) -> Turn:
        with self._state_lock:
            if self._closed or cli._closing.is_set():
                raise Cancelled("Samsara is shutting down")
            if self._turn is not None:
                raise BusyError("Hermes is still working on the last one.")
            self._turn = Turn(self._generation)
            return self._turn

    def valid(self, turn: Turn) -> bool:
        with self._state_lock:
            return not self._closed and not turn.cancelled.is_set() and turn.generation == self._generation

    def finish(self, turn: Turn):
        with self._state_lock:
            if self._turn is turn:
                self._turn = None

    def stop(self):
        with self._state_lock:
            self._generation += 1
            turn = self._turn
            if turn is not None:
                turn.cancelled.set()
                if turn.process is not None:
                    cli._kill_tree(turn.process)

    def shutdown(self):
        self.stop()
        with self._state_lock:
            self._closed = True

    def new_conversation(self, home: Path | None = None):
        with self._state_lock:
            self.stop()
            with cli._sessions_lock:
                home = home or samsara_home_dir()
                data = self._sessions(home)
                data.pop(self.name, None)
                self._save_sessions(home, data)

    def _remember_session(self, session, home, turn):
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            return
        with self._state_lock:
            if not self.valid(turn):
                return
            with cli._sessions_lock:
                data = self._sessions(home)
                data[self.name] = session
                self._save_sessions(home, data)

    def ask(self, question: str, config=None, *, home=None, progress=None, turn=None):
        turn = turn or self.begin()
        process = None
        started = time.monotonic()
        try:
            config = config or {}
            if not permitted(config):
                raise RuntimeError("Enable Hermes in Settings and accept its notice first.")
            channel = config.get("agent_channel") or {}
            settings = config.get("hermes") or {}
            home = home or samsara_home_dir()
            cwd = Path(channel.get("cwd") or cli.DEFAULT_CWD)
            timeout = max(.01, float(channel.get("timeout_s", 240)))
            if not cwd.is_dir():
                raise RuntimeError("Hermes working directory is unavailable.")
            prompt = f"{settings.get('voice_preamble') or PREAMBLE}\n\n{question}"
            with self._state_lock:
                if not self.valid(turn):
                    raise Cancelled()
                with cli._sessions_lock:
                    session = self._sessions(home).get(self.name)
                exe = self.executable or [shutil.which("hermes")]
                if not exe[0]:
                    raise FileNotFoundError("hermes")
                argv = hermes_argv(exe, session, settings, timeout)
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                turn.process = process
                with cli._active_lock:
                    cli._active.add(process)
            events = queue.Queue()

            def drain(stream, source):
                try:
                    for line in stream:
                        events.put((source, line.rstrip("\r\n")))
                finally:
                    events.put((source, None))

            thread_registry.spawn("hermes.stdout", drain, args=(process.stdout, "out"))
            thread_registry.spawn("hermes.stderr", drain, args=(process.stderr, "err"))
            # Write on a daemon too: a stuck/large stdin pipe remains cancellable.
            def send():
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except (OSError, ValueError):
                    events.put(("send", "Hermes could not receive the message."))
            thread_registry.spawn("hermes.stdin", send)
            output, errors, closed = [], [], set()
            system_id = None
            while len(closed) < 2:
                if not self.valid(turn):
                    raise Cancelled()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError()
                try:
                    source, line = events.get(timeout=min(.05, remaining))
                except queue.Empty:
                    continue
                if source == "send":
                    raise RuntimeError(line)
                if line is None:
                    closed.add(source)
                elif source == "out":
                    output.append(line)
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(event, dict) and event.get("type") == "system":
                        system_id = event.get("session_id") or system_id
                        self._remember_session(system_id, home, turn)
                else:
                    errors.append(line)
                    if system_id is None and line.strip().startswith("session_id:"):
                        self._remember_session(line.partition(":")[2].strip(), home, turn)
            code = process.wait(timeout=max(.01, timeout - (time.monotonic() - started)))
            if code:
                raise RuntimeError(first_error(errors, f"Hermes exited with code {code}."))
            answer, new_session = parse_hermes(output, errors)
            with self._state_lock:
                if not self.valid(turn):
                    raise Cancelled()
                if new_session:
                    with cli._sessions_lock:
                        data = self._sessions(home)
                        data[self.name] = new_session
                        self._save_sessions(home, data)
            elapsed = time.monotonic() - started
            logger.info("Hermes completed input_chars=%d output_chars=%d session=%s elapsed_s=%.2f exit=%d",
                        len(question), len(answer), new_session or session, elapsed, code)
            return cli.AgentReply(answer, new_session or session, tuple(argv), elapsed)
        except subprocess.TimeoutExpired:
            raise TimeoutError() from None
        finally:
            if process is not None:
                cli._kill_tree(process)
                with cli._active_lock:
                    cli._active.discard(process)
            self.finish(turn)
