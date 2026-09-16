"""Voice command entry points for the quick memo capture lane.

"take a memo" starts the batch capture (queue 07); "open memos" / "show my
memos" opens the memo list queue 92 added, so a memo can be reached, played
and searched without touching the keyboard.
"""
from samsara.plugin_commands import command


@command("take a memo", aliases=["quick memo"], pack="utilities")
def start_quick_memo(app, remainder):
    """Starts recording a quick memo straight away."""
    starter = getattr(app, "start_memo_capture", None)
    return bool(starter and starter())


@command("open memos", aliases=["show my memos", "open my memos", "memo list"],
         pack="utilities")
def open_memo_list(app, remainder):
    """Opens the Memos page, where your saved memos are kept."""
    opener = getattr(app, "open_hub_page", None)
    if not callable(opener):
        return False
    # Imported here rather than at module scope: a plugin is imported during
    # command registration, long before any Qt window exists.
    from samsara.ui.home_qt import MEMOS  # noqa: PLC0415
    return bool(opener(MEMOS))
