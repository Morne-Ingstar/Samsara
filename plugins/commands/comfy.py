"""Image commands backed by the owner's local ComfyUI server."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field

from samsara.agent_channel.claude_reply import spoken_form
from samsara.agent_channel.comfy import (ComfyClient, ComfyError, JobCancelled,
                                         JobTimeout, SubmissionUnknown, settings)
from samsara.command_registry import DispatchResult, DispatchState
from samsara.log import get_logger
from samsara.plugin_commands import command
from samsara.runtime import thread_registry

logger = get_logger(__name__)


@dataclass
class ImageState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    cancel: threading.Event = field(default_factory=threading.Event)
    active: bool = False
    client: object = None
    job: object = None
    last_prompt: str = ""
    paths: list = field(default_factory=list)
    status: str = "No image has been requested."


_state_lock = threading.Lock()


def _state(app):
    with _state_lock:
        if not hasattr(app, "_comfy_state"):
            app._comfy_state = ImageState()
        return app._comfy_state


def _feedback(app, text, *, kind="success", caption=None):
    """Same conversation speech/chip conventions as 309; never speak artwork text."""
    coordinator = getattr(app, "audio_coordinator", None)
    def show_caption():
        emit = getattr(coordinator, "_emit_caption", None)
        if caption and emit:
            emit(caption, hold=False)
    if coordinator is not None:
        spoken, _ = spoken_form(text)
        handle = coordinator.speak(spoken, category="conversation", interruptible=True,
                                   on_done=show_caption)
        if str(getattr(handle, "utterance_id", "")).startswith("noop-"):
            show_caption()
    show = getattr(app, "_show_outcome_chip", None)
    if show:
        show(caption or text, kind)


def _result(state=DispatchState.COMPLETED):
    return DispatchResult(state, "image", "image")


def try_voice_command(app, text, generation=None):
    """Claim exact image grammar in Ava's conversation lane before any model.

    Recognition deliberately ignores pack state: the existing executor then
    enforces it. A disabled image command must not leak artwork to a model.
    """
    from samsara import execution_policy, plugin_commands
    from samsara.command_registry import CommandMatcher, view_tokens
    matcher = CommandMatcher()
    matcher.load_plugins(plugin_commands._MODULE_ENTRIES.get(__name__, {}))
    matcher.freeze()
    if text.startswith("STAGED TEXT:\n"):
        # The upstream envelope has no unambiguous delimiter for a multiline
        # image prompt. Never guess at it, or send that artwork to a model.
        if any(matcher.match(part)[0] for part in text.split("\n\n")[1:]):
            _feedback(app, "Please say the image command again without staged text.", kind="warning")
            return _result(DispatchState.REJECTED)
        return None
    if [part[0] for part in view_tokens(text)] == ["show", "me"]:
        entry, remainder = matcher.match("open the image")
    else:
        entry, remainder = matcher.match(text)
    if entry is None or (remainder and not getattr(entry.handler, "verbatim_argument", False)):
        return None
    executor = getattr(app, "command_executor", None)
    if executor is None:
        _feedback(app, "The image command is unavailable.", kind="warning")
        return _result(DispatchState.FAILED)
    result = executor.execute_canonical(entry.phrase, app, route=execution_policy.Route.EXACT,
        generation=generation, args={"remainder": remainder}, source_text=text)
    logger.info("[COMFY] voice route command=%s result=%s", entry.phrase, result.state.value)
    if result.state in (DispatchState.REJECTED, DispatchState.FAILED, DispatchState.MISS):
        _feedback(app, "The image command is unavailable. Check that the AI Assistant command pack is enabled.", kind="warning")
    return result


def _complete(app, state, result):
    state.paths = result.paths
    count = len(result.paths)
    spoken_count = "one image" if count == 1 else f"{count} images"
    state.status = f"Done — {spoken_count}, {round(result.elapsed)} seconds. Say open it."
    if settings(app.config)["auto_open"]:
        try:
            os.startfile(str(result.paths[0]))
        except OSError:
            state.status += " I couldn't open the viewer."
    _feedback(app, state.status, caption=", ".join(path.name for path in result.paths))
    logger.info("[COMFY] complete prompt_id=%s elapsed=%.2f outputs=%s",
                result.job.prompt_id, result.elapsed, count)


def _start(app, prompt_text=None, *, resume=False, cancel=False):
    state = _state(app)
    with state.lock:
        if state.active:
            if cancel:
                state.cancel.set()
                _feedback(app, "Cancelling the image.", kind="pending")
            else:
                _feedback(app, state.status, kind="pending")
            return _result(DispatchState.QUEUED)
        if not resume and state.job and state.job.state not in ("complete", "cancelled", "failed"):
            _feedback(app, "The previous image is still unresolved. Say image status or cancel the image.", kind="warning")
            return _result()
        state.active = True
        state.cancel.clear()
        if cancel:
            state.cancel.set()

    def progress(job):
        state.status = ("Still queued in ComfyUI." if job.state == "queued" else
                        "Still rendering" + (f", {job.percent} percent." if job.percent is not None else "."))
        _feedback(app, state.status, kind="pending")

    async def render():
        if not resume:
            cfg = settings(app.config)
            state.client = ComfyClient(cfg["base_url"], config=app.config)
        async with state.client as client:
            if not resume:
                state.status = "Sending to ComfyUI."
                _feedback(app, state.status, kind="pending")
                if state.cancel.is_set():
                    raise JobCancelled("Image cancelled before sending.")
                state.job = await client.submit(prompt_text)
                state.last_prompt = prompt_text
                state.status = "Image queued in ComfyUI."
                logger.info("[COMFY] submitted prompt_id=%s", state.job.prompt_id)
            if resume and cancel:
                await client.interrupt(state.job)
                raise JobCancelled("Image cancelled.")
            if resume:
                # A fresh observation window; the original elapsed time is retained.
                timeout = settings(app.config)["timeout_s"] + (time.monotonic() - state.job.started)
            else:
                timeout = None
            record = await client.poll(state.job, on_progress=progress,
                                       cancel=state.cancel, timeout_s=timeout)
            result = await client.fetch_outputs(state.job, record)
            _complete(app, state, result)

    def run():
        try:
            asyncio.run(render())
        except JobCancelled as exc:
            if state.job:
                state.job.state = "cancelled"
            state.status = str(exc)
            _feedback(app, state.status)
        except ComfyError as exc:
            state.status = str(exc)
            if isinstance(exc, SubmissionUnknown):
                state.job = state.client.job
                state.last_prompt = prompt_text
                if state.job:
                    state.job.state = "unconfirmed"
            elif state.job and not isinstance(exc, JobTimeout):
                # A lost connection is unresolved, not proof that the job stopped.
                from samsara.agent_channel.comfy import ServerDown
                if not isinstance(exc, ServerDown):
                    state.job.state = "failed"
            _feedback(app, state.status, kind="warning")
            logger.info("[COMFY] stopped reason=%s", type(exc).__name__)
        except Exception:
            state.status = "The image request failed. Check image status before retrying."
            _feedback(app, state.status, kind="warning")
            logger.error("[COMFY] unexpected image worker failure")
        finally:
            with state.lock:
                state.active = False

    try:
        thread_registry.spawn("comfy.image", run)
    except Exception:
        state.active = False
        _feedback(app, "I couldn't start the image request.", kind="warning")
    return _result(DispatchState.QUEUED)


@command("make an image of", aliases=["give me an image of", "generate an image of", "draw me", "render an image of"],
         pack="ai", risk_class="read", ai_composable=False, side_effects=["network", "file", "ui"])
def make_image(app, remainder):
    """Makes an image locally in ComfyUI, using exactly the description you say."""
    if not remainder.strip():
        _feedback(app, "Tell me what image to make.")
        return _result()
    return _start(app, remainder)


make_image.verbatim_argument = True


@command("make another", pack="ai", risk_class="read", ai_composable=False, side_effects=["network", "file", "ui"])
def make_another(app, remainder):
    """Makes another image with the same description and a fresh seed."""
    if not _state(app).last_prompt:
        _feedback(app, "Make an image first.")
        return _result()
    return _start(app, _state(app).last_prompt)


@command("make it again but", pack="ai", risk_class="read", ai_composable=False, side_effects=["network", "file", "ui"])
def make_amended(app, remainder):
    """Makes another image, appending your change verbatim to the description."""
    previous = _state(app).last_prompt
    if not previous or not remainder.strip():
        _feedback(app, "Make an image first, then tell me the change.")
        return _result()
    return _start(app, previous + "\n" + remainder)


make_amended.verbatim_argument = True


@command("open the image", aliases=["open it"], pack="ai", risk_class="ui", side_effects=["ui"])
def open_image(app, remainder):
    """Opens the last completed image in your default image viewer."""
    paths = _state(app).paths
    if not paths:
        _feedback(app, "There isn't a completed image yet.")
    else:
        try:
            os.startfile(str(paths[0]))
            _feedback(app, "Opening the image.", caption=paths[0].name)
        except OSError:
            _feedback(app, "I couldn't open the image viewer.", kind="warning", caption=paths[0].name)
    return _result()


@command("where is the image", pack="ai", risk_class="read", side_effects=[])
def where_image(app, remainder):
    """Says where the last completed image was saved."""
    paths = _state(app).paths
    if not paths:
        _feedback(app, "There isn't a completed image yet.")
    else:
        _feedback(app, f"It's in your Samsara images folder, under {paths[0].parent.name}. Say open it.",
                  caption=str(paths[0]))
    return _result()


@command("cancel the image", pack="ai", risk_class="read", side_effects=["network"])
def cancel_image(app, remainder):
    """Cancels this session's image without deleting any saved outputs."""
    state = _state(app)
    if state.active:
        return _start(app, cancel=True)
    if state.job and state.job.state not in ("complete", "cancelled", "failed"):
        return _start(app, resume=True, cancel=True)
    _feedback(app, "There isn't a running image to cancel.")
    return _result()


@command("image status", pack="ai", risk_class="read", side_effects=["network"])
def image_status(app, remainder):
    """Reports image progress and rechecks an unresolved render."""
    state = _state(app)
    if not state.active and state.job and state.job.state not in ("complete", "cancelled", "failed"):
        return _start(app, resume=True)
    _feedback(app, state.status, kind="pending" if state.active else "success")
    return _result()
