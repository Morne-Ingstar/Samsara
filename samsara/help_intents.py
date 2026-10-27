"""Offline, whole-request help discovery (943); never execute a topic as a command.

The executor calls route_request only in an explicit command lane. The handler
seam also supports existing ``help <topic>`` calls, without changing search help.
No profile, UI, registry, model or network access occurs during matching.
"""
from dataclasses import dataclass
import re


# These prefixes advertise discovery through the normal plugin catalog. The
# command-lane seam handles the full grammar before embedded command matching.
HELP_PREFIXES = ("how do i", "how can i", "where do i", "help me",
                 "what is the command to", "is there a way to")


@dataclass(frozen=True)
class Target:
    place: str
    topics: tuple[str, ...]
    tasks: tuple[str, ...]


# Every destination below is an existing samsara_navigation.PLACES action.
# The six 939 terms are referenced here; their unpublished card text is not copied.
_DATA = """
reference|command_reference|the commands;the list of things I can say;the command reference;the cheat sheet|see the commands;see what I can say;find a list of commands;find out what this can do
settings|settings|Samsara settings;the app settings|change my settings;change Samsara settings
general|general|general settings;startup settings|change general settings;change startup settings
dictation|dictation|dictation settings;typing settings|change dictation settings;change how text is typed
audio|audio|audio settings;sound settings|change audio settings;change sound settings
microphone|general|microphone settings;my microphone;my mic|change my mic;change the microphone;change my microphone;choose another microphone
ava|ava|Ava;Ava settings;the AI assistant|turn Ava off;turn Ava on;set up Ava;disable the assistant;enable the assistant
shortcuts|shortcuts|shortcuts;hold to record keys;the recording keys|change my shortcuts;change the hold to record keys;change my recording key
help_settings|help|help settings;the help page|find the help page;change help settings
home|home|Samsara Home;the home page|get back to Home;find Samsara Home
command_list|command_list|my command list;the Commands page|find my command list;see my saved commands
history|history|history;what I dictated;my previous dictation|see what I said earlier;find my history;see my previous dictation
dictionary|dictionary|the dictionary;my custom words;my vocabulary|add a word;fix a word it gets wrong;teach it my name;change my vocabulary
snippets|snippets|snippets;my saved phrases|save a phrase;find my saved phrases
memos|memos|memos;my voice notes|find my voice notes;see my memos
voice_help|voice_help|voice troubleshooting;voice help|fix it when it can't hear me;find out why it can't hear me
commands|commands|the command library;command packs|change command packs;turn command packs on;turn command packs off
alarms|alarms|alarms;my alarm list|see my alarms;manage my alarms
health|health|app health;the health checks|check app health;see the health checks
music|music|music settings;music controls|find music settings;set up music controls
components|components|downloaded components;installed models|manage installed models;see downloaded components
integrations|integrations|integrations;connected apps|set up integrations;see connected apps
connect_ai_app|connect_ai_app|AI app connections;the connect an AI app page|connect an AI app;connect my AI app
guides|guides|the guides;the tutorials|find the guides;see the tutorials
tutorial|tutorial|the tutorial;the beginner lessons|start over the tutorial;learn how to use this;do the tutorial again;restart the tutorial;learn the basics
microphone_guide|microphone_guide|the microphone setup guide;the mic setup guide|set up my microphone;test my microphone;check my mic setup
ava_setup|ava_setup|the Ava setup guide;the AI setup guide|find the Ava setup guide;learn to set up the assistant
quick_reference|quick_reference|the quick reference;the quick guide|find the quick reference;see the quick guide
reminders|reminders|reminders;my reminder list|see my reminders;manage my reminders
tasks|tasks|tasks;my task list|see my tasks;find my task list
listening_display|general|the listening display;the floating display;the full display;the compact display|change the listening display;choose the compact display;choose the full display
updates|updates|updates;the update checker|check for an update;find the update checker
about|about|the version;information about Samsara|see my version;find information about Samsara
documentation|documentation|the documentation;the docs;the online guides|read the documentation;find the online guides;read more about Samsara
discord|discord|the community;the Discord community|find the community;join the Discord community
support|support|support;the support page|get support;find the support page
diagnostics|diagnostics|dictation diagnostics;the diagnostics window|see dictation diagnostics;find the diagnostics window
live_log|live_log|the live log;live log messages|see the live log;view live log messages
log|log|the main log;the log file|find the main log;open the log file
transcribe_file|transcribe_file|file transcription;the transcribe a file window|transcribe an audio file;turn an audio file into text
benchmark_review|benchmark_review|benchmark results;the benchmark review|review benchmark results;see benchmark results
correction|correction|the last dictation correction;the correction window|correct my last dictation;fix my last dictation
stress_test|stress_test|the stress test wizard;the stress test window|find the stress test wizard;open the stress test window
wake_word_debug|wake_word_debug|wake word debugging;the wake word debug window|debug the wake word;find the wake word debug window
config_folder|config_folder|the config folder;the configuration folder|find my configuration folder;open the config folder
wake_word|dictation|the wake word;wake activation|set up the wake word;use a wake word;start without pressing a key
hands_free|dictation|hands-free;hands free;hands free mode|use hands free;keep it listening;dictate without holding a key
speech|audio|the spoken replies;the voice output;the talking voice|make it stop talking;turn off the voice;stop spoken replies;turn the voice back on;stop it talking;mute the voice
"""

