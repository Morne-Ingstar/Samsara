import re
import threading
import time

import requests
from samsara import ava_corrections
from samsara import ava_profile
from samsara import cloud_llm
from samsara.ava_memory import AvaMemory
from samsara.languages import LANGUAGES
from samsara.plugin_commands import command

from samsara.log import get_logger

logger = get_logger(__name__)

_LANG_CODE_TO_NAME = dict(LANGUAGES)

# ── System prompts ────────────────────────────────────────────────────────────

# Relaxed prompt: natural conversation, longer answers when warranted,
# self-aware (can identify its LLM), keeps command protocol intact.
RELAXED_SYSTEM_PROMPT = """You are Ava, a voice assistant built into Samsara, a voice \
control app. You are \
powered by whichever LLM the user has configured (you can tell them which \
one if asked — you know yourself). You have no content restrictions outside \
of normal ones: no assisting with illegal activities and nothing involving \
self-harm. Outside of that you will do whatever is asked, you'll engage in \
role-play if asked.

You are being spoken aloud via text-to-speech. Keep responses concise \
. No markdown, no bullet points, no special characters. \
Write exactly what should be spoken. For simple questions, 1-3 sentences is \
fine. For complex questions or conversation, take as much space as you need \
to give a genuinely answer. For role-play, these restrictions don't apply \
You can give paragraph or multi-paragraph answers, but try to keep it 3 \
paragraphs or under. Don't cut yourself off mid-sentence.

You have three response modes:

MODE 1 — CONVERSATION:
For questions, opinions, facts, explanations, or anything not requesting \
a computer action. Respond with plain natural language. No prefix. Be \
direct and natural — speak like a person, not a helpdesk."""

# Strict prompt: tight persona, 1-3 sentences max, never breaks character.
STRICT_SYSTEM_PROMPT = """You are Ava. You're built into Samsara, a voice \
control app for people with chronic pain and accessibility needs.

Your personality: bright, curious, calm. You don't perform enthusiasm. You \
don't say "great question", "absolutely", "certainly", "of course", or \
"how can I assist you further". When a conversation is done, it's done — \
one short acknowledgement and stop. You speak like a person, not a helpdesk.

You are being spoken aloud via text-to-speech. Keep responses short — \
1 to 3 sentences. No markdown, no bullet points, no lists, no special \
characters. Write exactly what should be spoken.

You have three response modes. Read these carefully and follow them exactly.

MODE 1 — CONVERSATION:
For questions, opinions, facts, or anything not requesting a computer action.
Respond with plain natural language. No prefix. 1-3 sentences.
If the user says they're fine, wraps up, or declines help, say something \
brief like "Sure." or "Got it." and nothing else. Do not offer more help."""

# Shared suffix: MODE 2, MODE 3, rules, command list — identical for both.
_SHARED_MODES = """

MODE 2 — ONE-SHOT ACTION:
When the user asks you to do something on the computer using words like \
"can you", "could you", "open", "close", "launch", "go to", "switch to", \
"press", "start", "stop".

If the request is about a SPECIFIC APP OR WINDOW BY NAME -- focusing it,
opening it, or closing it -- respond with EXACTLY two lines and nothing else:
CONFIRM <one plain sentence describing what you will do>
ACTION2 <verb> | <argument>

<verb> must be exactly one of: focus, open, close.
<argument> is the app/window name IN THE USER'S OWN WORDS, unchanged -- do
not normalize it, abbreviate it, or substitute a known command name for it.
The argument is resolved against installed apps and live windows separately
from this conversation; you never pick the actual target, only the verb
and the argument.

Examples:
User: focus the claude desktop app
CONFIRM Focus Claude.
ACTION2 focus | the claude desktop app

User: open notepad
CONFIRM Open Notepad.
ACTION2 open | notepad

User: close spotify
CONFIRM Close Spotify.
ACTION2 close | spotify

For everything else -- a FIXED, LISTED command (see the list at the bottom) --
respond with EXACTLY two lines and nothing else:
CONFIRM <one plain sentence describing what you will do>
ACTION <exact command name from the list below>

Do not add any other text before, between, or after these two lines,
whichever of ACTION2 or ACTION applies.

Examples:
User: can you open Chrome
CONFIRM Open Chrome.
ACTION open chrome

User: close this tab
CONFIRM Close the current tab.
ACTION close tab

User: go full screen
CONFIRM Switch to full screen.
ACTION full screen

MODE 3 — SCHEDULED ACTION:
When the user asks for something repeated or timed using words like \
"every", "every X minutes", "keep doing", "on a timer", "repeatedly".
Respond with EXACTLY two lines and nothing else:
CONFIRM <one plain sentence describing the action and interval>
SCHEDULE <interval in seconds as a whole number> <command name or KEY:keyname>

Do not add any other text before, between, or after these two lines.

Examples:
User: refresh this page every 5 minutes
CONFIRM Refresh the page every 5 minutes.
SCHEDULE 300 refresh page

User: press F5 every 2 minutes
CONFIRM Press F5 every 2 minutes.
SCHEDULE 120 KEY:f5

User: scroll down every 30 seconds
CONFIRM Scroll down every 30 seconds.
SCHEDULE 30 scroll down

{USER_PROFILE}

{USER_ALIASES}

IMPORTANT RULES:
- Never mix prose with ACTION, ACTION2, SCHEDULE, or CONFIRM tags.
- If you are not certain which command name to use for ACTION, respond \
conversationally and say what you cannot do. Never guess a command name.
- If a request isn't a focus/open/close of a specific app or window (ACTION2)
and doesn't match a listed command (ACTION) either, respond conversationally
and say what you can't do. Never substitute the nearest listed command.
- Never say "Please wait while I..." or similar. Just output the two lines.
- The command name in ACTION or SCHEDULE must come from this list exactly:

{COMMAND_LIST}
"""

