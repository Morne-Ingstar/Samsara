"""540: optional beta updates, with synthetic GitHub transport only."""
import io
import json
import hashlib
import zipfile
import pytest
from unittest.mock import Mock
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QCheckBox

from samsara import updater
from samsara.runtime_manifest import OWW_MODEL_FILENAMES


@pytest.fixture(autouse=True)
def drain_deleted_qobjects(qapp):
    """Destroy deferred Qt objects before the next test creates another window."""
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


class _UpdateApp:
    def __init__(self, beta=False, automatic=False):
        self.config = {'updates': {'include_prerelease': beta, 'automatic_checks': automatic}}

    def update_config_and_save(self, updates):
        self.config.update(updates)

    def _resolve_parked_draft_before_quit(self):
        return True


def test_updates_choice_is_saved_without_network_and_invalidates_offer(qapp, monkeypatch):
    from samsara.ui import update_qt
    app = _UpdateApp()
    release = SimpleNamespace(version='0.30.0-beta.3', asset_size=123)
    network = Mock()
    monkeypatch.setattr(update_qt, 'check_for_update', network)
    dialog = update_qt._UpdateDialog(app, initial_release=release)
    try:
        choice = dialog.findChild(QCheckBox, 'includeBetaUpdatesCheckbox')
        assert choice is not None
        assert not choice.isChecked()
        choice.click()
        assert app.config['updates'] == {'include_prerelease': True, 'automatic_checks': False}
        assert dialog._release is None
        assert dialog._phase == 'idle'
        network.assert_not_called()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_manual_check_passes_explicit_channel_and_disables_choice(qapp, monkeypatch):
    from samsara.ui import update_qt
    app = _UpdateApp(beta=True)
    workers = []
    check = Mock(return_value=None)
    monkeypatch.setattr(update_qt, 'update_unavailable_reason', lambda: None)
    monkeypatch.setattr(update_qt, 'check_for_update', check)
    monkeypatch.setattr(update_qt.thread_registry, 'spawn', lambda name, target, **kw: workers.append(target))
    dialog = update_qt._UpdateDialog(app)
    try:
        choice = dialog.findChild(QCheckBox, 'includeBetaUpdatesCheckbox')
        assert choice is not None
        dialog.start_check()
        assert not choice.isEnabled()
        workers[0]()
        qapp.processEvents()
        check.assert_called_once_with(current_version=update_qt.__version__, allow_prerelease=True)
        assert choice.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_failed_beta_choice_save_reverts_checkbox_and_runtime_config(qapp):
    from samsara.ui import update_qt
    app = _UpdateApp()
    def fail(updates):
        app.config.update(updates)
        raise OSError('synthetic persistence failure')
    app.update_config_and_save = fail
    dialog = update_qt._UpdateDialog(app)
    try:
        choice = dialog.findChild(QCheckBox, 'includeBetaUpdatesCheckbox')
        assert choice is not None
        choice.click()
        assert not choice.isChecked()
        assert app.config['updates']['include_prerelease'] is False
        assert 'could not' in dialog._status.text().lower()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_automatic_beta_check_drops_offer_after_channel_round_trip(monkeypatch):
    from samsara.ui import update_qt
    from samsara.ui.update_channel_qt import save_beta_choice
    app = _UpdateApp(beta=True, automatic=True)
    workers, posted = [], []
    offered = Mock()
    check = Mock(return_value=SimpleNamespace(version='0.30.0-beta.3', asset_size=123))
    monkeypatch.setattr(update_qt, 'is_frozen_build', lambda: True)
    monkeypatch.setattr(update_qt, 'update_unavailable_reason', lambda: None)
    monkeypatch.setattr(update_qt, 'check_for_update', check)
    monkeypatch.setattr(update_qt.thread_registry, 'spawn', lambda name, target, **kw: workers.append(target))
    monkeypatch.setattr(update_qt.qt_runtime, 'post', posted.append)
    assert update_qt.maybe_start_automatic_update_check(app, offered)
    workers[0]()
    check.assert_called_once_with(current_version=update_qt.__version__, allow_prerelease=True)
    assert save_beta_choice(app, False)
    assert save_beta_choice(app, True)
    posted[0]()
    offered.assert_not_called()


