"""Loss-safe Compact quit and truthful kept-draft hold receipts, without bootstrap."""
from samsara.log import describe_text, safe_exception
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import kept_session_draft
from tests.test_kept_draft_survives_505 import TEXT, end_session, make_manager


def quit_method():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8-sig'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == '_resolve_parked_draft_before_quit')
    namespace = {'logger': Mock(), 'describe_text': describe_text, 'safe_exception': safe_exception}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<quit>', 'exec'), namespace)
    return namespace[method.name]


@pytest.mark.parametrize('choice,allowed', [('send', True), ('discard', True), ('keep', False)])
def test_compact_quit_asks_once_and_resolves_only_chosen_action(monkeypatch, choice, allowed):
    from samsara.ui import quit_draft_dialog_qt
    manager, inserted = make_manager(full=False)
    end_session(manager)
    ask = Mock(return_value=choice)
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: False, _session_mode_manager=manager)
    assert quit_method()(app) is allowed
    ask.assert_called_once_with()
    assert inserted == ([TEXT] if choice == 'send' else [])
    assert manager.retained_draft == (TEXT if choice == 'keep' else '')
    assert not manager.dictate_pending_buffer


@pytest.mark.parametrize('retained', ['', '   '])
def test_empty_compact_quit_does_not_ask(monkeypatch, retained):
    from samsara.ui import quit_draft_dialog_qt
    ask = Mock()
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: False,
                          _session_mode_manager=SimpleNamespace(retained_draft=retained, has_parked_hold=False))
    assert quit_method()(app)
    ask.assert_not_called()


def test_compact_failed_send_keeps_words_and_asks_again(monkeypatch):
    from samsara.ui import quit_draft_dialog_qt
    manager, inserted = make_manager(full=False)
    end_session(manager)
    manager._inject_fn = lambda *args: False
    ask = Mock(side_effect=['send', 'keep'])
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: False, _session_mode_manager=manager)
    assert not quit_method()(app)
    assert manager.retained_draft == TEXT
    assert not quit_method()(app)
    assert ask.call_count == 2
    assert not inserted


def test_compact_dialog_failure_keeps_words(monkeypatch):
    from samsara.ui import quit_draft_dialog_qt
    manager, _ = make_manager(full=False)
    end_session(manager)
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', Mock(side_effect=RuntimeError('synthetic')))
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: False, _session_mode_manager=manager)
    assert not quit_method()(app)
    assert manager.retained_draft == TEXT


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('source,label,kind', [
    ('pause', 'Saved to History. The kept draft is unchanged.', 'accent'),
    ('voice review', 'Saved to History. The kept draft is unchanged.', 'accent'),
    ('focus changed', 'Send refused; target focus changed', 'error'),
    ('failed delivery', 'Delivery failed; draft kept', 'error'),
])
def test_hold_receipt_describes_source_without_changing_kept_draft(monkeypatch, full, source, label, kind):
    from samsara import tutorial_capture
    manager, _ = make_manager(full=full)
    end_session(manager)

    before = (manager.draft_document.text, manager.retained_draft, manager.recoverable_draft)
    record = Mock(return_value=True)
    monkeypatch.setattr(tutorial_capture, 'record_final', record)
    show = Mock()
    session = SimpleNamespace(app=SimpleNamespace(_session_mode_manager=manager,
                              command_mode_active=False, _show_outcome_chip=show))
    kept_session_draft.park_streaming_hold(session, manager, 'Synthetic hold.', None, 1.0, source=source)
    record.assert_called_once_with(session, 'Synthetic hold.', None, 1.0)
    show.assert_called_once_with(label, kind)
    assert (manager.draft_document.text, manager.retained_draft, manager.recoverable_draft) == before


def test_compact_reminder_has_one_production_definition():
    root = Path(__file__).resolve().parents[1]
    # Construct the phrase so this regression does not duplicate its literal.
    phrase = 'Kept draft: ' + 'restart hands-free to send or clear.'
    files = [root / 'dictation.py', *(root / 'samsara').rglob('*.py')]
    occurrences = sum(p.read_text(encoding='utf-8-sig').count(phrase) for p in files)
    assert occurrences == 1
    assert kept_session_draft.COMPACT_KEPT_DRAFT_HINT == phrase
