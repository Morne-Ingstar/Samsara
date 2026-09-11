"""Voice command entry point for the quick memo capture lane."""
from samsara.plugin_commands import command


@command("take a memo", aliases=["quick memo"], pack="utilities")
def start_quick_memo(app, remainder):
    starter = getattr(app, "start_memo_capture", None)
    return bool(starter and starter())