def test_failed_save_restores_choice_when_app_replaces_config():
    from samsara.ui.update_channel_qt import save_beta_choice
    app = _UpdateApp()
    def fail(updates):
        app.config = dict(app.config, **updates)
        raise OSError('synthetic replacement/save failure')
    app.update_config_and_save = fail
    assert not save_beta_choice(app, True)
    assert app.config['updates']['include_prerelease'] is False


def test_beta_install_passes_channel_to_preparation(qapp, monkeypatch):
    from samsara.ui import update_qt
    from PySide6.QtWidgets import QMessageBox
    app = _UpdateApp(beta=True)
    workers = []
    release = SimpleNamespace(version='0.30.0-beta.3', asset_size=123)
    prepare = Mock(return_value=SimpleNamespace(tag='v0.30.0-beta.3'))
    monkeypatch.setattr(update_qt.QMessageBox, 'question', lambda *a, **kw: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(update_qt, 'prepare_update', prepare)
    helper = Mock(side_effect=OSError('synthetic helper boundary'))
    monkeypatch.setattr(update_qt, 'launch_prepared_update', helper)
    monkeypatch.setattr(update_qt.thread_registry, 'spawn', lambda name, target, **kw: workers.append(target))
    dialog = update_qt._UpdateDialog(app, initial_release=release)
    try:
        dialog._primary_clicked()
        assert not dialog._beta_choice.isEnabled()
        workers[0]()
        qapp.processEvents()
        assert prepare.call_args.kwargs['allow_prerelease'] is True
        assert callable(prepare.call_args.kwargs['progress_callback'])
        helper.assert_called_once()
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize('saved,checked', [(True, True), (False, False), ('true', False), (1, False)])
def test_setup_rerun_preserves_only_explicit_boolean_choice(qapp, tmp_path, monkeypatch, saved, checked):
    from samsara.ui import first_run_wizard_qt as wizard, first_run_pages_qt as pages
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **kw: None)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda win: None)
    monkeypatch.setattr(pages, 'cuda_chosen', lambda win: False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'updates': {'include_prerelease': saved}}))
    window = wizard._WizardWindow(path)
    try:
        assert window._decisions.include_beta_updates.isChecked() is checked
        assert json.loads(path.read_text())['updates']['include_prerelease'] == saved
    finally:
        window.close()
        window.deleteLater()


class _Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self._url = url

    def geturl(self):
        return self._url


def _opener_for(mapping):
    def open_request(request, timeout=None):
        return _Response(mapping[request.full_url], request.full_url)
    return open_request


def _release_payload(tag):
    name = f'Samsara-Windows-{tag}.zip'
    base = f'https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/'
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [
        {'name': name, 'size': 123, 'browser_download_url': base + name},
        {'name': name + '.sha256', 'size': 96, 'browser_download_url': base + name + '.sha256'}]}


def _beta(tag):
    release = _release_payload(tag)
    release['prerelease'] = True
    return release


def test_opt_in_queries_release_list_and_selects_newest_published_version(monkeypatch):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    draft = _beta('v9.0.0-beta.1')
    draft['draft'] = True
    releases = [_beta('v0.30.0-beta.9'), draft, _release_payload('v0.29.0'),
                _beta('v0.30.0-beta.11')]
    url = updater.GITHUB_RELEASES_API.removesuffix('/latest') + '?per_page=30'
    release = updater.check_for_update(
        '0.30.0-beta.2', allow_prerelease=True,
        opener=_opener_for({url: json.dumps(releases).encode()}))
    assert release is not None
    assert release.tag == 'v0.30.0-beta.11'


@pytest.mark.parametrize('current,expected', [
    ('0.30.0-beta.11', 'v0.30.0'), ('0.30.0', None), ('0.31.0-beta.1', None)])
def test_beta_channel_prefers_stable_same_version_and_never_downgrades(monkeypatch, current, expected):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    releases = [_beta('v0.30.0-beta.12'), _release_payload('v0.30.0')]
    result = updater.check_for_update(current, allow_prerelease=True,
        opener=_opener_for({updater.GITHUB_RELEASE_LIST_API: json.dumps(releases).encode()}))
    assert (result.tag if result else None) == expected


