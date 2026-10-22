"""Hermes background commands, cancellation and capture-safe voice feedback."""
from contextlib import nullcontext
import time

from samsara.command_registry import DispatchState
from samsara.config_defaults import cfg_get
from samsara.runtime import thread_registry
from . import cli_agents
from .claude_reply import spoken_form
from .spoken_digest import first_reply
from .hermes import BusyError, Cancelled, EFFORTS, permitted


def channel(app):
    if not hasattr(app, "_hermes_channel"):
        app._hermes_channel = cli_agents.agent("hermes")
    return app._hermes_channel


def level(app):
    return cfg_get(getattr(app, "config", {}) or {}, "tts.feedback_level")


def capturing(app):
    if any(bool(getattr(app, key, False)) for key in (
            "recording", "is_speaking", "hotkey_pressed", "_hold_down_key",
            "_command_mode_key_held", "_ava_mode_key_held")):
        return True
    controller = getattr(getattr(app, "listening_indicator", None), "controller", None)
    consumer = getattr(app, "_wake_consumer", None)
    if getattr(consumer, "_silero_was_speech", False) or getattr(consumer, "_speech_start_ns", None) is not None:
        return True
    capture = getattr(getattr(controller, "model", None), "capture", None)
    return getattr(capture, "value", capture) in {"recording", "transcribing"}


def schedule(app, callback):
    post = getattr(app, "_schedule_ui", None)
    if post:
        post(callback)
    else:
        from samsara.ui import qt_runtime
        qt_runtime.post(callback)


def chip(app, text, kind="info", valid=lambda: True):
    def show():
        with channel(app)._state_lock:
            if not valid():
                return
            indicator = getattr(app, "listening_indicator", None)
            if callable(getattr(type(indicator), "show_feedback", None)):
                question = getattr(indicator, "_question_alive", None)
                if callable(question) and question():
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(100, indicator, show)
                    return  # Keep the Compact question answerable, then show this result.
                from samsara.live_surface.feedback import Feedback, FeedbackKind, DEFAULT_TTL_MS
                feedback = Feedback(FeedbackKind.ERROR if kind == "error" else FeedbackKind.INFORMATION,
                                    owner="hermes.result", alive=valid)
                indicator.show_feedback("Hermes: " + text, kind, DEFAULT_TTL_MS, feedback)
                # Complete the Full display's queued handoff under the same
                # generation lock; a reset cannot overtake a pending result.
                controller = getattr(indicator, "controller", None)
                if controller:
                    controller.drain()
            else:
                callback = getattr(app, "_show_outcome_chip", None)
                if callback:
                    callback("Hermes: " + text, kind)
    schedule(app, show)


def waiting(app, active, turn):
    def update():
        if active:
            if not channel(app).valid(turn):
                return
            app._hermes_waiting_turn = turn
        elif getattr(app, "_hermes_waiting_turn", None) is not turn:
            return
        indicator = getattr(app, "listening_indicator", None)
        if indicator is not None:
            indicator.set_agent_waiting("Hermes", active)
        if not active:
            app._hermes_waiting_turn = None
    schedule(app, update)


def sound(app, cue):
    callback = getattr(app, "play_sound", None)
    if callback and level(app) != "silent":
        callback(cue)


def play(app, text, *, result=False, valid=lambda: True):
    """Recheck generation, capture and feedback level at actual delivery."""
    while valid():
        with getattr(app, "_hold_capture_lifecycle_lock", None) or nullcontext():
            if not capturing(app):
                if not valid():
                    return
                current = level(app)
                if current == "full":
                    audio = getattr(app, "audio_coordinator", None)
                    if audio:
                        handle = audio.speak(text, category="ava_response", interruptible=True)
                        app._hermes_speech = (audio, handle)
                elif current == "brief":
                    sound(app, "agent_response" if result else "error")
                return
        time.sleep(.05)


def notice(app, text, *, error=True):
    chip(app, text, "error" if error else "info")
    # Commands/refusals also wait until the command hold has ended.
    agent = channel(app)
    generation = agent._generation
    thread_registry.spawn("hermes.notice", lambda: play(app, text, result=not error, valid=lambda:
        not agent._closed and not cli_agents._closing.is_set() and agent._generation == generation))