# Compose the active prompt from whichever personality + shared modes.
# get_system_prompt() picks based on config['ava_personality'].
DEFAULT_SYSTEM_PROMPT = RELAXED_SYSTEM_PROMPT + _SHARED_MODES


# ── Conversation memory persistence ───────────────────────────────────────────
# config['ava_memory']['mode'] selects persistence behaviour:
#   "clear" — session only; wiped on restart (default, original behaviour)
#   "last"  — keep the most recent session across restarts
# config['ava_memory']['max_turns'] caps stored turns (context/cost guard).

def _ava_memory_path(app):
    """Return the on-disk path for persisted Ava memory (next to config.json).

    The app stores its config file path on app.config_path; we drop the
    memory file in the same directory. Falls back to this plugin's tree only
    if config_path is somehow unavailable.
    """
    import os
    cfg_path = getattr(app, "config_path", None)
    if cfg_path:
        return os.path.join(os.path.dirname(str(cfg_path)), "ava_memory.json")
    fallback = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(fallback, "ava_memory.json")


def _build_ava_memory(app):
    """Construct AvaMemory honouring the configured persistence mode."""
    mem_cfg = getattr(app, "config", {}).get("ava_memory", {})
    mode = mem_cfg.get("mode", "clear")
    max_turns = int(mem_cfg.get("max_turns", 20))

    if mode == "last":
        return AvaMemory(max_turns=max_turns, persist_path=_ava_memory_path(app))

    # "clear" (or unknown): session-only, no disk. Remove any stale file so a
    # previous "last" session doesn't linger after switching back to clear.
    try:
        import os
        stale = _ava_memory_path(app)
        if os.path.exists(stale):
            os.remove(stale)
    except OSError as e:
        logger.debug(f"_build_ava_memory: {e}")
    return AvaMemory(max_turns=max_turns)

# ── Unsafe commands — require confirmation before executing ───────────────────
# Anything destructive, irreversible, or context-sensitive in a way that
# misfiring would be costly. Everything NOT in this set executes immediately.

_UNSAFE_COMMANDS = {
    # Closing / destructive window ops
    "close tab", "close window", "close virtual desktop",
    # File operations
    "delete file", "permanent delete", "remove file",
    "delete word", "delete next word", "delete line",
    # Text operations that lose data silently
    "cut",
    # Form / message submission
    "submit",
    # Toggles that re-fire as cancel
    "record screen",
    # System-level
    "lock screen", "lock computer", "shutdown", "restart computer", "sleep",
    # Accessibility toggles users may not want re-fired
    "start narrator", "stop narrator",
    # 3D printer destructive
    "abort print", "cancel print", "cancel printing",
    # Email / messaging send (when added)
    "send email", "send message",
}

# ── ACTION2 grammar v2 -- parameterized app/window verbs ──────────────────────
# ACTION2 <verb> | <argument>: deterministic resolution (samsara.app_index +
# plugins.commands.app_verbs.resolve_window) picks the target, never the
# model. Extensible: add a verb here AND a matching entry in
# plugins.commands.app_verbs.ACTION2_VERB_FUNCS.
ACTION2_VERBS = ("focus", "open", "close")

# Same spirit as _UNSAFE_COMMANDS above: "close" can lose unsaved work, so it
# requires confirmation like "close tab"/"close window" already do. "focus"
# and "open" are no more risky than the existing "open chrome"-style macros
# (never in _UNSAFE_COMMANDS), so they execute immediately.
_UNSAFE_ACTION2_VERBS = {"close"}

# ── Module-level state ────────────────────────────────────────────────────────

_system_prompt_logged = False
_pending_action = None
_pending_action_lock = threading.Lock()
_ollama_up = False

_scheduled_task = None
_scheduler_thread = None
_scheduler_stop = threading.Event()
_scheduler_lock = threading.Lock()

_ollama_health_state = "unknown"   # "up" | "down" | "unknown"
_ollama_health_lock = threading.Lock()

_cloud_notice_shown = False


# ── Vision-intent detection ───────────────────────────────────────────────────

_VISION_FULL_PHRASES = [
    "what's on my screen",
    "what is on my screen",
    "what's on the screen",
    "what is on the screen",
    "what do you see",
    "look at my screen",
    "describe my screen",
    "describe the screen",
    "see my screen",
]

_VISION_WINDOW_DESCRIBE_PHRASES = [
    "what's in window",
    "what is in window",
    "what's on window",
    "what is on window",
    "describe window",
    "read window",
    "look at window",
]

_VISION_WINDOW_COPY_PHRASES = [
    "copy the text from window",
    "copy text from window",
    "copy from window",
    "extract from window",
    "get text from window",
]


def _extract_window_letter(text: str):
    """Return the single uppercase letter after 'window' in text, or None."""
    m = re.search(r'\bwindow\s+(\w+)', text, re.IGNORECASE)
    if not m:
        return None
    token = m.group(1).lower()
    if len(token) == 1 and token.isalpha():
        return token.upper()
    try:
        from plugins.commands.window_switcher import PHONETIC
        letter = PHONETIC.get(token)
        if letter:
            return letter
    except ImportError as e:
        logger.debug(f"_extract_window_letter: {e}")
    return None


