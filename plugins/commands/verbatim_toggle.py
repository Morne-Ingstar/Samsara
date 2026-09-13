"""Spoken toggle for the VERBATIM dictation profile.

    "literal on"  / "verbatim on"   -- force the profile everywhere
    "literal off" / "verbatim off"  -- back to normal dictation

Registered in the same command registry as every other spoken command so it
appears in Settings -> Commands and in the Quick Reference. The DICTATE
lanes do not route through this registry, so dictation.py ALSO consumes the
same phrases before the language-confidence gate -- see
DictationApp._consume_verbatim_toggle. Both paths call the one setter,
set_verbatim_forced(), and the phrase table lives in samsara/verbatim.py.
"""

from samsara.plugin_commands import command


def _set(app, on: bool) -> bool:
    setter = getattr(app, "set_verbatim_forced", None)
    if setter is None:
        print("[VERBATIM] app has no set_verbatim_forced -- toggle unavailable")
        return True
    setter(on)
    try:
        app.play_sound("success")
    except Exception:
        pass
    return True


@command("literal on", aliases=["verbatim on"], pack="core")
def handle_literal_on(app, remainder=""):
    """Force the verbatim dictation profile on, regardless of target app."""
    return _set(app, True)


@command("literal off", aliases=["verbatim off"], pack="core")
def handle_literal_off(app, remainder=""):
    """Return to normal dictation formatting."""
    return _set(app, False)
