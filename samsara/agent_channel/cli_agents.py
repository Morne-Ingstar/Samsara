"""Headless subscription CLI conversations, with bounded child processes."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from samsara.agent_channel.claude_reply import spoken_form
from samsara.agent_channel.spoken_digest import first_reply
from samsara.config_defaults import DEFAULTS
from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

DEFAULT_CWD = Path.home()
DEFAULT_PREAMBLE = ("This message was spoken aloud and your reply will be read aloud. "
                    "Answer in plain spoken sentences first; put any code, paths or lists after that.")
_active: set[subprocess.Popen] = set()
_active_lock = threading.Lock()
_sessions_lock = threading.Lock()
_turn_locks = {"gpt": threading.Lock(), "claude": threading.Lock()}
_closing = threading.Event()


def resolve_executable(name: str) -> list[str]:
    """Follow the installed npm shim to its direct target (no cmd shell quoting)."""
    shim = shutil.which(("codex" if name == "gpt" else name) + ".cmd")
    if not shim:
        raise FileNotFoundError(name)
    root = Path(shim).parent
    if name == "claude":
        target = root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        argv = [str(target)]
    elif name == "gpt":
        target = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = root / "node.exe"
        argv = [str(node if node.is_file() else shutil.which("node.exe") or "node.exe"), str(target)]
    else:
        raise ValueError(name)
    if not target.is_file():
        raise FileNotFoundError(target)
    return argv


def codex_argv(exe: list[str], prompt: str, session: str | None, cwd: Path,
               allow_writes: bool) -> list[str]:
    sandbox = "workspace-write" if allow_writes else "read-only"
    isolation = ["-c", "project_doc_max_bytes=0"]
    if session:
        # resume does not expose -s or -C; --config carries the same sandbox.
        return [*exe, "exec", "resume", "--json", "--skip-git-repo-check",
                *isolation, "-c", f"sandbox_mode={sandbox}", session, "-"]
    return [*exe, "exec", "--json", "--skip-git-repo-check", "-C", str(cwd),
            *isolation, "--sandbox", sandbox, "-"]


def claude_argv(exe: list[str], prompt: str, session: str | None, cwd: Path,
                allow_writes: bool) -> list[str]:
    # Unlike --bare, safe mode retains subscription authentication.
    argv = [*exe, "-p", "--safe-mode", "--output-format", "stream-json", "--verbose"]
    if not allow_writes:
        argv += ["--restricted", "--tools", "Read,Glob,Grep,WebSearch",
                 "--permission-mode", "plan"]
    if session:
        argv += ["--resume", session]
    return [*argv, prompt]


def parse_codex(lines: list[str]) -> tuple[str, str | None]:
    session = None
    answer = ""
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            session = event.get("thread_id") or session
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "")
    return answer, session


def parse_claude(lines: list[str]) -> tuple[str, str | None]:
    session = None
    answer = ""
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        session = event.get("session_id") or session
        if event.get("type") == "result":
            answer = event.get("result", "")
            if event.get("is_error"):
                raise RuntimeError(answer or "Claude failed")
    return answer, session


def clean_reply(text: str) -> str:
    """Remove only terminal, uppercase agent-task metadata outside code fences."""
    lines = text.splitlines(keepends=True)
    fence = None
    fenced = []
    for line in lines:
        fenced.append(fence is not None)
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + ",}", line.strip()):
                fence = None
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    removed = False
    while end:
        line = lines[end - 1].strip()
        if fenced[end - 1] or line != line.upper() or not re.fullmatch(
                r"(?:PROMPT:?[ \t]+|FINAL LINE:[ \t]*|MODEL:[ \t]*)\S[^\r\n]*", line):
            break
        removed = True
        end -= 1
        while end and not lines[end - 1].strip():
            end -= 1
    return "".join(lines[:end]).rstrip("\r\n") if removed else text


def _codex_env(home: Path) -> dict[str, str]:
    """Reuse only file authentication in a separate, persistent Codex profile.

    CODEX_HOME controls global AGENTS discovery; project_doc_max_bytes=0
    separately disables cwd/ancestor instruction discovery. Never copy config,
    rules, skills or personal instructions into this profile.
    """
    env = os.environ.copy()
    personal = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    isolated = home / "agent_codex"
    isolated.mkdir(parents=True, exist_ok=True, mode=0o700)
    source, target = personal / "auth.json", isolated / "auth.json"
    # Keep refreshed credentials from the child unless the main CLI signed in
    # more recently. No credential contents go into logs or transcripts.
    if source.is_file() and (not target.is_file() or source.stat().st_mtime_ns > target.stat().st_mtime_ns):
        temp = target.with_suffix(".tmp")
        with temp.open("wb") as stream:
            os.chmod(temp, 0o600)
            stream.write(source.read_bytes())
        os.replace(temp, target)
    env["CODEX_HOME"] = str(isolated)
    return env


@dataclass(frozen=True)
class AgentReply:
    text: str
    session_id: str | None
    argv: tuple[str, ...]
    elapsed_s: float

    def __post_init__(self):
        # Also covers Hermes, which constructs this same reply envelope.
        object.__setattr__(self, "text", clean_reply(self.text))


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    from samsara.process_exit import trace_exit
    trace_exit("agent-child-tree-kill", target_pid=process.pid)
    try:
        import psutil
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
        psutil.wait_procs([*children, parent], timeout=2)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def shutdown_all() -> None:
    _closing.set()
    with _active_lock:
        processes = list(_active)
    for process in processes:
        _kill_tree(process)


thread_registry.on_shutdown(shutdown_all)


def _console_popen_kwargs() -> dict[str, int]:
    """Keep headless CLI launches from creating a Windows console."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


