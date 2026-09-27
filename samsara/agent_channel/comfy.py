"""Local ComfyUI jobs. No cloud fallback, redirects, or automatic submission retries."""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from samsara.paths import samsara_home_dir


class ComfyError(Exception):
    pass


class ServerDown(ComfyError):
    pass


class JobTimeout(ComfyError):
    pass


class JobCancelled(ComfyError):
    pass


class SubmissionUnknown(ComfyError):
    """POST may have landed: never silently retry it."""


def settings(config=None):
    from samsara.config_schema import SETTINGS_SCHEMA
    result = {key[6:]: copy.deepcopy(value["default"])
              for key, value in SETTINGS_SCHEMA.items() if key.startswith("comfy.")}
    result.update((config or {}).get("comfy", {}))
    return result


def local_url(value):
    parsed = urlsplit(value)
    host = parsed.hostname
    if host == "localhost":
        host = "127.0.0.1"
    try:
        valid = ipaddress.ip_address(host).is_loopback
    except ValueError:
        valid = False
    if (not valid or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ComfyError("ComfyUI must use an HTTP loopback address on this machine.")
    return f"http://{'[' + host + ']' if ':' in host else host}:{parsed.port or 80}"


def _set_input(graph, path, value):
    if not isinstance(path, list) or len(path) != 3 or path[1] != "inputs":
        raise ComfyError("Workflow fields must be [node, inputs, field] paths.")
    try:
        inputs = graph[str(path[0])]["inputs"]
        if path[2] not in inputs or isinstance(inputs[path[2]], list):
            raise KeyError(path[2])
        inputs[path[2]] = value
    except (KeyError, TypeError) as exc:
        raise ComfyError("Workflow field is missing or linked; import the workflow again.") from exc


@dataclass
class Job:
    prompt_id: str
    prompt_text: str
    seed: int
    started: float
    percent: int | None = None
    state: str = "queued"


@dataclass
class RenderResult:
    job: Job
    paths: list[Path]
    elapsed: float


class ComfyClient:
    """Use as an async context manager; each instance owns one job and socket."""

    def __init__(self, base_url="http://127.0.0.1:8001", *, config=None, home=None,
                 poll_interval=1.0, progress_interval=15.0, request_timeout=10.0):
        self.base_url = local_url(base_url)
        self.config = settings(config)
        self.home = Path(home) if home else samsara_home_dir()
        self.client_id = uuid.uuid4().hex
        self.poll_interval = poll_interval
        self.progress_interval = progress_interval
        self.request_timeout = request_timeout
        self.session = None
        self.socket = None
        self.job = None

    async def __aenter__(self):
        async def no_redirect(*_):
            raise ComfyError("ComfyUI redirects are not allowed.")
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(no_redirect)
        self.session = aiohttp.ClientSession(trust_env=False,
            timeout=aiohttp.ClientTimeout(total=self.request_timeout), trace_configs=[trace])
        self.socket = None
        return self

    async def __aexit__(self, *_):
        if self.socket is not None:
            await self.socket.close()
        await self.session.close()

    async def _request(self, method, path, *, body=None, binary=False):
        try:
            async with self.session.request(method, self.base_url + path, json=body,
                                            allow_redirects=False) as response:
                if response.status >= 300:
                    # Error bodies can contain the owner's prompt; never log them.
                    if method == "POST" and path == "/prompt" and response.status >= 500:
                        raise SubmissionUnknown("ComfyUI submission is unconfirmed. Say image status before retrying.")
                    raise ComfyError(f"ComfyUI rejected {path.split('?')[0]} (HTTP {response.status}).")
                if binary:
                    return await response.read()
                data = await response.read()
                return json.loads(data) if data else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if method == "POST" and path == "/prompt":
                raise SubmissionUnknown("ComfyUI submission could not be confirmed. Check image status before retrying.") from exc
            raise ServerDown("ComfyUI isn't running") from exc
        except (ValueError, UnicodeError) as exc:
            if method == "POST" and path == "/prompt":
                raise SubmissionUnknown("ComfyUI submission is unconfirmed. Say image status before retrying.") from exc
            raise ComfyError("ComfyUI returned an unreadable response.") from exc

    async def health(self):
        return await self._request("GET", "/system_stats")

    async def object_info(self):
        return await self._request("GET", "/object_info")

    async def queue(self):
        return await self._request("GET", "/queue")

    def prepare(self, prompt_text, workflow_name=None, overrides=None):
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise ComfyError("Tell me what image to make.")
        name = workflow_name or self.config["default_workflow"]
        try:
            template = self.config["workflows"][name]
            path = Path(template["path"])
            if not path.is_absolute():
                path = self.home / path
            graph = json.loads(path.read_text(encoding="utf-8-sig"))
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise ComfyError("Import the ComfyUI workflow before making an image.") from exc
        if not isinstance(graph, dict) or "nodes" in graph:
            raise ComfyError("The workflow must be exported in API format.")
        values = dict(overrides or {})
        if values.keys() - {"seed", "width", "height"}:
            raise ComfyError("Only seed, width, and height overrides are supported.")
        values.setdefault("seed", secrets.randbits(63))
        for field, value in values.items():
            limit = 2**64 - 1 if field == "seed" else 16384
            minimum = 0 if field == "seed" else 16
            if type(value) is not int or not minimum <= value <= limit:
                raise ComfyError(f"Invalid {field} override.")
            _set_input(graph, template.get(field), value)
        text = self.config["prompt_prefix"] + prompt_text + self.config["prompt_suffix"]
        _set_input(graph, template.get("prompt"), text)
        return graph, values["seed"]

    async def _connect_progress(self):
        try:
            self.socket = await self.session.ws_connect(
                self.base_url.replace("http:", "ws:", 1) + "/ws",
                params={"clientId": self.client_id}, timeout=self.request_timeout,
                max_msg_size=16 * 1024 * 1024)
        except (aiohttp.ClientError, asyncio.TimeoutError, ComfyError):
            self.socket = None  # History remains the authority when WS is unavailable.

    async def submit(self, prompt_text, workflow_name=None, overrides=None):
        graph, seed = self.prepare(prompt_text, workflow_name, overrides)
        await self.health()
        info = await self.object_info()
        if any(info.get(node.get("class_type"), {}).get("api_node") for node in graph.values()):
            raise ComfyError("Cloud API nodes are not allowed in local image workflows.")
        await self._connect_progress()  # Subscribe before enqueueing.
        started = time.monotonic()
        # ComfyUI 0.36 accepts caller-supplied IDs. Keep it even if the POST
        # response is lost, so status/cancel can observe this exact job.
        self.job = Job(str(uuid.uuid4()), prompt_text, seed, started)
        reply = await self._request("POST", "/prompt", body={
            "prompt": graph, "client_id": self.client_id, "prompt_id": self.job.prompt_id})
        if not reply.get("prompt_id"):
            raise SubmissionUnknown("ComfyUI submission is unconfirmed. Say image status before retrying.")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", str(reply["prompt_id"])):
            raise SubmissionUnknown("ComfyUI returned an invalid job identity. Check its queue before retrying.")
        self.job.prompt_id = str(reply["prompt_id"])
        return self.job

    async def interrupt(self, job):
        if self.job is not job:
            raise ComfyError("That image does not belong to this session.")
        # Delete this pending item only. Never clear the server's queue.
        await self._request("POST", "/queue", body={"delete": [job.prompt_id]})
        queue = await self.queue()
        if any(row[1] == job.prompt_id for row in queue.get("queue_running", [])):
            await self._request("POST", "/interrupt", body={"prompt_id": job.prompt_id})
        job.state = "cancelled"

    def _progress_message(self, job, message):
        data = message.get("data", {})
        if data.get("prompt_id") != job.prompt_id:
            return
        kind = message.get("type")
        if kind == "execution_interrupted":
            job.state = "cancelled"
            raise JobCancelled("Image cancelled.")
        if kind == "execution_error":
            raise ComfyError("ComfyUI could not render the image. Check its window for details.")
        if kind in ("execution_start", "executing", "progress"):
            job.state = "rendering"
        if kind == "progress" and isinstance(data.get("max"), (int, float)) and data["max"] > 0:
            job.percent = max(0, min(100, round(100 * data["value"] / data["max"])))

    async def poll(self, job, *, on_progress=None, cancel=None, timeout_s=None):
        timeout_s = self.config["timeout_s"] if timeout_s is None else timeout_s
        next_speech = time.monotonic() + self.progress_interval
        next_history = 0.0
        while True:
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                await self.interrupt(job)
                raise JobCancelled("Image cancelled.")
            if now - job.started >= timeout_s:
                job.state = "timed out"
                raise JobTimeout("The image timed out. It may still be rendering; say cancel the image to stop it.")
            if now >= next_history:
                history = await self._request("GET", f"/history/{job.prompt_id}")
                record = history.get(job.prompt_id)
                if record:
                    status = record.get("status", {})
                    for kind, _data in status.get("messages", []):
                        if kind == "execution_interrupted":
                            raise JobCancelled("Image cancelled.")
                    if status.get("status_str") == "error":
                        raise ComfyError("ComfyUI could not render the image. Check its window for details.")
                    if status.get("completed") or record.get("outputs"):
                        job.state = "complete"
                        return record
                queue = await self.queue()
                if any(row[1] == job.prompt_id for row in queue.get("queue_running", [])):
                    job.state = "rendering"
                next_history = now + self.poll_interval
            if now >= next_speech:
                if on_progress:
                    on_progress(job)
                next_speech = now + self.progress_interval
            wait = max(0.01, min(self.poll_interval, next_history - time.monotonic(),
                                 timeout_s - (time.monotonic() - job.started)))
            if self.socket is None or self.socket.closed:
                await asyncio.sleep(wait)
                continue
            try:
                message = await self.socket.receive(timeout=wait)
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._progress_message(job, json.loads(message.data))
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    await self.socket.close()
            except asyncio.TimeoutError:
                pass
            except (aiohttp.ClientError, ValueError, TypeError, KeyError):
                await self.socket.close()

    async def fetch_outputs(self, job, record):
        from urllib.parse import urlencode
        directory = self.home / "images" / datetime.now().strftime("%Y-%m-%d")
        paths = []
        for output in record.get("outputs", {}).values():
            for item in output.get("images", []):
                filename = item.get("filename", "")
                subfolder = item.get("subfolder", "")
                if (item.get("type") != "output" or Path(filename).name != filename
                        or any(c in filename for c in '\\/:')
                        or Path(filename).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")
                        or Path(subfolder).is_absolute() or ":" in subfolder
                        or ".." in subfolder.replace("\\", "/").split("/")):
                    raise ComfyError("ComfyUI returned an invalid output path.")
                content = await self._request("GET", "/view?" + urlencode({
                    "filename": filename, "subfolder": subfolder, "type": "output"}), binary=True)
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{job.prompt_id[:8]}_{len(paths) + 1}_{filename}"
                if target.exists():
                    if target.read_bytes() != content:
                        raise ComfyError("An existing image has different contents; nothing overwritten.")
                else:
                    with target.open("xb") as stream:
                        stream.write(content)
                paths.append(target)
        if not paths:
            raise ComfyError("ComfyUI finished without a saved image.")
        def cell(value):
            return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")
        index = directory.parent / "index.md"
        with index.open("a", encoding="utf-8") as stream:
            if stream.tell() == 0:
                stream.write("| Time | Prompt | Seed | Path |\n| --- | --- | --- | --- |\n")
            for path in paths:
                stream.write("| " + " | ".join(map(cell, (datetime.now().isoformat(timespec="seconds"),
                    job.prompt_text, job.seed, path))) + " |\n")
        return RenderResult(job, paths, time.monotonic() - job.started)