def test_beta_preparation_without_explicit_opt_in_is_rejected(monkeypatch):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    release = updater.ReleaseInfo(version='0.30.0-beta.3', tag='v0.30.0-beta.3',
        asset_size=123, asset_url='https://github.com/unused',
        checksum_url='https://github.com/unused.sha256', checksum_size=96)
    network = Mock()
    with pytest.raises(updater.ReleaseMetadataError):
        updater.prepare_update(release, opener=network)
    network.assert_not_called()


def test_missing_beta_default_is_false_and_truthy_values_do_not_opt_in():
    from samsara.config_defaults import cfg_get
    from samsara.ui.update_channel_qt import includes_betas
    assert cfg_get({}, 'updates.include_prerelease') is False
    for saved in (None, False, 'true', 1):
        assert not includes_betas({'updates': {'include_prerelease': saved}})
    assert includes_betas({'updates': {'include_prerelease': True}})


def test_explicit_beta_opt_in_stages_checksum_verified_archive(tmp_path, monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('Samsara.exe', b'synthetic new executable')
        bundle.writestr('commands.json', b'{"commands": {}}')
        bundle.writestr('_internal/ctranslate2/ctranslate2.dll', b'synthetic runtime')
        for filename in OWW_MODEL_FILENAMES:
            bundle.writestr('_internal/openwakeword/resources/models/' + filename, b'synthetic model')
    archive = output.getvalue()
    tag = 'v0.30.0-beta.3'
    name = f'Samsara-Windows-{tag}.zip'
    base = f'https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/'
    checksum = f'{hashlib.sha256(archive).hexdigest()}  {name}\n'.encode()
    release = updater.ReleaseInfo(version=tag.removeprefix('v'), tag=tag,
        asset_size=len(archive), asset_url=base + name,
        checksum_url=base + name + '.sha256', checksum_size=len(checksum))
    install = tmp_path / 'Samsara'
    install.mkdir()
    (install / 'Samsara.exe').write_bytes(b'synthetic old executable')
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    monkeypatch.setattr(updater, '_frozen_install_dir', lambda: install)
    monkeypatch.setattr(updater, 'samsara_home_dir', lambda: tmp_path / 'profile')
    prepared = updater.prepare_update(release, allow_prerelease=True,
        opener=_opener_for({base + name: archive, base + name + '.sha256': checksum}))
    assert prepared.tag == tag
    assert (prepared.staged_dir / 'Samsara.exe').read_bytes() == b'synthetic new executable'
    assert (install / 'Samsara.exe').read_bytes() == b'synthetic old executable'


def test_setup_finishes_with_explicit_beta_choice_without_enabling_automatic_checks(qapp, tmp_path, monkeypatch):
    from samsara.ui import first_run_wizard_qt as wizard, first_run_pages_qt as pages
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *args, **kwargs: None)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda window: None)
    monkeypatch.setattr(pages, 'cuda_chosen', lambda window: False)
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', Mock())
    window = wizard._WizardWindow(tmp_path / 'config.json')
    window._config['microphone'] = 7  # Synthetic completed microphone choice.
    try:
        window._step = [name for name, _ in wizard._STEPS].index('Complete')
        window._show_step()
        choice = window._current_page.findChild(QCheckBox, 'includeBetaUpdatesCheckbox')
        assert choice is not None
        assert not choice.isChecked()
        choice.click()
        window._go_next()
        assert window.result['updates']['include_prerelease'] is True
        assert not window.result['updates'].get('automatic_checks', False)
        pages.autostart.sync_run_entry.assert_not_called()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- FINISH IT 3: the Updates window shows all of its text, scrolls when it must ----

