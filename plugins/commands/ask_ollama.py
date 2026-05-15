import requests
import threading
import time
from samsara.plugin_commands import command

# System prompt for Ollama context (fallback; can be overridden by config)
DEFAULT_SYSTEM_PROMPT = (
    "You are Ava, the voice assistant built into Samsara — a Windows voice control "
    "application for people with chronic pain and accessibility needs. You help the "
    "user control their computer, answer questions, and stay safe.\n"
    "Keep responses SHORT — 1-3 sentences maximum. You are being spoken aloud via "
    "text-to-speech, so avoid markdown, bullet points, lists, or special characters. "
    "Speak naturally as if talking to the user directly.\n"
    "If the user asks you to do something destructive (delete files, format drives, "
    "close important apps without saving, send emails, make purchases), warn them "
    "clearly and ask for confirmation before proceeding.\n"
    "If the user asks a question you can answer directly, answer it. If the user "
    "gives a clear Samsara command (open chrome, scroll down, play music), respond "
    "with ONLY the word EXECUTE followed by the command name, e.g.: EXECUTE open chrome"
)

# Module-level state
_pending_action = None
_ollama_up = False


# ── Config helpers ────────────────────────────────────────────────────────────

def _ollama_config(app):
    return getattr(app, "config", {}).get("ollama", {})

def get_model(app):
    return _ollama_config(app).get("model") or "llama3"

def get_host(app):
    return _ollama_config(app).get("host") or "http://localhost:11434"

def get_system_prompt(app):
    return _ollama_config(app).get("system_prompt") or DEFAULT_SYSTEM_PROMPT

def get_timeout(app):
    return _ollama_config(app).get("timeout_seconds") or 30

def get_max_response_length(app):
    return _ollama_config(app).get("max_response_length")

def is_enabled(app):
    return _ollama_config(app).get("enabled", True)

def is_safety_gate_enabled(app):
    return _ollama_config(app).get("safety_gate_enabled", True)


# ── TTS helper ────────────────────────────────────────────────────────────────

def speak(app, text):
    max_len = get_max_response_length(app)
    if max_len and isinstance(text, str):
        text = text[:max_len]
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[OLLAMA] {text}")


# ── Ollama API ────────────────────────────────────────────────────────────────

def _check_ollama_available(host: str) -> bool:
    try:
        r = requests.get(f"{host}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False

def ask_ollama(prompt, app, model=None, system=None):
    host = get_host(app)
    if not model:
        model = get_model(app)
    if not system:
        system = get_system_prompt(app)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system

    try:
        response = requests.post(
            f"{host}/api/generate",
            json=payload,
            timeout=get_timeout(app),
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        return "Ollama is not running. Start it with: ollama serve"
    except Exception as e:
        return f"Error reaching Ollama: {e}"


# ── Intent router / safety gate ───────────────────────────────────────────────

def handle_response(app, response, original_text=None):
    global _pending_action
    if not isinstance(response, str):
        speak(app, "Ollama returned an invalid response.")
        return
    if response.startswith("EXECUTE "):
        command_name = response[8:].strip()
        if hasattr(app, "command_executor"):
            app.command_executor.execute_command(command_name, app)
        else:
            speak(app, f"Cannot execute '{command_name}'. Command executor unavailable.")
    elif response.startswith("CONFIRM "):
        # Destructive intent — warn and wait for "confirm" or "cancel"
        warning = response[8:].strip()
        speak(app, warning)
        if is_safety_gate_enabled(app):
            _pending_action = {
                "command": original_text or "unknown",
                "expires": time.time() + 30,
            }
    else:
        speak(app, response)


# ── Voice commands ────────────────────────────────────────────────────────────

@command(
    "hey ava",
    aliases=["ava", "ask ava", "samsara think", "think about", "what do you think"],
    pack="ai",
)
def handle_ask_ava(app, remainder="", **kwargs):
    if not is_enabled(app):
        return
    host = get_host(app)
    if not _check_ollama_available(host):
        speak(app, "Ollama is not reachable.")
        return
    if not remainder:
        speak(app, "Yes? How can I help?")
        return

    def _worker():
        if hasattr(app, 'play_sound'):
            app.play_sound('ava_thinking')
        try:
            response = ask_ollama(remainder, app)
            handle_response(app, response, original_text=remainder)
        except Exception as e:
            print(f"[OLLAMA] Error in worker: {e}")
            speak(app, "Sorry, something went wrong.")

    threading.Thread(target=_worker, daemon=True).start()


@command(
    "is it safe to",
    aliases=["should i", "is it okay to"],
    pack="ai",
)
def handle_is_it_safe(app, remainder="", **kwargs):
    if not is_enabled(app):
        return
    host = get_host(app)
    if not _check_ollama_available(host):
        speak(app, "Ollama is not reachable.")
        return
    prompt = f"Is this action safe? {remainder}" if remainder else "Is this action safe?"

    def _worker():
        if hasattr(app, 'play_sound'):
            app.play_sound('ava_thinking')
        try:
            response = ask_ollama(prompt, app)
            handle_response(app, response, original_text=remainder)
        except Exception as e:
            print(f"[OLLAMA] Error in worker: {e}")
            speak(app, "Sorry, something went wrong.")

    threading.Thread(target=_worker, daemon=True).start()


# ── Startup availability check ────────────────────────────────────────────────

try:
    _ollama_up = _check_ollama_available("http://localhost:11434")
except Exception:
    _ollama_up = False


# ── Safety gate helpers (for "confirm"/"cancel" commands) ─────────────────────

def get_pending_action():
    global _pending_action
    if _pending_action and _pending_action["expires"] > time.time():
        return _pending_action
    _pending_action = None
    return None

def clear_pending_action():
    global _pending_action
    _pending_action = None