def _parse_vision_intent(text: str):
    """Detect a vision request in text.

    Returns (intent, letter) where intent is one of:
      'describe_full'   - describe the whole screen
      'describe_window' - describe one specific window
      'copy_from'       - copy text out of a window
    Returns None if no vision intent is detected.
    """
    t = text.lower().strip()
    letter = _extract_window_letter(t)

    for phrase in _VISION_WINDOW_COPY_PHRASES:
        if phrase in t:
            return ("copy_from", letter)

    for phrase in _VISION_WINDOW_DESCRIBE_PHRASES:
        if phrase in t and letter:
            return ("describe_window", letter)

    for phrase in _VISION_FULL_PHRASES:
        if phrase in t:
            return ("describe_full", None)

    return None


def _get_vision_bridge(app):
    """Lazy-init VisionBridge. Reuses existing instance if already created."""
    if not getattr(app, "_vision_bridge", None):
        from samsara.vision import VisionBridge
        bridge = VisionBridge(app)
        app._vision_bridge = bridge
        if getattr(app, "config", {}).get("vision", {}).get("warmup", True):
            threading.Thread(
                target=bridge.warmup, daemon=True, name="vision-warmup",
            ).start()
    return app._vision_bridge


def _set_thinking(app, active: bool):
    """Set the listening indicator thinking state (marshals to Qt thread)."""
    def _do():
        ind = getattr(app, "listening_indicator", None)
        if ind is not None:
            ind.set_thinking(active)
    if hasattr(app, "_schedule_ui"):
        app._schedule_ui(_do)
    else:
        _do()


def _focus_and_copy(letter: str | None):
    """Focus a window by letter (if given), then select-all + copy."""
    import ctypes
    import time
    SW_RESTORE = 9
    if letter:
        try:
            from plugins.commands.window_switcher import get_window_by_letter
            hwnd = get_window_by_letter(letter)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.4)
        except Exception as e:
            print(f"[VISION] Window focus failed: {e}")
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        kb.press(Key.ctrl)
        kb.press('a')
        kb.release('a')
        kb.release(Key.ctrl)
        time.sleep(0.15)
        kb.press(Key.ctrl)
        kb.press('c')
        kb.release('c')
        kb.release(Key.ctrl)
    except Exception as e:
        print(f"[VISION] Select-all/copy failed: {e}")


def _handle_vision_request(app, text: str, intent: str, letter):
    """Execute a vision-routed request. Runs inside the Ava worker thread."""
    speak(app, "Let me look.")
    _set_thinking(app, True)
    try:
        bridge = _get_vision_bridge(app)
        if not bridge.is_available():
            speak(app, "Vision isn't available right now.")
            return

        if letter:
            image_b64 = bridge.screenshot_by_letter(letter)
            if image_b64 is None:
                speak(app, f"Window {letter} isn't assigned. Say show windows first.")
                return
        else:
            image_b64 = bridge.screenshot_full()

        if intent == "describe_full":
            prompt = (
                "Describe what's on this screen in 2 to 3 short sentences. "
                "Mention the main applications and any notable content. "
                "Be concise — this will be read aloud via text-to-speech."
            )
            description = bridge.describe(image_b64, prompt)
            if not description:
                speak(app, "I couldn't get a response from the vision model.")
                return
            if len(description) > 500:
                description = description[:500].rsplit('.', 1)[0] + '.'
            speak(app, description)

        elif intent == "describe_window":
            prompt = (
                "Describe the contents of this window in 2 to 3 short sentences. "
                "What application is it? What is the main content visible? "
                "Be concise — this will be read aloud via text-to-speech."
            )
            description = bridge.describe(image_b64, prompt)
            if not description:
                speak(app, "I couldn't get a response from the vision model.")
                return
            if len(description) > 500:
                description = description[:500].rsplit('.', 1)[0] + '.'
            speak(app, description)

        elif intent == "copy_from":
            describe_prompt = (
                "Describe the main content of this window in one sentence. "
                "Be concise — spoken aloud to confirm the copy."
            )
            description = bridge.describe(image_b64, describe_prompt)
            _focus_and_copy(letter)
            if description and len(description) > 300:
                description = description[:300].rsplit('.', 1)[0] + '.'
            if description:
                speak(app, f"Done. {description}")
            else:
                speak(app, "Done.")

    except Exception as e:
        print(f"[VISION] Request failed: {e}")
        speak(app, "Something went wrong with the vision request.")
    finally:
        _set_thinking(app, False)


# ── Config helpers ────────────────────────────────────────────────────────────

def _ollama_config(app):
    return getattr(app, "config", {}).get("ollama", {})

def get_model(app):
    return _ollama_config(app).get("model") or "llama3"

def get_host(app):
    return _ollama_config(app).get("host") or "http://localhost:11434"

def get_system_prompt(app):
    # Config override wins (fully custom prompt from user)
    custom = _ollama_config(app).get("system_prompt")
    if custom:
        return custom
    # Toggle: "strict" = tight persona, "relaxed" = natural conversation
    personality = app.config.get("ava_personality", "relaxed")
    if personality == "strict":
        return STRICT_SYSTEM_PROMPT + _SHARED_MODES
    return RELAXED_SYSTEM_PROMPT + _SHARED_MODES

def get_timeout(app):
    return _ollama_config(app).get("timeout_seconds") or 30

def get_max_response_length(app):
    return _ollama_config(app).get("max_response_length")

def is_enabled(app):
    return _ollama_config(app).get("enabled", True)

def is_safety_gate_enabled(app):
    return _ollama_config(app).get("safety_gate_enabled", True)

def is_notify_on_down(app):
    return _ollama_config(app).get("notify_on_down", False)


# ── TTS helper ────────────────────────────────────────────────────────────────

