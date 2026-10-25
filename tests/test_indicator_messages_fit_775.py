"""775: synthetic inventory on both displays, bounded geometry and error lifetimes."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
    LiveSurfaceModel, Notice, NoticeKind, PresentationState, SurfaceEvent, SurfaceView, VisibleForm)
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record
from samsara.ui.indicator_message_layout import message_lines
from samsara.ui.listening_indicator import ListeningIndicator
from samsara.ui.live_surface_qt import LiveSurfaceWidget

# Statically recoverable producer templates; dynamic substitutions are synthetic.
# Runtime exception/command/provider text is unbounded: fallback is tested below.
MESSAGES = (('typed', 'success'), ('asleep', 'accent'), ('review', 'accent'), ('staged', 'pending'), ('undone', 'success'), ('waiting', 'pending'), ('Captured', 'success'), ('? example', 'warning'), ('cancelled', 'warning'), ('confirmed', 'success'), ('Avaexample', 'pending'), ('draft back', 'success'), ('draft parked', 'pending'), ('edit dropped', 'warning'), ('draft example', 'accent'), ('? interact_off', 'warning'), ('forgot example', 'success'), ('undone example', 'success'), ('word not found', 'warning'), ('? interact_exit', 'warning'), ('nothing to halt', 'warning'), ('nothing to park', 'warning'), ('nothing to send', 'accent'), ('? interact_armed', 'warning'), ('? interact_reply', 'warning'), ('Ask Hermes what?', 'app'), ('Recording failed', 'error'), ('refused: example', 'warning'), ('refused: unclear', 'warning'), ('? interact_action', 'warning'), ('Wake phrase heard', 'result'), ('sent, unconfirmed', 'warning'), ('? interact_cleared', 'warning'), ('? interact_confirm', 'warning'), ('? interact_expired', 'warning'), ('? interact_refused', 'warning'), ('? interact_unknown', 'warning'), ('Ava: nothing to do', 'warning'), ('nothing to correct', 'warning'), ('→ command: example', 'success'), ('? interact_choosing', 'warning'), ('? interact_selected', 'warning'), ("can't park: unclear", 'warning'), ('copied to clipboard', 'success'), ('refused: focus lock', 'warning'), ('correction cancelled', 'warning'), ('nothing to take back', 'warning'), ('example example (Ava)', 'success'), ('nothing to bring back', 'warning'), ('refused: say it again', 'warning'), ('reading it backexample', 'accent'), ('refused: nothing staged', 'warning'), ('edited example 3 changes', 'success'), ('New Hermes session ready.', 'app'), ('cleared; nothing inserted', 'warning'), ('example - saved to History', 'warning'), ('example window did not change', 'error'), ("example can't type: admin window", 'error'), ('cancelled, say bring back my draft', 'warning'), ('Draft cleared - say "bring it back"', 'success'), ('undone example example nothing left', 'warning'), ('proposed edit: 3 changes - say apply', 'pending'), ('draft cleared, say bring back my draft', 'success'), ('undone example of example example stopped', 'warning'), ("Hermes couldn't start its background turn.", 'app'), ('Hermes stopped; your conversation is kept.', 'app'), ('heard: Jarvis open notepad — not a command', 'warning'), ('heard: Jarvis open notepad — open notepad…', 'warning'), ('heard: Jarvis open notepad — ran open notepad', 'warning'), ('heard: Jarvis open notepad — suggestion cancelled', 'warning'), ('Say "switch window" on its own; it needs your yes.', 'warning'), ('Say new Hermes session with a listed effort level.', 'app'), ('heard: Jarvis open notepad — window did not change', 'warning'), ('heard: Jarvis open notepad — Window commands is off', 'warning'), ('heard: Jarvis open notepad — open notepad? yes or no', 'warning'), ('Enable Hermes in Settings and accept its notice first.', 'app'), ('open notepad · switch window · scroll down · close tab', 'warning'), ('Done: open notepad · switch window · scroll down · close tab', 'warning'), ("Save 'example' as another way to say 'example'? — say save alias", 'pending'), ('I could not do "switch window": window did not change. Nothing ran.', 'warning'), ('heard: Jarvis open notepad — did you mean open notepad? say yes or no', 'warning'), ('Completed (Ava): open notepad · switch window · scroll down · close tab', 'warning'), ('Done: open notepad · switch window · scroll down · close tab · stopped at switch window: window did not change', 'warning'), ('The local model is still loading.', 'accent'), ('Audio lost. Dictate again.', 'error'), ('Words unclear. Say try again.', 'warning'), ('copied: example', 'warning'), ('mouse hotkey off - using example', 'warning'), ('mouse hotkey active', 'success'), ('Delivery failed; draft kept', 'error'), ('Kept draft: restart hands-free to send or clear.', 'accent'), ('Send refused; target focus changed', 'error'), ('Saved to History. The kept draft is unchanged.', 'accent'), ('No microphone. Connect a microphone to dictate.', 'error'), ('Undid: example', 'success'), ('Nothing to undo', 'error'), ('Undo: go back to where it was typed', 'error'), ('Undone', 'success'), ('Cannot undo safely here', 'error'), ('Redone', 'success'), ('Cannot redo safely here', 'error'), ('halted example', 'warning'), ('→ example', 'accent'), ('→', 'accent'), ('✗ ✗ example', 'error'), ("✗ can't type: admin window", 'error'), ('✓ fixed: example', 'success'), ('✓ word fixed', 'success'), ('undone example of example — stopped', 'warning'), ('reading it back…', 'accent'), ('proposed edit: example change - say apply', 'pending'), ('edited ✓ example change', 'success'), ('no edit — ✗ example', 'warning'), ('no edit', 'warning'), ('Ava…', 'pending'), ('✓ example (Ava)', 'success'), ('→ example: display error', 'warning'), ('→ display error', 'warning'), ('undone example — nothing left', 'warning'), ('No speech', 'info'), ('Hermes: example', 'warning'), ('example thinking', 'accent'), ('Network voice unavailable - using the Windows voice.', 'warning'), ("Samsara's code changed on disk - restart to load it", 'warning'), ('This is how Samsara sounds at this speed and volume.', 'success'), ('Ava is off. Turn her on in Settings > Ava.', 'warning'), ('ignored - Ava was speaking', 'warning'), ('Interact needs a fresh voice capture', 'warning'), ('✗ no speech', 'error'), ('low-confidence decode - not staged', 'warning'), ('echo of your draft - not staged', 'warning'), ('…', 'pending'), ('Send refused; draft not confirmed in target', 'error'), ('Send refused; Claude draft not confirmed', 'error'), ('Auto-paste off; text not sent', 'error'), ('Paste failed', 'error'), ('REC', 'live'), ('✗ no mic', 'error'), ('✗ transcribe failed', 'error'), ('mic lost', 'error'), ('The local model is still loading.', 'running'), ('Ava thinking', 'running'), ('Vision thinking', 'running'), ('Hermes: Synthetic status', 'accent'))


@pytest.fixture
def displays(qapp, monkeypatch):
    created = []
    def make(edge, t, scale=1, negative=False):
        area = QRect(-round(1920/scale) if negative else 0, 0, round(1920/scale), round(1040/scale))
        class FakeScreen:
            def name(self): return '775 synthetic'
            def availableGeometry(self): return QRect(area)
            def geometry(self): return QRect(area)
        fake = FakeScreen()
        monkeypatch.setattr(QApplication, 'screens', staticmethod(lambda: [fake]))
        monkeypatch.setattr(QApplication, 'primaryScreen', staticmethod(lambda: fake))
        screen = Screen(fake.name(), Rect(area.x(),area.y(),area.width(),area.height()), True)
        cfg = {'ui': {'live_surface': {'enabled': True, 'placement': {
            'preferred_monitor':screen.id, 'monitors':{screen.id:placement_record(Placement(edge,t))}}}}}
        controller = LiveSurfaceController(SimpleNamespace(config=cfg))
        # These tests supply immutable views directly; the controller's
        # polling refresh must not replace them with its separate model.
        monkeypatch.setattr(controller, 'refresh_expired', lambda: None)
        controller._screens = lambda: [screen]
        notice = Notice(NoticeKind.NONE, '', None)
        view = SurfaceView(VisibleForm.LIVE, CaptureState.HANDS_FREE_LISTENING, Lane.HANDS_FREE_DICTATE,
            DocumentState.EDITABLE_DRAFT, PresentationState.COMPACT, notice, 'synthetic',1,
            'Synthetic draft.', '', True)
        full = LiveSurfaceWidget(controller, reduced_motion=True)
        controller.widget = full
        controller._connect_intents()
        full.refresh(view, animate=False); controller._place_widget(); full.show()
        compact = ListeningIndicator(); compact.set_idle_animation(False)
        compact._custom_position = {'screen':fake.name(),'cx':.5,'cy':.5,'edge':edge.value,'t':t}
        compact.show()
        created.extend([full, compact])
        return SimpleNamespace(area=area,full=full,compact=compact,controller=controller,view=view)
    yield make
    for w in created:
        w.hide()
        if isinstance(w, ListeningIndicator): w.destroy()
        w.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('scale', [1, 1.5])
@pytest.mark.parametrize('edge', [Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT])
@pytest.mark.parametrize('t', [0., .5, 1.])
def test_inventory_fits_both_displays_and_every_dock(displays, qapp, scale, edge, t):
    d = displays(edge,t,scale,negative=t==1)
    for message, kind in MESSAGES:
        kind = kind if kind in {k.value for k in FeedbackKind} else 'warning'
        feedback = FeedbackKind(kind)
        notice = Notice(NoticeKind.INFORMATION,message,None,feedback=feedback)
        d.full.refresh(replace(d.view,notice=notice),animate=False)
        d.controller._place_widget()
        d.compact.show_outcome(message,kind if kind in ('success','error','warning','accent','pending','live') else 'pending',None,
            _feedback=Feedback(feedback, FeedbackPurpose.RUNNING, 'synthetic') if feedback is FeedbackKind.RUNNING else None)
        qapp.processEvents()
        band = d.full._chip or d.full._band
        assert not band.elided, message
        assert band.toolTip() == message
        assert band.rect().contains(band.label.geometry())
        assert band.height() >= len(message_lines(band._message,band.label.font(),band.label.width())[0])*band.label.fontMetrics().lineSpacing()
        assert d.full.rect().contains(band.geometry())
        rect = d.compact._layout()[3]
        lines, elided = message_lines(message,d.compact._chip_font(),rect.width()-24)
        assert not elided, message
        assert rect.height() >= len(lines)*QFontMetrics(d.compact._chip_font()).lineSpacing()
        assert d.compact._message_window.toolTip() == message
        for w in (d.full,d.compact):
            assert d.area.contains(w.geometry()), (message,edge,t,w.geometry())
            if edge is Edge.BOTTOM: assert w.geometry().bottom() <= d.area.bottom()-8
    # A new message of a different size retains the selected edge/corner.
    for message in ('Short result', 'A longer replacement result with words '*4):
        d.full.refresh(replace(d.view,notice=Notice(NoticeKind.SUCCESS,message,8)),animate=False)
        d.controller._place_widget()
        d.compact.show_outcome(message,'success')
        for w in (d.full,d.compact):
            bounds=w.geometry()
            if edge is Edge.TOP: assert bounds.top()==d.area.top()+8
            if edge is Edge.BOTTOM: assert bounds.bottom()==d.area.bottom()-8
            if edge is Edge.LEFT: assert bounds.left()==d.area.left()+8
            if edge is Edge.RIGHT: assert bounds.right()==d.area.right()-8


@pytest.mark.parametrize('token', ['many words ' * 120, 'x'*1200, 'line one\nline two\nline three'])
def test_only_text_beyond_two_lines_is_elided_with_full_hover(displays,qapp,token):
    d=displays(Edge.BOTTOM,0,1.5)
    d.full.refresh(replace(d.view,notice=Notice(NoticeKind.ERROR,token,8)),animate=False)
    d.controller._place_widget()
    d.compact.show_outcome(token,'error'); qapp.processEvents()
    band=d.full._chip or d.full._band
    assert band.elided and band.label.text().endswith('…')
    assert len(band.label.text().splitlines())==2
    assert band.toolTip()==band.label.toolTip()==token
    rect=d.compact._layout()[3]
    lines,elided=message_lines(token,d.compact._chip_font(),rect.width()-24)
    assert len(lines)==2 and elided and lines[-1].endswith('…')
    assert d.compact._message_window.toolTip()==token
    assert d.area.contains(d.full.geometry()) and d.area.contains(d.compact.geometry())


def test_errors_clear_on_next_utterance_and_click_but_device_fault_survives(displays,qapp,monkeypatch):
    d=displays(Edge.BOTTOM,0)
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post',lambda *_:None)
    clock=[0.]
    model=d.controller.model=LiveSurfaceModel(clock=lambda:clock[0])
    def error():
        model.consume(SurfaceEvent(EventKind.NOTICE,notice=NoticeKind.ERROR,message='Synthetic failure',persistent=True))
        d.controller.drain()
        d.compact.show_outcome('Synthetic failure','error',None)
        qapp.processEvents()
    error()
    assert model.view().notice.expires_at==4.0  # 891: minimum time to read an error.
    QTest.mouseClick(d.full._chip.label,Qt.MouseButton.LeftButton)
    QTest.mouseClick(d.compact._message_window,Qt.MouseButton.LeftButton)
    assert not model.view().notice.message and not d.compact._chip_label
    error()
    model.consume(SurfaceEvent(EventKind.CAPTURE_STARTED,capture_id='synthetic',lane=Lane.HOLD))
    d.compact.set_listening(True)
    assert not model.view().notice.message and not d.compact._chip_label
    error()
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED,capture=CaptureState.HANDS_FREE_LISTENING))
    model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED,capture=CaptureState.RECORDING))
    assert not model.view().notice.message
    error()
    clock[0]=4.0  # 891: expiry follows the error reading-time floor.
    d.compact._chip_deadline=0
    d.compact._clear_outcome()
    assert not model.view().notice.message and not d.compact._chip_label
    model.consume(SurfaceEvent(EventKind.DEVICE_CHANGED,device_lost=True))
    d.compact.set_device_lost(True)
    model.dismiss_error(); d.compact.dismiss_error()
    assert model.view().capture is CaptureState.UNAVAILABLE and d.compact._device_lost