TARGETS = {}
for _line in _DATA.strip().splitlines():
    _key, _place, _topics, _tasks = _line.split("|")
    TARGETS[_key] = Target(_place, tuple(_topics.split(";")), tuple(_tasks.split(";")))


def normalize(text):
    text = str(text or "").casefold().replace("’", "'")
    text = text.replace("i am ", "i'm ").replace("cannot ", "can't ")
    text = " ".join(text.split()).strip().rstrip(" .?!")
    if text.startswith("please "):
        text = text[7:]
    if text.endswith(" please"):
        text = text[:-7]
    return text.rstrip(" .?!")


TASK_FRAMES = ("how do i {}", "how can i {}", "where do i {}",
               "what's the command to {}", "is there a way to {}",
               "i want to {}", "help me {}", "help {}", "show me how to {}")
TOPIC_FRAMES = ("where's {}", "where is {}", "i can't find {}", "show me {}",
                "help {}", "help with {}", "what is {}", "what's {}")
CONFUSION = ("help", "i'm lost", "what do i do", "i'm confused", "what now",
             "i need help", "help me", "i don't know what to do", "what can this do")


@dataclass(frozen=True)
class HelpIntent:
    target: str
    topic: str = ""


PHRASES = {}
for _key, _target in TARGETS.items():
    for _forms, _slots in ((TASK_FRAMES, _target.tasks), (TOPIC_FRAMES, _target.topics)):
        for _slot in _slots:
            for _frame in _forms:
                _phrase = normalize(_frame.format(_slot))
                if _phrase in PHRASES and PHRASES[_phrase].target != _key:
                    raise ValueError("Ambiguous help phrase: " + _phrase)
                PHRASES[_phrase] = HelpIntent(_key, normalize(_slot))

# Natural requests which are not a mechanical topic/frame combination.
PHRASES.update({
    "what are command packs": HelpIntent("commands"),
    "what can this do": HelpIntent("reference"),
    "what can i say here": HelpIntent("reference"),
    "it can't hear me": HelpIntent("voice_help"),
    "help it can't hear me": HelpIntent("voice_help"),
    "how do i make it hear me": HelpIntent("voice_help"),
})


def match_help(text, *, command_mode):
    """Exact topic lookup; unknown leading help requests get reference, never a guess.

    A caller MUST supply the lane. Embedded/quoted questions never qualify.
    Dictation receives None even for a complete known help question.
    """
    if not command_mode:
        return None
    phrase = normalize(text)
    if phrase in PHRASES:
        return PHRASES[phrase]
    if phrase in CONFUSION:
        return HelpIntent("confusion")
    if re.match(r"^(?:how (?:do|can) i|where (?:do i|is|are)|where's|what(?:'s| is) the command to|what(?:'s| is| are)|"
                r"is there a way to|i can't find|show me|i want to|help(?: me| with)?)\s+", phrase):
        return HelpIntent("unknown", phrase)
    return None