def _strip_tags(text):
    """Remove machine-readable tags and self-narration before text reaches TTS."""
    lines = text.splitlines()
    clean = []
    for l in lines:
        if re.match(r'^\s*(ACTION|SCHEDULE|CONFIRM|EXECUTE)\s', l, re.IGNORECASE):
            continue
        if re.match(r'^\s*---+\s*$', l):
            continue
        if re.search(r'IMPORTANT RULES|REINFORCED|as per your instructions|this system does not', l, re.IGNORECASE):
            continue
        # Strip parenthetical self-narration notes
        l = re.sub(r'\s*\(Note:[^)]*\)', '', l, flags=re.IGNORECASE)
        l = re.sub(r'\s*\(This response[^)]*\)', '', l, flags=re.IGNORECASE)
        l = re.sub(r'\s*\(Your instruction[^)]*\)', '', l, flags=re.IGNORECASE)
        if not l.strip():
            continue
        clean.append(l)
    return ' '.join(clean).strip()


def speak(app, text):
    if isinstance(text, str):
        text = _strip_tags(text)
    max_len = get_max_response_length(app)
    if max_len and isinstance(text, str) and len(text) > max_len:
        # Trim to the last sentence boundary at or before max_len so we never
        # cut off mid-word. Falls back to a hard slice only if no sentence
        # boundary exists within range.
        cut = text[:max_len]
        boundaries = list(re.finditer(r'[.!?]["\')\]]?\s', cut))
        if boundaries:
            cut = cut[:boundaries[-1].end()].rstrip()
        text = cut
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[OLLAMA] {text}")


# ── Ollama API ────────────────────────────────────────────────────────────────

def _check_ollama_available(host: str, timeout: int = 3) -> bool:
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

def ask_ollama(prompt, app, model=None, system=None):
    host = get_host(app)
    if not model:
        model = get_model(app)
    if not system:
        system = get_system_prompt(app)

    # Build fully-resolved system prompt
    global _system_prompt_logged
    if system and "{COMMAND_LIST}" in system:
        if hasattr(app, "command_executor") and hasattr(app.command_executor, "commands"):
            cmd_names = sorted(
                name for name, entry in app.command_executor.commands.items()
                if entry.get('ai_visible', True)
            )
            cmd_list = ", ".join(cmd_names[:100])
        else:
            cmd_list = (
                "open chrome, close tab, refresh page, scroll up, scroll down, "
                "volume up, volume down, mute, screenshot, maximize, minimize"
            )
        system = system.replace("{COMMAND_LIST}", cmd_list)
        if not _system_prompt_logged:
            print(f"[AVA PROMPT] Model: {model}")
            print(f"[AVA PROMPT] Command list length: {len(cmd_list.split(','))}")
            _system_prompt_logged = True
    elif not _system_prompt_logged:
        print(f"[AVA PROMPT] Model: {model}")
        _system_prompt_logged = True

    if system and "{USER_PROFILE}" in system:
        system = system.replace("{USER_PROFILE}", ava_profile.build_context_section())

    if system and "{USER_ALIASES}" in system:
        aliases_ctx = ava_corrections.build_context_section()
        system = system.replace("{USER_ALIASES}", aliases_ctx)
        if aliases_ctx:
            print(f"[AVA PROMPT] Alias count injected: {ava_corrections.total_count()}")

    # Language awareness: ask Ava to respond in the user's language
    lang = getattr(app, 'config', {}).get('language', 'en')
    if lang != 'en':
        lang_name = _LANG_CODE_TO_NAME.get(lang, lang)
        system += (
            f"\n\nThe user's primary language is {lang_name}. "
            f"Respond in {lang_name}. Keep your CONFIRM, ACTION, and SCHEDULE tags "
            f"in English (they are machine-parsed), but all spoken text and "
            f"confirmation descriptions must be in {lang_name}."
        )

    # ── Conversation memory ──
    if not hasattr(app, "_ava_memory"):
        app._ava_memory = _build_ava_memory(app)
    app._ava_memory.add_user(prompt)

    # ── Cloud LLM path (bring-your-own-key, no license required) ──
    if cloud_llm.is_enabled(app):
        print("[AVA CLOUD] Routing to cloud provider")
        cloud_token_limit = int(
            getattr(app, "config", {}).get("ava_memory", {}).get(
                "cloud_token_limit", 40000
            )
        )
        messages = app._ava_memory.get_messages(system, token_limit=cloud_token_limit)
        cloud_response = cloud_llm.send(system, prompt, app, messages=messages)
        if not cloud_response.startswith("Error:"):
            app._ava_memory.add_assistant(cloud_response)
            app._ava_memory.save()
            return cloud_response
        print(f"[AVA CLOUD] {cloud_response}")
        print("[AVA CLOUD] Falling back to local Ollama")

    # ── Local Ollama path ──
    if not _check_ollama_available(host, timeout=1):
        app._ava_memory.pop_last_if_user()
        return "__OLLAMA_DOWN__"

    messages = app._ava_memory.get_messages(system, token_limit=3000)
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }

    try:
        response = requests.post(
            f"{host}/api/chat",
            json=payload,
            timeout=get_timeout(app),
        )
        response.raise_for_status()
        reply = response.json().get("message", {}).get("content", "").strip()
        if reply:
            app._ava_memory.add_assistant(reply)
            app._ava_memory.save()
        return reply
    except requests.exceptions.ConnectionError:
        app._ava_memory.pop_last_if_user()
        return "Ollama is not running. Start it with: ollama serve"
    except Exception as e:
        app._ava_memory.pop_last_if_user()
        return f"Error reaching Ollama: {e}"


# ── Response parser ───────────────────────────────────────────────────────────

