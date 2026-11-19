"""1275: synthetic words survive unknown targets without automatic previews."""
from types import SimpleNamespace as NS
import time

import pytest
from PySide6.QtWidgets import QLabel

from samsara import recent_privacy as privacy
from samsara.config_defaults import cfg_get
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.history import HistoryManager
from samsara.ui import history_view as hv
from samsara.ui.history_unverified_qt import prepare_paste_again
from samsara.ui.home_recent_qt import presentation, RecentActivity


WORDS = 'Synthetic lilac boats cross the pond.'


@pytest.fixture
def app(tmp_path, monkeypatch):
    db = HistoryManager(tmp_path / 'history.db')
    app = NS(config={}, history_db=db)
    monkeypatch.setattr(privacy, 'focused_target', lambda: privacy.TargetPrivacy('unverified_target', 'fixture.exe'))
    yield app
    db.close()


def write(app, **fields):
    privacy.write_history(app, raw_text=WORDS, display_text=WORDS, **fields)
    return dict(app.history_db.recent()[0])


def test_default_and_private_inheritance_are_enabled(app):
    assert SETTINGS_SCHEMA['history.store_unverified_text']['default'] is True
    assert cfg_get({}, 'history.store_unverified_text') is True
    assert privacy.store_unverified_text(app) is True
    assert privacy.store_unverified_text(NS(config={'history': {'store_unverified_text': False}})) is False


@pytest.mark.parametrize('reason', ['unverified_target', 'password_field', ''])
@pytest.mark.parametrize('keep', [True, False])
def test_storage_policy_and_reopen(app, monkeypatch, reason, keep, tmp_path):
    app.config = {'history': {'store_unverified_text': keep}}
    monkeypatch.setattr(privacy, 'focused_target', lambda: privacy.TargetPrivacy(reason, 'fixture.exe'))
    row = write(app, app_context='Synthetic title', audio_path='synthetic.wav')
    withheld = reason == 'password_field' or (reason == 'unverified_target' and not keep)
    assert row['raw_text'] == row['display_text'] == ('' if withheld else WORDS)
    assert row['privacy_reason'] == reason
    other = HistoryManager(tmp_path / 'history.db')
    try:
        assert dict(other.recent()[0])['display_text'] == row['display_text']
    finally:
        other.close()
    assert presentation(row, True)[0] == ('hidden' if reason else f'“{WORDS}”')
    if reason:
        assert WORDS not in hv.list_text(row)
    if withheld:
        assert row['app_context'] == '' and row['audio_path'] is None
        assert hv.copy_text(row) == ''


@pytest.mark.parametrize('keep', [True, False])
def test_pre_delivery_row_and_cached_home_are_concealed(app, monkeypatch, qapp, keep):
    app.config = {'history': {'store_unverified_text': keep}}
    entry = app.history_db.add(WORDS)
    app.history = [('synthetic timestamp', WORDS, False)]
    app.save_history = lambda: None
    monkeypatch.setattr(privacy, 'sharing_detected', lambda: False)
    home = RecentActivity(app, lambda page: None, lambda: 'Hold to talk')
    home.rows = [dict(app.history_db.recent()[0])]
    app._notify_main_window = lambda text: home._paint_rows()
    try:
        assert privacy.before_delivery(app, WORDS)
        row = dict(app.history_db.recent()[0])
        assert row['id'] == entry and row['privacy_reason'] == 'unverified_target'
        assert row['display_text'] == (WORDS if keep else '')
        assert home._row(0)['display_text'] == ''
        assert presentation(home._row(0), True)[0] == 'hidden'
        assert app.history[0][1] == ''  # Legacy tuples cannot carry the concealment metadata.
    finally:
        home.deleteLater()


@pytest.mark.parametrize('reason', ['password_field', 'unverified_target'])
def test_withheld_row_cannot_be_resurrected_by_retry_or_status(app, reason):
    entry = app.history_db.add(WORDS, privacy_reason=reason, store_unverified_text=False)
    app.history_db.add(WORDS, entry_id=entry)
    app.history_db.update_status(entry, 'success', WORDS)
    row = dict(app.history_db.recent()[0])
    assert row['raw_text'] == row['display_text'] == ''
    assert row['privacy_reason'] == reason