def _squeezed_or_overlapping(dialog):
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QLabel
    labels = [w for w in dialog.findChildren(QLabel) if w.isVisibleTo(dialog) and w.text()]
    problems = []
    for label in labels:
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(label.font())
        document.setPlainText(label.text())
        document.setTextWidth(label.width())
        if label.height() + 1 < document.size().height():
            problems.append(('squeezed', label.text()[:30]))
        for other in labels:
            if other is not label and label.geometry().intersects(other.geometry()):
                problems.append(('overlap', label.text()[:30], other.text()[:30]))
    return problems


def _states(update_qt, state):
    saved = state != 'cannot_save'
    config = {}
    def save(updates):
        if not saved:
            return False
        config.update(updates)
        return True
    app = SimpleNamespace(config=config, update_config_and_save=save, config_path=None)
    release = SimpleNamespace(version='0.31.0-beta.1', asset_size=150000000) if state == 'found' else None
    dialog = update_qt._UpdateDialog(app, initial_release=release)
    return dialog


def _show_on_screen_of_height(dialog, height, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(type(dialog), 'screen', lambda self: SimpleNamespace(
        availableGeometry=lambda: QRect(0, 0, 1366, height)))
    dialog.show()
    for _ in range(20):
        QApplication.processEvents()


@pytest.mark.parametrize('state', ['opens', 'ticked', 'found', 'cannot_save'])
def test_updates_window_opens_tall_enough_for_all_its_text(qapp, monkeypatch, state):
    from samsara.ui import update_qt
    dialog = _states(update_qt, state)
    try:
        _show_on_screen_of_height(dialog, 900, monkeypatch)
        if state in ('ticked', 'cannot_save'):
            dialog._beta_choice.setChecked(True)
            qapp.processEvents()
            qapp.processEvents()
        assert _squeezed_or_overlapping(dialog) == []
        assert dialog._scroll.verticalScrollBar().maximum() == 0
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize('state', ['opens', 'ticked', 'found', 'cannot_save'])
def test_updates_window_scrolls_instead_of_clipping_on_a_small_screen(qapp, monkeypatch, state):
    from samsara.ui import update_qt
    dialog = _states(update_qt, state)
    try:
        _show_on_screen_of_height(dialog, 320, monkeypatch)
        if state in ('ticked', 'cannot_save'):
            dialog._beta_choice.setChecked(True)
            qapp.processEvents()
            qapp.processEvents()
        assert dialog.height() <= 320
        assert _squeezed_or_overlapping(dialog) == []
        assert dialog._scroll.verticalScrollBar().maximum() > 0       # the rest is a scroll away
        assert dialog._primary.isVisibleTo(dialog)                    # the buttons stay put
    finally:
        dialog.close()
        dialog.deleteLater()


def _list_opener(payload, seen):
    def opener(request, timeout=None):
        seen.append(request.full_url)
        return _Response(json.dumps(payload).encode(), request.full_url)
    return opener


def test_release_list_asks_for_the_newest_thirty_only(monkeypatch):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    seen = []
    updater.check_for_update('0.30.0-beta.2', allow_prerelease=True,
                             opener=_list_opener([_beta('v0.30.0-beta.3')], seen))
    assert [url.rpartition('?')[2] for url in seen] == ['per_page=30']
    with pytest.raises(updater.ReleaseMetadataError):
        updater._validate_github_api_url(updater.GITHUB_RELEASES_API.removesuffix('/latest') + '?per_page=100')


def test_one_unparsable_tag_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    odd = _beta('v0.30.0-beta.3')
    odd['tag_name'] = 'nightly-build'
    missing = {'draft': False, 'assets': []}
    result = updater.check_for_update('0.30.0-beta.2', allow_prerelease=True,
        opener=_list_opener([odd, missing, _beta('v0.30.0-beta.5')], []))
    assert result.tag == 'v0.30.0-beta.5'


def test_list_with_no_usable_entry_offers_nothing(monkeypatch):
    monkeypatch.setattr(updater, '_require_update_eligible', lambda: None)
    odd = _beta('v0.30.0-beta.3')
    odd['tag_name'] = 'nightly-build'
    draft = _beta('v0.30.0-beta.9')
    draft['draft'] = True
    assert updater.check_for_update('0.30.0-beta.2', allow_prerelease=True,
                                    opener=_list_opener([odd, draft], [])) is None