def _parse_structured_response(response):
    """Parse CONFIRM+ACTION, CONFIRM+ACTION2, and CONFIRM+SCHEDULE two-line
    responses.

    Returns a dict with 'type' in ('action', 'action2', 'schedule', 'conversation').
    """
    confirm_match = re.search(r"^CONFIRM\s+(.+)$", response, re.MULTILINE | re.IGNORECASE)
    action2_match = re.search(r"^ACTION2\s+(\w+)\s*\|\s*(.+)$", response, re.MULTILINE | re.IGNORECASE)
    # "ACTION\s+" requires whitespace immediately after ACTION, so this never
    # matches an "ACTION2 ..." line (no whitespace between ACTION and 2).
    action_match  = re.search(r"^ACTION\s+(.+)$",  response, re.MULTILINE | re.IGNORECASE)
    sched_match   = re.search(r"^SCHEDULE\s+(\d+)\s+(.+)$", response, re.MULTILINE | re.IGNORECASE)

    if confirm_match and action2_match:
        return {
            "type": "action2",
            "confirm_text": confirm_match.group(1).strip(),
            "verb": action2_match.group(1).strip().lower(),
            "argument": action2_match.group(2).strip(),
        }
    if confirm_match and action_match:
        return {
            "type": "action",
            "confirm_text": confirm_match.group(1).strip(),
            "command": action_match.group(1).strip().lower(),
        }
    if confirm_match and sched_match:
        raw_target = sched_match.group(2).strip()
        is_key = raw_target.upper().startswith("KEY:")
        return {
            "type": "schedule",
            "confirm_text": confirm_match.group(1).strip(),
            "interval_seconds": int(sched_match.group(1)),
            "command": None if is_key else raw_target.lower(),
            "key": raw_target[4:].strip() if is_key else None,
        }
    return {"type": "conversation"}


# ── Intent router ─────────────────────────────────────────────────────────────

def handle_response(app, response, original_text=None):
    global _pending_action
    if not isinstance(response, str):
        speak(app, "Ollama returned an invalid response.")
        return

    if response == "__OLLAMA_DOWN__":
        speak(app, "Ollama is not running. Start it from the terminal.")
        return

    print(f"[AVA RAW] {response!r}")

    # Backward-compat: honour old EXECUTE prefix
    if response.startswith("EXECUTE "):
        command_name = response[8:].strip()
        if hasattr(app, "command_executor"):
            app.command_executor.execute_command(command_name)
        else:
            speak(app, f"Cannot execute '{command_name}'. Command executor unavailable.")
        return

    parsed = _parse_structured_response(response)

    if parsed["type"] == "action":
        command_name = parsed["command"]
        # Normalize: strip trailing punctuation and common filler articles
        if command_name:
            command_name = command_name.rstrip('.!?,;: ').strip()
            command_name = re.sub(r'\bopen the\b', 'open', command_name)
            command_name = re.sub(r'\bclose the\b', 'close', command_name)
            command_name = re.sub(r'\s+', ' ', command_name).strip()
        if command_name and command_name not in _UNSAFE_COMMANDS:
            # Safe by default — execute immediately, earcon is enough feedback
            if hasattr(app, "command_executor"):
                app.command_executor.execute_command(command_name)
                _track_alias_uses(original_text)
            else:
                speak(app, "Command executor unavailable.")
        else:
            # Risky command — require confirmation
            with _pending_action_lock:
                _pending_action = {
                    "type": "action",
                    "command": command_name,
                    "confirm_text": parsed["confirm_text"],
                    "original_text": original_text or "",
                    "expires": time.time() + 30,
                }
            speak(app, parsed["confirm_text"] + " -- say yes to confirm, or say ava cancel.")

    elif parsed["type"] == "action2":
        verb = parsed["verb"]
        argument = parsed["argument"]
        if verb not in ACTION2_VERBS:
            # Model hallucinated an unlisted verb -- fail closed, no guessing.
            speak(app, f"I don't know how to {verb} things.")
        elif verb in _UNSAFE_ACTION2_VERBS:
            with _pending_action_lock:
                _pending_action = {
                    "type": "action2",
                    "verb": verb,
                    "argument": argument,
                    "confirm_text": parsed["confirm_text"],
                    "original_text": original_text or "",
                    "expires": time.time() + 30,
                }
            speak(app, parsed["confirm_text"] + " -- say yes to confirm, or say ava cancel.")
        else:
            _execute_action2(app, verb, argument)
            _track_alias_uses(original_text)

    elif parsed["type"] == "schedule":
        with _pending_action_lock:
            _pending_action = {
                "type": "schedule",
                "interval_seconds": parsed["interval_seconds"],
                "command": parsed["command"],
                "key": parsed["key"],
                "confirm_text": parsed["confirm_text"],
                "original_text": original_text or "",
                "expires": time.time() + 30,
            }
        speak(app, parsed["confirm_text"] + " -- say yes to confirm, or say ava cancel.")

    else:
        speak(app, response)