def test_retained_retry_keeps_unknown_metadata_and_new_password_wins(app):
    row = write(app)
    app.history_db.add('Synthetic revised words.', entry_id=row['id'])
    saved = dict(app.history_db.recent()[0])
    assert saved['privacy_reason'] == 'unverified_target'
    assert saved['display_text'] == 'Synthetic revised words.'
    app.history_db.add('', status='failed', privacy_reason='unverified_target', entry_id=row['id'])
    saved = dict(app.history_db.recent()[0])
    assert saved['display_text'] == 'Synthetic revised words.'
    app.history_db.add(WORDS, privacy_reason='password_field', entry_id=row['id'])
    saved = dict(app.history_db.recent()[0])
    assert saved['privacy_reason'] == 'password_field' and saved['raw_text'] == ''


def test_confirmed_password_overrides_recent_unknown_receipt(app, monkeypatch):
    app._recent_delivery_privacy = (time.monotonic(), privacy.TargetPrivacy('unverified_target'))
    monkeypatch.setattr(privacy, 'focused_target', lambda: privacy.TargetPrivacy('password_field'))
    row = write(app)
    assert row['privacy_reason'] == 'password_field'
    assert row['display_text'] == ''


@pytest.mark.parametrize('status', ['failed', 'success', 'buffered'])
def test_collapsed_row_has_direct_copy_and_paste_actions(app, qapp, monkeypatch, status):
    row = write(app, status=status)
    copied, armed = [], []
    monkeypatch.setattr(hv.HistoryView, '_reload', lambda self: None)
    view = hv.HistoryView(None, on_paste_again=lambda entry: armed.append(hv.copy_text(entry)))
    try:
        view.resize(900, 650)
        view._render_rows([row], append=False)
        item = view._list.item(1)
        widget = view._list.itemWidget(item)
        assert WORDS not in widget.text_label.text()
        assert widget.text_label.toolTip() == ''
        assert not widget.show_button.isHidden()
        assert not widget.copy_button.isHidden()
        assert not widget.paste_again_button.isHidden()
        monkeypatch.setattr(view, '_set_clipboard', lambda text: copied.append(text))
        widget.copy_button.click()
        widget.paste_again_button.click()
        assert copied == armed == [WORDS]
        widget.show_button.click()
        assert widget.full_text == WORDS and widget.show_button.text() == 'Hide'
        widget.show_button.click()
        assert WORDS not in widget.full_text
        assert widget.relayout(850) > hv._ROW_MIN_HEIGHT
    finally:
        view.deleteLater()


def test_detail_selection_and_reselection_require_show(app, qapp):
    row = write(app)
    row['raw_text'] = 'Synthetic alternate recognition.'
    card = hv._DetailCard(lambda: None, lambda: None, lambda: None)
    try:
        card.show_row(row)
        assert WORDS not in card.toPlainText()
        assert row['raw_text'] not in card.heard_as.text()
        card.show_button.click()
        assert card.toPlainText() == WORDS
        assert row['raw_text'] in card.heard_as.text()
        card.show_row(row)
        assert WORDS not in card.toPlainText()
        assert card.heard_as.text() == ''
    finally:
        card.deleteLater()


def test_explicit_paste_prepares_selected_words_without_injection(app, monkeypatch):
    row = write(app, status='failed')
    from samsara import paste_again
    notes, controls = [], []
    app._show_outcome_chip = lambda *args: notes.append(args)
    app._paste_preserving_clipboard = lambda *a, **kw: pytest.fail('History must not inject into its own window')
    monkeypatch.setattr(paste_again, '_refresh_control', lambda app: controls.append(True))
    prepare_paste_again(app, row)
    assert paste_again.last_text(app) == WORDS and controls == [True]
    assert 'Choose a text window' in notes[0][0]


def test_schema_and_history_view_size_budget():
    from pathlib import Path
    from tools import config_defaults_check
    assert config_defaults_check.find_violations() == []
    assert len((Path(__file__).parents[1] / 'samsara/ui/history_view.py').read_text(encoding='utf-8').splitlines()) <= 1781