class CliAgent:
    def __init__(self, name: str, argv_builder: Callable, parse_reply: Callable,
                 executable: list[str] | None = None):
        self.name = name
        self.argv_builder = argv_builder
        self.parse_reply = parse_reply
        self.executable = executable

    def _session_file(self, home: Path) -> Path:
        return home / "agent_sessions.json"

    def _sessions(self, home: Path) -> dict:
        try:
            data = json.loads(self._session_file(home).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_sessions(self, home: Path, data: dict) -> None:
        home.mkdir(parents=True, exist_ok=True)
        path = self._session_file(home)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def new_conversation(self, home: Path | None = None) -> None:
        home = home or samsara_home_dir()
        with _turn_locks[self.name], _sessions_lock:
            data = self._sessions(home)
            data.pop(self.name, None)
            self._save_sessions(home, data)

    def ask(self, question: str, config: dict | None = None, *, home: Path | None = None,
            progress: Callable[[str], None] | None = None) -> AgentReply:
        with _turn_locks[self.name]:
            if _closing.is_set():
                raise RuntimeError("Samsara is shutting down")
            return self._ask_unlocked(question, config, home=home, progress=progress)

    def _ask_unlocked(self, question: str, config: dict | None = None, *, home: Path | None = None,
                      progress: Callable[[str], None] | None = None) -> AgentReply:
        config = config or {}
        home = home or samsara_home_dir()
        cwd = Path(config.get("cwd") or DEFAULT_CWD)
        timeout = max(0.01, float(config.get("timeout_s", DEFAULTS["agent_channel.timeout_s"])))
        allow_writes = bool(config.get("allow_writes", False))
        prompt = f"{config.get('voice_preamble') or DEFAULT_PREAMBLE}\n\n{question.strip()}"
        if not cwd.is_dir():
            raise FileNotFoundError(cwd)
        with _sessions_lock:
            saved = self._sessions(home)
            # Old histories may already contain personal instructions. Start
            # clean once, then keep the usual follow-up/resume behaviour.
            session = saved.get(self.name) if self.name in saved.get("_instruction_isolation", []) else None
        exe = self.executable or resolve_executable(self.name)
        argv = self.argv_builder(exe, prompt, session, cwd, allow_writes)
        started = time.monotonic()
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   env=_codex_env(home) if self.name == "gpt" else os.environ.copy(),
                                   **_console_popen_kwargs())
        with _active_lock:
            _active.add(process)
        events: queue.Queue = queue.Queue()

        def drain(stream, source):
            for line in stream:
                events.put((source, line.rstrip("\r\n")))
            events.put((source, None))

        thread_registry.spawn(f"{self.name}.stdout", drain, args=(process.stdout, "out"))
        thread_registry.spawn(f"{self.name}.stderr", drain, args=(process.stderr, "err"))
        if process.stdin:
            if self.name == "gpt":
                process.stdin.write(prompt)
            process.stdin.close()
        output: list[str] = []
        errors: list[str] = []
        closed = set()
        try:
            while len(closed) < 2:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"{self.name} didn't answer in time")
                try:
                    source, line = events.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    closed.add(source)
                elif source == "out":
                    output.append(line)
                    if progress:
                        progress(line)
                else:
                    errors.append(line)
            code = process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
            if code:
                raise RuntimeError(errors[-1] if errors else f"{self.name} exited {code}")
            answer, new_session = self.parse_reply(output)
            answer = clean_reply(answer)
            if not answer.strip():
                raise RuntimeError(f"{self.name} returned no answer")
            if new_session:
                with _sessions_lock:
                    data = self._sessions(home)
                    data[self.name] = new_session
                    data["_instruction_isolation"] = sorted(set(data.get("_instruction_isolation", [])) | {self.name})
                    self._save_sessions(home, data)
            transcript = home / "agent_transcripts" / f"{self.name}.md"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                             f"You: {question.strip()}\n\n{self.name.upper()}: {answer.strip()}\n")
            return AgentReply(answer, new_session or session, tuple(argv),
                              time.monotonic() - started)
        except BaseException:
            _kill_tree(process)
            raise
        finally:
            with _active_lock:
                _active.discard(process)


def agent(name: str) -> CliAgent:
    if name == "hermes":
        from .hermes import HermesAgent
        return HermesAgent()
    if name in {"gpt", "codex"}:
        return CliAgent("gpt", codex_argv, parse_codex)
    if name == "claude":
        return CliAgent("claude", claude_argv, parse_claude)
    raise ValueError(name)


def followup(app, action: str) -> bool:
    state = getattr(app, "_cli_agent_voice_state", None)
    if not state or time.monotonic() > state["expires"]:
        return False
    from .claude_reply import _drill_followup
    if state.get("agent") == "hermes":
        from .hermes_voice import channel, followup as hermes_followup
        if not channel(app).valid(state["turn"]):
            return False
        # Hermes paging owns capture waiting and the current feedback level.
        if hermes_followup(app, action, state):
            return True
        return _drill_followup(app, action, state, cli=True)
    if _drill_followup(app, action, state, cli=True):
        return True
    return False