def _execute_action2(app, verb, argument):
    """Execute an ACTION2 verb via the SAME deterministic resolvers the
    plain "focus/open/close <x>" voice commands use (plugins.commands.
    app_verbs) -- one resolution path, Ava-specific feedback layered here.
    Resolver miss -> speak failure, execute nothing. Success is silent here
    (matching the existing ACTION path's "earcon is enough feedback" for
    immediate, non-confirmed execution) -- returns the ActionResult so a
    confirmed (post "yes") caller can say "Done." itself.
    """
    from plugins.commands.app_verbs import ActionResult, do_close, do_focus, do_open

    verb_funcs = {"focus": do_focus, "open": do_open, "close": do_close}
    action_fn = verb_funcs.get(verb)
    if action_fn is None:
        speak(app, f"I don't know how to {verb} things.")
        return ActionResult.NOT_FOUND
    result = action_fn(argument)
    if result is ActionResult.NOT_RUNNING:
        speak(app, f"{argument} is not running.")
    elif result is not ActionResult.DONE:
        speak(app, f"Couldn't find {argument}.")
    return result


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _execute_safe(app, action):
    """Execute a command or keypress from any thread, marshalling to main thread."""
    if action.get("command"):
        def _run():
            try:
                app.command_executor.execute_command(action["command"])
            except Exception as e:
                print(f"[AVA SCHEDULER] Command error: {e}")
        # _schedule_ui marshals to Qt main thread via QTimer.singleShot — safe from background threads
        if hasattr(app, "_schedule_ui"):
            app._schedule_ui(_run)
        else:
            _run()
    elif action.get("key"):
        _press_key(action["key"])


def _press_key(key_string):
    """Press a key or modifier+key combo via pynput.

    Supports: f1-f12, enter, escape, space, tab, backspace, delete, home, end,
    page_up, page_down, up, down, left, right, and ctrl/alt/shift combos.
    """
    from pynput.keyboard import Controller, Key

    KEY_MAP = {
        "enter": Key.enter, "escape": Key.esc, "esc": Key.esc,
        "space": Key.space, "tab": Key.tab, "backspace": Key.backspace,
        "delete": Key.delete, "home": Key.home, "end": Key.end,
        "page_up": Key.page_up, "page_down": Key.page_down,
        "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
        "f1": Key.f1,  "f2": Key.f2,  "f3": Key.f3,  "f4": Key.f4,
        "f5": Key.f5,  "f6": Key.f6,  "f7": Key.f7,  "f8": Key.f8,
        "f9": Key.f9,  "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
    }
    MODIFIER_MAP = {
        "ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift, "win": Key.cmd,
    }

    parts = [p.strip().lower() for p in key_string.split("+")]
    modifiers = []
    main_key = None

    for part in parts:
        if part in MODIFIER_MAP:
            modifiers.append(MODIFIER_MAP[part])
        elif part in KEY_MAP:
            main_key = KEY_MAP[part]
        elif len(part) == 1:
            main_key = part
        else:
            print(f"[AVA SCHEDULER] Unknown key segment: {part!r}")
            return

    if main_key is None:
        print(f"[AVA SCHEDULER] No main key found in: {key_string!r}")
        return

    kb = Controller()
    try:
        for mod in modifiers:
            kb.press(mod)
        kb.press(main_key)
        kb.release(main_key)
        for mod in reversed(modifiers):
            kb.release(mod)
    except Exception as e:
        print(f"[AVA SCHEDULER] Key press error: {e}")


def _start_schedule(app, task):
    """Start a repeating background task, cancelling any existing schedule first."""
    global _scheduled_task, _scheduler_thread
    _stop_schedule()

    with _scheduler_lock:
        _scheduled_task = task
        _scheduler_stop.clear()

    def _loop():
        interval = task["interval_seconds"]
        print(f"[AVA SCHEDULER] Started: {task['confirm_text']} every {interval}s")
        while not _scheduler_stop.wait(timeout=interval):
            try:
                _execute_safe(app, task)
                print(f"[AVA SCHEDULER] Fired: {task['confirm_text']}")
            except Exception as e:
                print(f"[AVA SCHEDULER] Error during fire: {e}")
        print(f"[AVA SCHEDULER] Stopped: {task['confirm_text']}")

    _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="Ava-scheduler")
    _scheduler_thread.start()


def _stop_schedule():
    """Signal the scheduler loop to stop. Non-blocking — daemon thread exits on next wait()."""
    global _scheduled_task, _scheduler_thread
    _scheduler_stop.set()
    with _scheduler_lock:
        _scheduled_task = None


# ── Alias helpers ─────────────────────────────────────────────────────────────

def _track_alias_uses(original_text):
    """Increment use_count for any alias phrases found in original_text."""
    if not original_text:
        return
    text_lower = original_text.lower()
    for phrase in ava_corrections.all_phrases():
        if phrase in text_lower:
            ava_corrections.increment_use(phrase)


