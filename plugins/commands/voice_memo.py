"""Voice commands to arm/cancel voice memo capture (samsara/voice_memo.py).

"voice memo" arms a one-shot divert: the NEXT hold-to-dictate recording is
saved into the Obsidian vault (audio + transcript) instead of injected as
text. "cancel memo" clears the arm before that recording happens.
"""
from samsara import voice_memo
from samsara.plugin_commands import command


@command("voice memo", aliases=["capture memo"], pack="utilities")
def arm_voice_memo(app, remainder):
    """Gets ready to record a memo, which you then dictate as usual."""
    voice_memo.arm(app)
    app.play_sound("start")
    print("[MEMO] Say your memo with hold-to-dictate")
    return True


@command("cancel memo", pack="utilities")
def cancel_voice_memo(app, remainder):
    """Cancels the memo you were about to record."""
    was_armed = voice_memo.disarm(app)
    app.play_sound("stop" if was_armed else "error")
    print("[MEMO] Cancelled" if was_armed else "[MEMO] Nothing armed")
    return True
