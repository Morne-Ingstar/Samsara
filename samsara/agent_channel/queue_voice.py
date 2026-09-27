"""Read the owner's file-backed prompt queue without creating another ledger."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from samsara.agent_channel.claude_reply import spoken_form
from samsara.intent.normalize import parse_number
from samsara.paths import samsara_home_dir


DEFAULT_QUEUE = Path(r"C:\Users\Morne\Documents\Claude\queue")
DEFAULT_REPORTS = Path(r"C:\Users\Morne\Documents\Claude\reports")
DEFAULT_PYTHON = Path(r"F:\envs\sami\python.exe")
_ID = re.compile(r"\d{2,3}[a-z]?", re.I)
_PROMPT = re.compile(r"^(\d+[a-z]?)_.*\.md$", re.I)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^]]+)\]\([^)]*\)")


class QueueReadError(Exception):
    """The queue or its canonical parser could not be read."""


@dataclass(frozen=True)
class QueuePaths:
    queue_root: Path = DEFAULT_QUEUE
    reports_root: Path = DEFAULT_REPORTS
    python: Path = DEFAULT_PYTHON
    home: Path | None = None
    note_command: Path | None = None


class _ImportOutput(io.StringIO):
    def reconfigure(self, **_kwargs):
        # queue_note.py requests UTF-8 on import; a windowed EXE has no stdout.
        pass


def _load_queue_note(root: Path, reports: Path):
    source = root.parent / "queue_note.py"
    if not source.is_file():
        raise QueueReadError("I can't read the queue folder")
    spec = importlib.util.spec_from_file_location("_samsara_queue_note", source)
    if spec is None or spec.loader is None:
        raise QueueReadError("I can't read the queue folder")
    module = importlib.util.module_from_spec(spec)
    # The script's top-level stdout.reconfigure() assumes a console. Shield
    # the import in windowed Samsara, then use its functions without a rewrite.
    try:
        with contextlib.redirect_stdout(_ImportOutput()):
            spec.loader.exec_module(module)
    except Exception as exc:
        raise QueueReadError("I can't read the queue folder") from exc
    module.QUEUE = root
    module.REPORTS = reports
    return module


def prompt_id(spoken: str) -> str | None:
    """Resolve a queue id using the shared number parser, including digit runs."""
    value = (spoken or "").strip().lower().rstrip(".?!")
    if _ID.fullmatch(value):
        return value
    words = value.split()
    if len(words) not in (2, 3):
        return None
    digits = [parse_number([word]) for word in words]
    if any(digit is None or not 0 <= digit <= 9 for digit in digits):
        return None
    result = "".join(str(digit) for digit in digits)
    return result if _ID.fullmatch(result) else None


def _plain(line: str) -> str:
    line = _LINK.sub(r"\1", line)
    line = re.sub(r"^[\s#>*-]+", "", line)
    line = line.replace("_", " ").replace(" — ", ", ").replace(" – ", ", ")
    line = re.sub(r"(?<=\w)\.(?=\w)", " ", line)
    return re.sub(r"[*`]", "", line).strip()


def _first_sentence(text: str) -> str:
    for heading in ("summary", "result", "changes", "phase 2", "part 2", "implementation"):
        match = re.search(rf"(?im)^(?:#{{1,3}}\s+)?{heading}\b[^\n]*\n", text)
        if match:
            for line in text[match.end():].splitlines():
                if line.startswith("#"):
                    break
                plain = _plain(line)
                if plain:
                    return re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0]
    for line in text.splitlines():
        plain = _plain(line)
        if plain and not plain.startswith(("MODEL:", "STATUS:", "PROMPT ", "PREMISES")):
            return re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0]
    return "Report filed."


def _report_prose(text: str) -> str:
    text = _CODE_BLOCK.sub(" there's a code block on screen ", text)
    return " ".join(_plain(line) for line in text.splitlines()
                    if _plain(line) and not line.lstrip().startswith(
                        ("|", "MODEL:", "STATUS:", "PROMPT ")))


class QueueView:
    def __init__(self, paths: QueuePaths):
        self.paths = paths
        if not paths.queue_root.is_dir() or not paths.reports_root.is_dir():
            raise QueueReadError("I can't read the queue folder")
        self.note = _load_queue_note(paths.queue_root, paths.reports_root)
        self._items: list[str] = []
        self._index = 0
        self._report_rest = ""
        self._last_report = ""
        self._report_expires = 0.0

    @property
    def state_file(self) -> Path:
        return (self.paths.home or samsara_home_dir()) / "queue_voice_state.json"

    def _last_finished_ns(self) -> int:
        try:
            return int(json.loads(self.state_file.read_text(encoding="utf-8"))["last_finished_ns"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def _save_finished_ns(self, value: int) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"last_finished_ns": value}), encoding="utf-8")

    def _page(self) -> tuple[str, int]:
        page = self._items[self._index:self._index + 3]
        self._index += len(page)
        if not page:
            return "That's all for now.", 0
        return " ".join(page), len(page)

    @property
    def has_more(self) -> bool:
        return self._index < len(self._items)

    def more(self) -> tuple[str, int]:
        return self._page()

    def _prompt_files(self, folder: Path) -> list[Path]:
        return [path for path in folder.glob("*.md") if _PROMPT.fullmatch(path.name)]

    def _prompt(self, pid: str) -> Path | None:
        for folder in (self.paths.queue_root, self.paths.queue_root / "running",
                       self.paths.queue_root / "hold", self.paths.queue_root / "done"):
            for path in self._prompt_files(folder):
                if self.note.label(path).lower() == pid.lower():
                    return path
        return None

    def _status(self, pid: str, prompt: Path | None = None) -> str:
        state, why = self.note.report_state(pid, prompt)
        if state is None:
            return "NO REPORT"
        if state == "blocked":
            return "BLOCKED"
        report = self.paths.reports_root / pid / "FINAL.md"
        text = report.read_text(encoding="utf-8", errors="replace")
        match = self.note._STATUS_LINE.search(text)
        if match:
            word = match.group(1).strip().upper()
            if "DONE" in word or "COMPLETE" in word:
                return "DONE"
            if any(flag in word for flag in ("AWAIT", "PREVIEW", "PICK", "ACCEPT")):
                return "AWAITING"
        return "AWAITING"  # queue_note reports work but no explicit DONE status

    def finished(self) -> tuple[str, int]:
        previous = self._last_finished_ns()
        cutoff = time.time_ns()
        reports = []
        for final in self.paths.reports_root.glob("*/FINAL.md"):
            if not _ID.fullmatch(final.parent.name):
                continue
            stamp = final.stat().st_mtime_ns
            if previous < stamp <= cutoff:
                reports.append((stamp, final.parent.name, final))
        reports.sort(reverse=True)
        self._items = []
        for _stamp, pid, final in reports:
            text = final.read_text(encoding="utf-8", errors="replace")
            summary = _first_sentence(text)
            sentence, _ = spoken_form(summary, len(summary.split()) + 20)
            self._items.append(f"{pid}, {self._status(pid, self._prompt(pid))}, {sentence.rstrip(' .')}.")
        self._save_finished_ns(cutoff)
        self._index = 0
        if not reports:
            if previous:
                when = datetime.fromtimestamp(previous / 1_000_000_000).strftime("%I:%M %p").lstrip("0")
                return f"Nothing new since {when}.", 0
            return "No reports yet.", 0
        return self._page()

    def read_report(self, pid: str) -> str:
        final = self.paths.reports_root / pid / "FINAL.md"
        if not final.is_file():
            self._last_report = self._report_rest = ""
            return f"{pid}, NO REPORT."
        text = final.read_text(encoding="utf-8", errors="replace")
        summary = _first_sentence(text)
        first, _ = spoken_form(f"{pid}, {self._status(pid, self._prompt(pid))}. {summary}", 60)
        self._last_report = first
        self._report_rest = _report_prose(text)
        self._report_expires = time.monotonic() + 60
        return first

    def report_followup(self, action: str) -> str | None:
        if not self._last_report or time.monotonic() > self._report_expires:
            return None
        self._report_expires = time.monotonic() + 60
        if action == "repeat that":
            return self._last_report
        if action == "read the rest":
            if not self._report_rest:
                return "That's all of the report."
            chunk, self._report_rest = spoken_form(self._report_rest, 60)
            self._last_report = chunk
            return chunk
        if action in ("stop claude", "stop queue"):
            self._report_rest = ""
            self._last_report = ""
            return ""
        return None

    def _claimed(self) -> dict[str, str]:
        running = self.paths.queue_root / "running"
        result = {}
        for path in self._prompt_files(running):
            result[self.note.label(path)] = self.note.parse(path)[0].get("AGENT", "agent unknown")
        for lock in (running / "locks").glob("*.lock"):
            pid = lock.stem.lower()
            if not _ID.fullmatch(pid):
                continue
            prompt = self._prompt(pid)
            result.setdefault(pid, self.note.parse(prompt)[0].get("AGENT", "agent unknown")
                              if prompt else "agent unknown")
        return result

    def running(self) -> tuple[str, int]:
        claimed = self._claimed()
        self._items = [f"{pid}, running, {agent}." for pid, agent in sorted(claimed.items(), reverse=True)]
        self._index = 0
        return self._page() if claimed else ("Nothing claimed right now.", 0)

    def _dependencies(self, head: dict[str, str]) -> set[str] | None:
        after = head.get("AFTER", "")
        required = self.note.stated_ids({"AFTER": after}) if after else set()
        order = head.get("ORDER", "").strip()
        if not order:
            return required
        match = re.search(r"\bAFTER\s+([^.()]+?)\s+(?:is|are)\s+"
                          r"(?:reconciled|committed|done|accepted|landed)\b|"
                          r"\bAFTER\s+([^.()]+?)\s+lands?\b", order, re.I)
        if "AFTER" in order.upper() and not match:
            return None
        tail = re.sub(r"\([^)]*\)", "", order[match.end():] if match else order)
        for sentence in tail.split("."):
            sentence = sentence.strip()
            if not sentence:
                continue
            if re.match(r"(?i)^(parallel.safe|independent|do not touch|does not dispatch)\b", sentence):
                continue
            if re.match(r"(?i)^never beside\b", sentence) and self.note.owned({"OWNS": sentence}):
                continue  # the running OWNS overlap check below enforces this
            return None
        if match:
            clause = next(group for group in match.groups() if group)
            required |= self.note.stated_ids({"AFTER": "AFTER " + clause})
        return required

    def runnable(self) -> tuple[str, int]:
        root = self.paths.queue_root
        claimed_ids = self._claimed()
        active_paths = [self._prompt(pid) for pid in claimed_ids]
        active_owns = []
        unknown_claim = False
        for path in active_paths:
            if path:
                owns = self.note.owned(self.note.parse(path)[0])
                if owns:
                    active_owns.append(owns)
                else:
                    unknown_claim = True
        for lock in (root / "running" / "locks").glob("*.lock"):
            try:
                data = json.loads(lock.read_text(encoding="utf-8"))
                owns = data.get("owns")
                if isinstance(owns, list) and owns and all(isinstance(item, str) for item in owns):
                    active_owns.append(set(owns))
                elif self._prompt(lock.stem) is None:
                    unknown_claim = True
            except (OSError, ValueError, TypeError, AttributeError):
                unknown_claim = True
        if unknown_claim:
            return "I can't verify a running claim. Check the note.", 0
        done_ids = {self.note.label(path) for path in self._prompt_files(root / "done")}
        ready = []
        uncertain = False
        for path in self._prompt_files(root):
            pid = self.note.label(path)
            if pid in claimed_ids:
                continue
            if self.note.report_state(pid, path)[0] is not None and not self.note._redispatch_reason(path, pid):
                continue
            head, title = self.note.parse(path)
            deps = self._dependencies(head)
            if deps is None:
                uncertain = True
                continue
            if not deps.issubset(done_ids):
                continue
            own = self.note.owned(head)
            if not own:
                uncertain = True
                continue
            if any(self.note._overlap(own, active) for active in active_owns):
                continue
            ready.append((pid, title or path.stem.removeprefix(pid + "_").replace("_", " ")))
        ready.sort(reverse=True)
        self._items = [f"{pid}, ready, {title}." for pid, title in ready]
        self._index = 0
        if not ready:
            return ("I can't prove a prompt is ready. Check the note." if uncertain
                    else "Nothing ready to run."), 0
        return self._page()

    def _run_note(self) -> str:
        script = self.paths.note_command or self.paths.queue_root.parent / "queue_note.py"
        try:
            result = subprocess.run([str(self.paths.python), str(script)],
                                    capture_output=True, text=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"queue note failed: {str(exc).splitlines()[-1]}"
        stdout = result.stdout.splitlines()
        stderr = result.stderr.splitlines()
        lines = (stderr or stdout) if result.returncode else (stdout or stderr)
        last = lines[-1].strip() if lines else "no output"
        return last if result.returncode == 0 else f"queue note failed: {last}"

    def queue_note(self) -> str:
        return self._run_note()

    def accept(self, pid: str) -> str:
        prompt = self._prompt(pid)
        if prompt is None:
            return f"I can't find prompt {pid}."
        if prompt.parent.name == "done":
            return f"{pid} is already accepted."
        if self.note.report_state(pid, prompt)[0] != "reported":
            return f"{pid} has no finished report to accept."
        marker = self.paths.queue_root / "accepted" / pid
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists() and marker.stat().st_size:
            return f"The acceptance marker for {pid} is not empty. Check the note."
        marker.touch(exist_ok=True)
        return f"Accepted {pid}. {self._run_note()}"