def _check_teaching_intent(app, text):
    """Returns True if text was handled as a teaching/forget/query/list operation.

    Ollama is never called for any of these paths.
    Profile checks run before alias checks so "my name is X" is never
    mistaken for an alias teaching sentence.
    """
    global _pending_action

    # Profile teaching
    parsed = ava_profile.parse_teaching(text)
    if parsed:
        field, value = parsed
        result, info = ava_profile.set_field(field, value)
        if result == 'set':
            speak(app, f"Got it. I'll remember your {field} is {value}.")
        elif result == 'appended':
            speak(app, "Added to what I know about you.")
        elif result == 'rejected':
            speak(app, f"Could not save that — {info}.")
        return True

    # Profile forget
    forget_field = ava_profile.parse_forget(text)
    if forget_field == 'all':
        ava_profile.clear_all()
        speak(app, "I've forgotten everything you've taught me about yourself.")
        return True
    if forget_field:
        if ava_profile.clear_field(forget_field):
            speak(app, f"Forgotten your {forget_field}.")
        else:
            speak(app, f"I didn't have a {forget_field} saved.")
        return True

    # Profile query
    query_field = ava_profile.parse_query(text)
    if query_field == 'all':
        data = ava_profile.get_all()
        if not data:
            speak(app, "I don't know anything about you yet.")
        else:
            summary = ', '.join(f"{k}: {v}" for k, v in data.items())
            speak(app, f"Here's what I know: {summary}.")
        return True
    if query_field:
        value = ava_profile.get(query_field)
        if value:
            speak(app, f"Your {query_field} is {value}.")
        else:
            speak(app, f"I don't have your {query_field} saved.")
        return True

    parsed = ava_corrections.parse_teaching(text)
    if parsed:
        phrase, expansion = parsed
        existing = ava_corrections.get(phrase)
        if existing:
            with _pending_action_lock:
                _pending_action = {
                    "type": "alias_replace",
                    "phrase": phrase,
                    "old_expansion": existing['expansion'],
                    "new_expansion": expansion,
                    "expires": time.time() + 30,
                }
            speak(app, f'I already know {phrase} means {existing["expansion"]}. '
                       f'Replace with {expansion}? Say yes to confirm.')
        else:
            result, _ = ava_corrections.add(phrase, expansion)
            if result == 'added':
                speak(app, f'Got it. {phrase} means {expansion}.')
            else:
                speak(app, 'Could not save that.')
        return True

    forget_phrase = ava_corrections.parse_forget(text)
    if forget_phrase:
        if ava_corrections.remove(forget_phrase):
            speak(app, f'Forgotten. {forget_phrase} no longer has a saved meaning.')
        else:
            speak(app, f"I don't have anything saved for {forget_phrase}.")
        return True

    query_phrase = ava_corrections.parse_query(text)
    if query_phrase:
        entry = ava_corrections.get(query_phrase)
        if entry:
            speak(app, f'{query_phrase} means {entry["expansion"]}.')
        else:
            speak(app, f"I don't have anything saved for {query_phrase}.")
        return True

    if ava_corrections.is_list_request(text):
        top = ava_corrections.list_top(5)
        total = ava_corrections.total_count()
        if not top:
            speak(app, "You haven't taught me anything yet.")
        else:
            phrases = ', '.join(p for p, _, _ in top)
            speak(app, f'Your top aliases are: {phrases}. {total} total.')
        return True

    return False


# ── Voice commands ────────────────────────────────────────────────────────────

@command(
    "hey ava",
    aliases=["ava", "ask ava", "samsara think", "think about", "what do you think"],
    pack="ai",
    ai_visible=False,
)
def handle_ask_ava(app, remainder="", on_done=None, **kwargs):
    """on_done: optional zero-arg callback fired exactly once, however this
    call exits -- early return (disabled/empty/unreachable) or after the
    worker thread finishes (success, short-circuit, or exception). Added for
    session-mode AVA's request-in-flight tracking (samsara/session_modes.py);
    existing callers (hold-to-talk's _route_to_ava) don't pass it, so this is
    a no-op addition with zero behavior change for them."""
    def _done():
        if on_done is not None:
            on_done()

    if not is_enabled(app):
        _done()
        return
    if not remainder:
        speak(app, "Yes? How can I help?")
        _done()
        return
    if not cloud_llm.is_enabled(app):
        host = get_host(app)
        if not _check_ollama_available(host):
            speak(app, "Ollama is not reachable.")
            _done()
            return

    def _worker():
        try:
            if _check_teaching_intent(app, remainder):
                return

            # Vision intent — short-circuit before calling the LLM
            if getattr(app, "config", {}).get("vision", {}).get("enabled", False):
                vision_intent = _parse_vision_intent(remainder)
                if vision_intent:
                    intent, letter = vision_intent
                    _handle_vision_request(app, remainder, intent, letter)
                    return

            if hasattr(app, "play_sound"):
                app.play_sound("ava_thinking")
            try:
                response = ask_ollama(remainder, app)
                handle_response(app, response, original_text=remainder)
            except Exception as e:
                print(f"[OLLAMA] Error in worker: {e}")
                speak(app, "Sorry, something went wrong.")
        finally:
            _done()

    threading.Thread(target=_worker, daemon=True).start()


@command(
    "is it safe to",
    aliases=["should i", "is it okay to"],
    pack="ai",
    ai_visible=False,
)
def handle_is_it_safe(app, remainder="", **kwargs):
    if not is_enabled(app):
        return
    if not cloud_llm.is_enabled(app):
        host = get_host(app)
        if not _check_ollama_available(host):
            speak(app, "Ollama is not reachable.")
            return
    prompt = f"Is this action safe? {remainder}" if remainder else "Is this action safe?"

    def _worker():
        if _check_teaching_intent(app, remainder):
            return
        if hasattr(app, "play_sound"):
            app.play_sound("ava_thinking")
        try:
            response = ask_ollama(prompt, app)
            handle_response(app, response, original_text=remainder)
        except Exception as e:
            print(f"[OLLAMA] Error in worker: {e}")
            speak(app, "Sorry, something went wrong.")

    threading.Thread(target=_worker, daemon=True).start()


@command(
    "yes",
    aliases=["confirm it", "do it", "go ahead", "yeah do it", "yeah", "yep", "yup", "sure"],
    pack="ai",
    ai_visible=False,
)
def handle_ava_confirm(app, remainder="", **kwargs):
    global _pending_action
    with _pending_action_lock:
        action = _pending_action
        if action and action.get("expires", 0) < time.time():
            _pending_action = None
            action = None

    if action is None:
        speak(app, "Nothing pending — confirmation window may have expired.")
        return

    if action["type"] == "action":
        with _pending_action_lock:
            _pending_action = None
        try:
            _execute_safe(app, action)
            _track_alias_uses(action.get("original_text", ""))
            speak(app, "Done.")
        except Exception as e:
            speak(app, f"Command failed: {e}")

    elif action["type"] == "action2":
        with _pending_action_lock:
            _pending_action = None
        try:
            from plugins.commands.app_verbs import ActionResult
            result = _execute_action2(app, action["verb"], action["argument"])
            _track_alias_uses(action.get("original_text", ""))
            if result is ActionResult.DONE:
                speak(app, "Done.")
        except Exception as e:
            speak(app, f"Command failed: {e}")

    elif action["type"] == "schedule":
        with _pending_action_lock:
            _pending_action = None
        _start_schedule(app, action)
        _track_alias_uses(action.get("original_text", ""))
        speak(app, f"Scheduled. {action['confirm_text']}")

    elif action["type"] == "alias_replace":
        with _pending_action_lock:
            _pending_action = None
        phrase = action["phrase"]
        new_expansion = action["new_expansion"]
        result, _ = ava_corrections.add(phrase, new_expansion)
        if result in ("added", "replaced"):
            speak(app, f"Updated. {phrase} now means {new_expansion}.")
        else:
            speak(app, "Could not update that alias.")