def route_request(executor, text, app, *, command_mode, route, generation, capture_path=None):
    """Enter the existing registered help handler through canonical policy."""
    intent = match_help(text, command_mode=True)
    if intent is None:
        return None
    # Literal existing commands (including aliases, personal/scoped/disabled
    # commands) retain ownership; never steal an exact registered utterance.
    from samsara.command_catalog import normalize_phrase
    if capture_path == "ava_hold":
        # 943b: forced matching on Ava's key is not help-discovery intent.
        # Only a whole registered phrase may take a question away from Ava.
        if normalize_phrase(text) in executor._matcher._entries:
            return None
        from samsara.command_registry import DispatchResult
        return DispatchResult.miss(text)
    if (normalize(text) in executor._matcher._entries
            or normalize_phrase(text) in executor._matcher._entries):
        return None
    if not command_mode:
        # Even the saved legacy opportunistic-matching opt-out must not turn
        # a new help question into an action in a dictation lane.
        from samsara.command_registry import DispatchResult
        return DispatchResult.miss(text)
    # 608 owns help <topic>, including app-scoped catalogue/control search.
    # Its handler tries that answer before asking us about an unknown topic.
    phrase = normalize(text)
    if phrase.startswith("help "):
        return None
    records, _by_id = executor._grammar_catalog()
    if any(phrase == normalize(example) for record in records
           for example in record.get("examples", ())):
        return None
    # Argument-bearing real commands also keep their full utterances, not
    # just their canonical aliases. Ignore only our new discovery prefixes.
    detail = executor._matcher.match_detail(text)
    if detail is not None:
        matched = " ".join(detail.phrase_tokens)
        if detail.entry.phrase != "what can i say" or matched not in HELP_PREFIXES:
            return None
    return executor.execute_canonical(
        "what can i say", app, route=route, generation=generation,
        args={"remainder": text}, source_text=text)


# PLACEHOLDER COPY (943). All new responses are spoken only. Existing target
# windows show their existing titles and content; no new visible surface.
CONFUSION_REPLY = "Start here: these are the things you can say."
UNKNOWN_REPLY = "Try saying 'help' followed by what you want to do."


def _scoped_answer(app, query, allowed_packs, live_text):
    """Use the Reference's phrase/alias substring search and 608's controls."""
    if live_text.startswith("Controls found here:"):
        return True
    from samsara.command_catalog import guidance_catalog, canonical_phrase
    from samsara.delivery_text import normalize_search_query
    executor = getattr(app, "command_executor", None)
    records = (executor._grammar_catalog()[0] if executor is not None
               else guidance_catalog())
    if records is None:
        return True  # Keep 608's unavailable-catalog answer rather than guessing.
    needle = normalize_search_query(query)
    return any(record.get("pack", "core") in allowed_packs
               and (needle in canonical_phrase(record)
                    or any(needle in alias for alias in record.get("aliases", ())))
               for record in records)


def handle_request(app, remainder="", *, allowed_packs=(), live_text="", **kwargs):
    """Extend 608 only after its scoped topic has no catalogue/control answer."""
    if kwargs.get("_help_intents_skip"):
        return False
    request = normalize(remainder)
    if not request or request == "search" or request.startswith("search "):
        return False
    intent = match_help(request, command_mode=True)
    if intent is None:
        if _scoped_answer(app, request, allowed_packs, live_text):
            return False
        intent = match_help("help " + request, command_mode=True)
    from plugins.commands.core_utils import what_can_i_say, speak_if_available
    if intent.target in {"reference", "confusion", "unknown"}:
        what_can_i_say(app, _help_intents_skip=True)
        if intent.target != "reference":
            speak_if_available(app, CONFUSION_REPLY if intent.target == "confusion" else UNKNOWN_REPLY)
    else:
        from samsara.samsara_navigation import PLACES, open_place
        place = TARGETS[intent.target].place
        open_place(app, place)
        speak_if_available(app, "Opening " + PLACES[place].label + ".")
    return True
