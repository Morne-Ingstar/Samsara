import os
import sys
from types import SimpleNamespace

import pytest

from samsara.delivery_text import SessionTextDelivery
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import CaptureState, Lane
from samsara.session_modes import SessionMode, SessionModeManager
from samsara.ui import qt_runtime
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tools.build_receipts import desktop, installed_probe, replay_input


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def test_reread_receipt_stages_draft_and_shows_control_from_unset_manager(qapp, tmp_path, monkeypatch):
    # Match the aliases in the disposable profile without running its bootstrap.
    monkeypatch.setitem(sys.modules, 'receipt_probe', installed_probe)
    monkeypatch.setitem(sys.modules, 'receipt_replay', replay_input)
    monkeypatch.setitem(sys.modules, 'receipt_desktop', desktop)
    from tools.build_receipts import daily_probe

    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())

    def unexpected_effect(*args, **kwargs):
        pytest.fail('The synthetic draft must not look up focus, deliver text or run a command')

    verbatim = {'enabled': True, 'address_bar': True}
    app = SimpleNamespace(
        config={'ui': {'live_surface': {'enabled': True}}, 'verbatim': verbatim},
        _session_mode_manager=None,
        continuous_active=False,
        _receipt_replay=SimpleNamespace(silence=lambda: None),
    )
    controller = app.live_surface = LiveSurfaceController(
        app, widget_factory=lambda owner: LiveSurfaceWidget(owner, reduced_motion=True),
    )
    indicator = app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)

    def apply_verbatim(text):
        assert app.config['verbatim']['enabled'] is False
        return None

    app._apply_verbatim_if_active = apply_verbatim
    app.process_transcription = app._apply_formatting_tokens = str

    def ensure_manager():
        if app._session_mode_manager is None:
            app._session_mode_manager = SessionModeManager(
                abort_phrases=['cancel'],
                foreground_exe_resolver=unexpected_effect,
                foreground_hwnd_resolver=unexpected_effect,
                inject_fn=unexpected_effect,
                remove_chars_fn=unexpected_effect,
                command_dispatch_fn=unexpected_effect,
                agent_dispatch_fn=unexpected_effect,
                prepare_dictate_fn=SessionTextDelivery(app, single_line_probe=unexpected_effect).prepare,
                buffer_dictate_until_commit=True,
                live_surface_enabled=True,
            )
        return app._session_mode_manager

    def set_continuous(active):
        app.continuous_active = active
        indicator.set_listening(active)

    app._ensure_session_mode_manager = ensure_manager
    app.start_continuous_mode = lambda: set_continuous(True)
    app.stop_continuous_mode = lambda: set_continuous(False)

    controller.start()
    try:
        assert daily_probe.perform(app, tmp_path, 'silence_start') == {'listening': True}
        assert app._session_mode_manager is None
        assert not controller.widget._reread.isVisible()

        assert daily_probe.perform(app, tmp_path, 'reread_stage') == {'draft_present': True}
        manager = app._session_mode_manager
        assert manager.mode is SessionMode.DICTATE
        assert manager.draft_document.text == controller.view().text == 'Synthetic receipt draft'
        assert controller.view().lane is Lane.HANDS_FREE_DICTATE
        assert app.config['verbatim'] is verbatim
        assert daily_probe.perform(app, tmp_path, 'reread_state') == {
            'reread_visible': True, 'handsfree_draft': True,
        }

        assert daily_probe.perform(app, tmp_path, 'continuous_stop') == {'stopped': True}
        assert manager.mode is SessionMode.COMMAND
        assert manager.draft_document.text == controller.view().text == ''
        assert controller.view().capture is CaptureState.OFF
        assert daily_probe.perform(app, tmp_path, 'reread_state') == {
            'reread_visible': False, 'handsfree_draft': False,
        }
    finally:
        controller.close()