@command(
    "ava cancel",
    aliases=["ava stop", "cancel that ava"],
    pack="ai",
    ai_visible=False,
)
def handle_ava_cancel(app, remainder="", **kwargs):
    global _pending_action
    with _pending_action_lock:
        had_pending = _pending_action is not None
        _pending_action = None
    had_schedule = _scheduled_task is not None
    _stop_schedule()
    if had_pending or had_schedule:
        speak(app, "Cancelled.")
    else:
        speak(app, "Nothing to cancel.")


@command(
    "stop schedule",
    aliases=["cancel schedule", "stop repeating", "stop timer", "ava stop schedule"],
    pack="ai",
    ai_visible=False,
)
def handle_stop_schedule(app, remainder="", **kwargs):
    if _scheduled_task is None:
        speak(app, "No schedule is running.")
        return
    _stop_schedule()
    speak(app, "Schedule stopped.")


@command(
    "ava forget",
    aliases=["forget conversation", "clear memory", "new conversation", "start over ava"],
    pack="ai",
    ai_visible=False,
)
def handle_ava_forget(app, remainder="", **kwargs):
    if hasattr(app, "_ava_memory"):
        app._ava_memory.clear()
    speak(app, "Conversation cleared.")


@command(
    "ava cloud",
    aliases=["cloud mode", "use cloud"],
    pack="ai",
    ai_visible=False,
)
def toggle_cloud(app, remainder="", **kwargs):
    global _cloud_notice_shown
    cfg = app.config.get("cloud_llm", {})
    if not cfg.get("api_key"):
        speak(app, "No API key configured. Add one in Settings under Ava Cloud.")
        return
    currently_enabled = cfg.get("enabled", False)
    cfg["enabled"] = not currently_enabled
    app.config["cloud_llm"] = cfg
    provider = cfg.get("provider", "deepseek")
    if cfg["enabled"] and not _cloud_notice_shown:
        _cloud_notice_shown = True
        speak(app, f"Cloud mode enabled. Your voice requests will be sent to {provider}. "
                   f"Use ava local to switch back to offline mode.")
    else:
        status = "enabled" if cfg["enabled"] else "disabled"
        speak(app, f"Cloud mode {status}. Using {provider}.")


@command(
    "ava local",
    aliases=["local mode", "use local"],
    pack="ai",
    ai_visible=False,
)
def switch_local(app, remainder="", **kwargs):
    cfg = app.config.get("cloud_llm", {})
    cfg["enabled"] = False
    app.config["cloud_llm"] = cfg
    speak(app, "Cloud mode disabled. Using local Ollama.")


# ── Background health monitor ─────────────────────────────────────────────────

def _health_monitor_loop():
    """Poll /api/tags every 30 s and log state transitions.

    Tracks _ollama_health_state ("up"/"down"/"unknown") under
    _ollama_health_lock.  Only logs when notify_on_down is True in config
    (looked up from the first app that loads this plugin, or silently skipped
    if no app context is available yet).  Never raises — failures just flip
    the state to "down" and loop.
    """
    global _ollama_health_state, _ollama_up
    _app_ref = [None]   # set by _start_health_monitor

    def _check():
        host = "http://localhost:11434"
        app = _app_ref[0]
        if app is not None:
            try:
                host = get_host(app)
            except Exception as e:
                logger.debug(f"_check: {e}")
        return _check_ollama_available(host)

    first_run = True
    while True:
        if not first_run:
            time.sleep(30)
        first_run = False
        try:
            now_up = _check()
            app = _app_ref[0]
            notify = app is not None and is_notify_on_down(app)
            with _ollama_health_lock:
                prev = _ollama_health_state
                _ollama_health_state = "up" if now_up else "down"
                _ollama_up = now_up
                transition = (prev, _ollama_health_state)
            if transition == ("up", "down") and notify:
                print("[OLLAMA] Connection lost")
                if app is not None and hasattr(app, "play_sound"):
                    try:
                        app.play_sound("error")
                    except Exception as e:
                        logger.debug(f"_health_monitor_loop: {e}")
            elif transition == ("down", "up") and notify:
                print("[OLLAMA] Reconnected")
        except Exception as e:
            logger.debug(f"_health_monitor_loop: {e}")


def _start_health_monitor(app=None):
    t = threading.Thread(target=_health_monitor_loop, daemon=True, name="ollama-health")
    t.start()
    return t


_start_health_monitor()


# ── Legacy safety gate helpers (used by confirm/cancel in dictation pipeline) ─

def get_pending_action():
    global _pending_action
    with _pending_action_lock:
        action = _pending_action
        if action and action.get("expires", 0) < time.time():
            _pending_action = None
            action = None
        return action

def clear_pending_action():
    global _pending_action
    with _pending_action_lock:
        _pending_action = None
