"""Generate a complete code review package for Gemini."""
import os
from datetime import datetime

ROOT = r"C:\Users\Morne\Projects\Samsara-dev"
OUT = r"C:\Users\Morne\Documents\Claude\samsara_gemini_review.md"

FILES = [
    ("dictation.py", "Main application — 6500-line monolith containing 6 classes"),
    ("samsara/command_parser.py", "Wake word command parser — pure functions, returns structured intent"),
    ("samsara/wake_word_matcher.py", "Token-aware wake phrase matching"),
    ("samsara/wake_corrections.py", "Whisper misrecognition correction map"),
    ("samsara/echo_cancel.py", "WASAPI loopback echo cancellation (NLMS filter)"),
    ("samsara/plugin_commands.py", "Plugin command system scaffold (not yet wired)"),
    ("samsara/clipboard.py", "Win32 clipboard save/restore for paste operations"),
    ("samsara/key_macros.py", "Tap-combos and toggle-hold for accessibility"),
    ("samsara/notifications.py", "Windows toast notifications"),
    ("samsara/alarms.py", "Nag-until-dismissed alarms with streak tracking"),
    ("samsara/profiles.py", "Dictionary/command profile import-export"),
    ("samsara/ui/wake_word_debug.py", "Wake word debug window with trace pipeline"),
    ("samsara/ui/listening_indicator.py", "Always-on-top mode/listening overlay"),
    ("samsara/ui/splash.py", "Splash screen (stale — duplicated in dictation.py)"),
    ("voice_training.py", "Voice training module"),
    ("commands.json", "Voice command definitions"),
]

HEADER = """# Samsara — Full Code Review Package
Generated: {date}

## What is Samsara?
Samsara is a fully offline voice dictation and command system for Windows,
built for accessibility (developer has chronic hand pain). It uses faster-whisper
for local speech-to-text, runs as a system tray app, and is evolving toward a
modular AI assistant with plugin support and API integrations.

Tech stack: Python 3.11, faster-whisper, sounddevice/PortAudio, CustomTkinter,
pystray, pynput, Win32 APIs.

## Architecture at a Glance

dictation.py is the monolith (~6500 lines, 6 classes). The samsara/ package
contains extracted modules, some active (command_parser, wake_word_matcher,
clipboard, alarms) and some stale from a partial refactor (audio.py, config.py,
speech.py — only used by tests, not the real app).

Key pipeline: Mic → PortAudio callback → speech/silence detection → Whisper →
wake word matching → command parsing → intent routing → execution or dictation.

Audio uses dual sample rates: capture at device native (44100/48000 via WASAPI),
resample to 16kHz before Whisper.

## What I Want You to Review

You are auditing this entire codebase as an independent reviewer. The code was
built iteratively with help from Claude and GPT. I need fresh eyes on:

1. **Architecture** — Is the monolith structure sustainable? What should be
   extracted next? Are there circular dependencies or tight couplings that
   will hurt as the app grows toward plugin support?

2. **Audio pipeline** — The dual sample rate system was just implemented.
   Are there timing bugs, race conditions, or edge cases in the 5 stream
   call sites? Is the resampling approach (np.interp) sufficient?

3. **Threading** — Multiple threads interact: PortAudio callbacks, Whisper
   transcription, pystray tray icon, pynput keyboard listener, tkinter main
   thread, Timer threads for animations/timeouts. Are there race conditions
   or deadlock risks?

4. **Wake word pipeline** — Detection → correction → matching → command parsing
   → intent routing → execution. Is this robust? What Whisper edge cases
   could break it?

5. **State management** — Mode switching, snooze, recording flags, wake word
   active flags, icon animation reasons. Is the state machine consistent?
   Can it get into impossible states?

6. **Error handling** — Where does the app fail silently? Where should it
   recover gracefully but doesn't? Are there exception-swallowing patterns
   that hide bugs?

7. **Security/Privacy** — Any concerns for a tool that listens to microphone
   input and controls keyboard/mouse?

8. **Performance** — The Settings and Debug windows are slow (CustomTkinter).
   Anything else that would degrade with extended use?

9. **Code quality** — Dead code, inconsistent patterns, magic numbers,
   missing docstrings, naming issues.

10. **Future-proofing** — Given the goal of evolving into a modular AI
    assistant with plugins and API integrations, what structural changes
    should be made now vs later?

Be specific. Reference file names and function names. Don't just say
"consider refactoring" — say what, where, and why.

## File Listing

"""

with open(OUT, 'w', encoding='utf-8') as out:
    out.write(HEADER.format(date=datetime.now().strftime('%Y-%m-%d %H:%M')))

    # File listing table
    for rel, desc in FILES:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            lines = sum(1 for _ in open(p, encoding='utf-8', errors='ignore'))
            out.write(f"- `{rel}` ({lines} lines) — {desc}\n")

    out.write(f"\n---\n\n")

    # Full source of each file
    for rel, desc in FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        content = open(p, encoding='utf-8', errors='ignore').read()
        ext = os.path.splitext(rel)[1].lstrip('.')
        if ext == 'json':
            ext = 'json'
        else:
            ext = 'python'

        out.write(f"## {rel}\n\n")
        out.write(f"*{desc}*\n\n")
        out.write(f"```{ext}\n")
        out.write(content)
        if not content.endswith('\n'):
            out.write('\n')
        out.write(f"```\n\n---\n\n")

size = os.path.getsize(OUT)
print(f"Review package written to: {OUT}")
print(f"Size: {size:,} bytes ({size // 1024} KB)")
