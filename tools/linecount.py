import os

root = r"C:\Users\Morne\Projects\Samsara-dev"
files = [
    "dictation.py",
    "samsara/command_parser.py",
    "samsara/wake_word_matcher.py",
    "samsara/wake_corrections.py",
    "samsara/echo_cancel.py",
    "samsara/plugin_commands.py",
    "samsara/clipboard.py",
    "samsara/key_macros.py",
    "samsara/notifications.py",
    "samsara/alarms.py",
    "samsara/profiles.py",
    "samsara/ui/wake_word_debug.py",
    "samsara/ui/listening_indicator.py",
    "samsara/ui/splash.py",
    "commands.json",
    "voice_training.py",
]

total = 0
for f in files:
    p = os.path.join(root, f)
    if os.path.exists(p):
        lines = sum(1 for _ in open(p, encoding='utf-8', errors='ignore'))
        total += lines
        print(f"  {lines:5d}  {f}")
    else:
        print(f"  -----  {f} (NOT FOUND)")
print(f"\n  {total:5d}  TOTAL")