def failure(exc):
    # PLACEHOLDER COPY (539).
    if isinstance(exc, FileNotFoundError):
        return "Hermes isn't installed on this computer."
    if isinstance(exc, TimeoutError):
        return "Hermes took too long; your conversation is kept."
    first = str(exc).splitlines()[0] if str(exc) else "No error detail was returned."
    return "Hermes couldn't answer: " + first[:180]


def ask(app, question):
    if not permitted(getattr(app, "config", {}) or {}):
        notice(app, "Enable Hermes in Settings and accept its notice first.")
        return DispatchState.FAILED
    if not question.strip():
        notice(app, "Ask Hermes what?")
        return DispatchState.FAILED
    agent = channel(app)
    try:
        turn = agent.begin()
    except BusyError as exc:
        notice(app, str(exc))
        return DispatchState.REJECTED
    except Cancelled:
        return DispatchState.REJECTED
    waiting(app, True, turn)
    sound(app, "agent_routing")

    def run():
        valid = lambda: agent.valid(turn) and not cli_agents._closing.is_set()
        try:
            result = agent.ask(question, app.config, turn=turn)
            if not valid():
                return
            waiting(app, False, turn)
            first = first_reply(app, result.text, source="hermes").spoken
            with agent._state_lock:
                if not valid():
                    return
                app._cli_agent_last = result
                app._cli_agent_voice_state = {"agent": "hermes", "turn": turn,
                    "last": first, "rest": result.text, "expires": time.monotonic() + 60}
            chip(app, first[:220] or "Answered", valid=valid)
            play(app, first, result=True, valid=valid)
        except Cancelled:
            pass
        except Exception as exc:
            if valid():
                waiting(app, False, turn)
                message = failure(exc)
                chip(app, message, "error", valid)
                play(app, message, valid=valid)
        finally:
            agent.finish(turn)
            waiting(app, False, turn)

    try:
        thread_registry.spawn("agent.hermes", run)
    except Exception:
        agent.finish(turn)
        waiting(app, False, turn)
        notice(app, "Hermes couldn't start its background turn.")
        return DispatchState.FAILED
    return DispatchState.QUEUED


def stop(app):
    channel(app).stop()
    cancel_readback(app)
    turn = getattr(app, "_hermes_waiting_turn", None)
    if turn:
        waiting(app, False, turn)
    if (getattr(app, "_cli_agent_voice_state", None) or {}).get("agent") == "hermes":
        app._cli_agent_voice_state = None
    notice(app, "Hermes stopped; your conversation is kept.", error=False)
    return True


def new(app, remainder=""):
    if not permitted(getattr(app, "config", {}) or {}):
        notice(app, "Enable Hermes in Settings and accept its notice first.")
        return DispatchState.FAILED
    words = remainder.strip().lower().rstrip(".?!").split()
    if words:
        if len(words) != 3 or words[0] != "with" or words[2] != "effort" or words[1] not in EFFORTS:
            notice(app, "Say new Hermes session with a listed effort level.")
            return DispatchState.FAILED
    channel(app).new_conversation()
    cancel_readback(app)
    turn = getattr(app, "_hermes_waiting_turn", None)
    if turn:
        waiting(app, False, turn)
    if (getattr(app, "_cli_agent_voice_state", None) or {}).get("agent") == "hermes":
        app._cli_agent_voice_state = None
        app._cli_agent_last = None
    if words:
        app.config.setdefault("hermes", {})["effort"] = words[1]
        save = getattr(app, "save_config", None)
        if save:
            save()
    notice(app, "New Hermes session ready.", error=False)
    return True


def cancel_readback(app):
    owned = getattr(app, "_hermes_speech", None)
    if owned:
        audio, handle = owned
        if handle is not None and getattr(audio, "_active_handle", None) is handle:
            audio.cancel_speech()
        app._hermes_speech = None


def followup(app, action, state):
    agent = channel(app)
    turn = state["turn"]
    if not agent.valid(turn):
        return False
    if action == "repeat that":
        text = state["last"]
    elif action == "read the rest":
        text, state["rest"] = spoken_form(state["rest"], 60)
        text = text or "That's all of the Hermes reply."
        state["last"] = text
    else:
        return False
    state["expires"] = time.monotonic() + 60
    chip(app, text[:220], valid=lambda: agent.valid(turn))
    thread_registry.spawn("hermes.readback", lambda: play(app, text, result=True,
        valid=lambda: agent.valid(turn) and not cli_agents._closing.is_set()))
    return True
