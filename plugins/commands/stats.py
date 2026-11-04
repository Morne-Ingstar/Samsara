"""Voice entry to the local usage page (1056)."""
from samsara.plugin_commands import command


@command('show my stats', aliases=['open stats'], pack='core',
         risk_class='safe', side_effects=['ui'], reversible=True,
         ai_composable=False, voice_triggerable=True)
def show_stats(app, remainder=''):
    """Opens your dictation totals and usage history stored on this PC."""
    # PLACEHOLDER COPY (1124)
    from samsara.samsara_navigation import _open_hub
    app._schedule_ui(lambda: _open_hub(app, 'Stats'))
