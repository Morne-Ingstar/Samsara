"""Synthetic folders, fake HKCU records and fake installer commands only."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from samsara import updater as up
from samsara import update_installer as ins
requires_installer = pytest.mark.skipif(not hasattr(ins,'PreparedInstaller'),reason='installer path intentionally absent from guard-only patch')


class Response(io.BytesIO):
    def __init__(self,data,url):
        super().__init__(data);self.url=url
    def geturl(self): return self.url


def opener(mapping):
    return lambda request,timeout=None: Response(mapping[request.full_url],request.full_url)


def owner(folder,**values):
    return dict(InstallLocation=str(folder),registry_name=ins.APP_KEY,
                **{'Inno Setup: Selected Components':'core,cuda_pack,wake_word_models'},**values)


@pytest.mark.parametrize('uninstaller,registered,expected',[(True,True,'installed'),(False,True,'damaged'),(True,False,'portable'),(False,False,'portable')])
def test_install_ownership_requires_both_markers(tmp_path,uninstaller,registered,expected):
    if uninstaller: (tmp_path/'unins000.exe').write_bytes(b'synthetic')
    records=[owner(tmp_path)] if registered else []
    assert ins.installation(tmp_path,entries=records).kind==expected


def test_no_live_registry_for_source_tests(tmp_path,monkeypatch):
    monkeypatch.setattr(ins.sys,'frozen',False,raising=False)
    read=Mock(side_effect=AssertionError('must not read live registry'))
    monkeypatch.setattr(ins,'read_uninstall_entries',read)
    assert ins.installation(tmp_path).kind=='portable'
    read.assert_not_called()


def installed(tmp_path,monkeypatch):
    folder=tmp_path/'Samsara';folder.mkdir()
    (folder/'Samsara.exe').write_bytes(b'old')
    (folder/'unins000.exe').write_bytes(b'uninstaller')
    (folder/'unins000.dat').write_bytes(b'uninstaller-data')
    (folder/'components').mkdir();(folder/'components'/'keep.dat').write_bytes(b'keep')
    info=ins.installation(folder,entries=[owner(folder)])
    monkeypatch.setattr(ins,'installation',lambda folder=None,**kw: info)
    monkeypatch.setattr(up,'_require_update_eligible',lambda: None)
    monkeypatch.setattr(up,'_frozen_install_dir',lambda: folder)
    monkeypatch.setattr(up,'samsara_home_dir',lambda: tmp_path/'profile')
    return folder


def payload(tag='v0.31.0',installer=True,sidecar=True):
    name=f'SamsaraSetup-{tag.removeprefix("v")}.exe' if installer else f'Samsara-Windows-{tag}.zip'
    base=f'https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/'
    assets=[dict(name=name,size=9,browser_download_url=base+name)]
    if sidecar: assets.append(dict(name=name+'.sha256',size=100,browser_download_url=base+name+'.sha256'))
    return dict(tag_name=tag,draft=False,prerelease='-' in tag,assets=assets)


@pytest.mark.parametrize('sidecar',[True,False])
@requires_installer
def test_installed_discovery_requires_installer_sidecar(tmp_path,monkeypatch,sidecar):
    installed(tmp_path,monkeypatch)
    api=up.GITHUB_RELEASES_API
    if sidecar:
        result=up.check_for_update('0.30.0',opener=opener({api:json.dumps(payload(sidecar=True)).encode()}))
        assert result.asset_url.endswith('SamsaraSetup-0.31.0.exe')
    else:
        result=up.check_for_update('0.30.0',opener=opener({api:json.dumps(payload(sidecar=False)).encode()}))
        assert result.installer_link_only


@requires_installer
def test_beta_channel_is_shared_by_installed_discovery(tmp_path,monkeypatch):
    installed(tmp_path,monkeypatch)
    releases=[payload('v0.31.0-beta.2'),payload('v0.31.0-beta.11')]
    result=up.check_for_update('0.31.0-beta.1',allow_prerelease=True,
        opener=opener({up.GITHUB_RELEASE_LIST_API:json.dumps(releases).encode()}))
    assert result.tag=='v0.31.0-beta.11'
    assert result.asset_url.endswith('SamsaraSetup-0.31.0-beta.11.exe')


def test_guard_refuses_installed_archive_preparation_without_download(tmp_path,monkeypatch):
    installed(tmp_path,monkeypatch)
    meta=payload(installer=False)
    network=Mock()
    release=up.ReleaseInfo('0.31.0','v0.31.0',9,meta['assets'][0]['browser_download_url'],meta['assets'][1]['browser_download_url'],100)
    with pytest.raises(up.UpdateNotSupported,match='installer'):
        up.prepare_update(release,opener=network)
    network.assert_not_called()


@pytest.mark.parametrize('checksum_ok,tag,allow',[(True,'v0.31.0',False),(False,'v0.31.0',False),(True,'v0.31.0-beta.2',True)])
@requires_installer
def test_prepare_installer_preserves_old_folder_and_components(tmp_path,monkeypatch,checksum_ok,tag,allow):
    folder=installed(tmp_path,monkeypatch)
    data=b'synthetic installer'
    name=f'SamsaraSetup-{tag.removeprefix("v")}.exe';base=f'https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/'
    checksum=((hashlib.sha256(data).hexdigest() if checksum_ok else '0'*64)+'  '+name+'\n').encode()
    release=up.ReleaseInfo(tag.removeprefix('v'),tag,len(data),base+name,base+name+'.sha256',len(checksum))
    kwargs=dict(opener=opener({base+name:data,base+name+'.sha256':checksum}),disk_usage=lambda path:SimpleNamespace(free=10**12),allow_prerelease=allow)
    if checksum_ok:
        result=up.prepare_update(release,**kwargs)
        assert isinstance(result,ins.PreparedInstaller)
        assert (result.staged_dir/'unins000.dat').read_bytes()==b'uninstaller-data'
        assert (result.staged_dir/'components'/'keep.dat').read_bytes()==b'keep'
        assert result.components=='core,cuda_pack,wake_word_models'
    else:
        with pytest.raises(up.UpdateIntegrityError): up.prepare_update(release,**kwargs)
        assert not list(tmp_path.glob('.Samsara-update-*'))
    assert (folder/'Samsara.exe').read_bytes()==b'old'


@requires_installer
def test_silent_arguments_preserve_folder_and_components_without_elevation_or_force():
    args=ins.installer_arguments(Path('C:/Synthetic folder'),'core,cuda_pack')
    assert '/CURRENTUSER' in args and '/NoFetch' in args
    assert '/NOCLOSEAPPLICATIONS' in args and '/NORESTARTAPPLICATIONS' in args
    assert '/COMPONENTS=core,cuda_pack' in args
    assert '/DIR=C:\\Synthetic folder' in args
    assert not any('FORCE' in arg or 'ALLUSERS' in arg for arg in args)
    with pytest.raises(up.UpdateError): ins.installer_arguments(Path('C:/Synthetic'),'evil')


def test_damaged_install_notice_is_once_and_does_not_write_registry(tmp_path,monkeypatch):
    info=ins.installation(tmp_path,entries=[owner(tmp_path)])
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: info)
    monkeypatch.setattr(ins,'_repair_reported',False)
    assert ins.repair_notice().message.endswith(ins.RELEASES_URL)
    assert ins.repair_notice() is None


def test_updates_window_has_installer_link_and_no_network(qapp,monkeypatch):
    from samsara.ui import update_qt
    from PySide6.QtCore import QCoreApplication,QEvent
    from PySide6.QtWidgets import QLabel
    monkeypatch.setattr(ins,'dialog_notice',lambda: ins.GUARD_MESSAGE)
    network=Mock();monkeypatch.setattr(update_qt,'check_for_update',network)
    dialog=update_qt._UpdateDialog(SimpleNamespace(config={}))
    try:
        note=dialog.findChild(QLabel,'installerUpdateNotice')
        assert ins.RELEASES_URL in note.text() and note.openExternalLinks()
        network.assert_not_called()
    finally:
        dialog.close();dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)



@pytest.mark.parametrize('location',['','relative','same',None])
@pytest.mark.parametrize('marker',[False,True])
def test_other_program_records_never_own_cwd_folder(tmp_path,monkeypatch,location,marker):
    monkeypatch.chdir(tmp_path)
    if marker: (tmp_path/'unins000.exe').write_bytes(b'fake')
    value=str(tmp_path) if location=='same' else location
    records=[dict(registry_name='OtherProgramOne',InstallLocation=value),
             dict(registry_name='OtherProgramTwo',InstallLocation=value)]
    info=ins.installation(tmp_path,entries=records)
    assert info.kind=='portable'
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: info)
    real=up.update_unavailable_reason
    monkeypatch.setattr(up,'update_unavailable_reason',lambda: real(frozen=True,platform_name='nt',home_override='',disable_override=''))
    assert isinstance(up.update_check_menu_label(),str)
    assert ins.archive_guard() is None
    assert ins.dialog_notice() is None
    assert ins.repair_notice() is None


@pytest.mark.parametrize('hive',['HKCU','HKLM','neither'])
@pytest.mark.parametrize('view',[64,32])
def test_own_key_hives_and_views_protect_tray_and_silent_route(tmp_path,monkeypatch,hive,view):
    (tmp_path/'unins000.exe').write_bytes(b'fake')
    records=[] if hive=='neither' else [dict(owner(tmp_path),hive=hive,view=view)]
    info=ins.installation(tmp_path,entries=records)
    assert info.kind==('portable' if hive=='neither' else 'installed')
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: info)
    real=up.update_unavailable_reason
    monkeypatch.setattr(up,'update_unavailable_reason',lambda: real(frozen=True,platform_name='nt',home_override='',disable_override=''))
    assert isinstance(up.update_check_menu_label(),str)
    if hive=='HKLM':
        assert ins.routing_guard()==ins.GUARD_MESSAGE
        assert ins.dialog_notice()==ins.GUARD_MESSAGE
        if hasattr(ins,'prepare'):
            with pytest.raises(up.UpdateNotSupported):
                ins.prepare(None,tmp_path,None,opener=Mock(),timeout=1,disk_usage=Mock(),allow_prerelease=False)


@pytest.mark.parametrize('marker',[False,True])
def test_registry_reader_raises_are_safe_and_logged_once(tmp_path,monkeypatch,caplog,marker):
    if marker: (tmp_path/'unins000.exe').write_bytes(b'fake')
    monkeypatch.setattr(ins.sys,'frozen',True,raising=False)
    monkeypatch.setattr(ins,'_detection_issue_logged',False)
    reader=Mock(side_effect=PermissionError('synthetic denied'))
    monkeypatch.setattr(ins,'read_uninstall_entries',reader)
    real_installation=ins.installation
    info=real_installation(tmp_path)
    assert info.kind==('installed' if marker else 'portable')
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: real_installation(tmp_path))
    real=up.update_unavailable_reason
    monkeypatch.setattr(up,'update_unavailable_reason',lambda: real(frozen=True,platform_name='nt',home_override='',disable_override=''))
    for _ in range(3):
        assert isinstance(up.update_check_menu_label(),str)
        assert ins.archive_guard()==(ins.GUARD_MESSAGE if marker else None)
        assert ins.dialog_notice()==(ins.GUARD_MESSAGE if marker else None)
        assert ins.repair_notice() is None
    assert sum('registry unreadable' in r.message for r in caplog.records)==1


@pytest.mark.parametrize('raw',['','relative',None,123])
def test_own_invalid_installlocation_cannot_claim_folder(tmp_path,monkeypatch,raw):
    info=ins.installation(tmp_path,entries=[dict(owner(tmp_path),InstallLocation=raw)])
    assert info.kind=='portable'
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: info)
    assert isinstance(up.update_check_menu_label(),str)


def test_unresolvable_own_path_returns_and_blocks_archive_with_marker(tmp_path,monkeypatch):
    (tmp_path/'unins000.exe').write_bytes(b'fake')
    bad=tmp_path/'bad'
    original=type(tmp_path).resolve
    def resolve(path,*a,**kw):
        if path==bad: raise OSError('synthetic unresolved path')
        return original(path,*a,**kw)
    monkeypatch.setattr(type(tmp_path),'resolve',resolve)
    info=ins.installation(tmp_path,entries=[dict(owner(tmp_path),InstallLocation=str(bad))])
    assert info.kind=='installed' and info.hive=='unknown'
    monkeypatch.setattr(ins,'installation',lambda *a,**kw: info)
    real=up.update_unavailable_reason
    monkeypatch.setattr(up,'update_unavailable_reason',lambda: real(frozen=True,platform_name='nt',home_override='',disable_override=''))
    assert isinstance(up.update_check_menu_label(),str)
    assert ins.archive_guard()==ins.GUARD_MESSAGE


def test_reader_opens_only_app_key_in_all_hives_and_views(monkeypatch):
    import sys
    opened=[]
    class Key:
        def __enter__(self): return self
        def __exit__(self,*a): pass
    def open_key(hive,path,reserved,flags):
        assert path==ins.UNINSTALL_ROOT+'\\'+ins.APP_KEY
        opened.append((hive,flags));return Key()
    fake=SimpleNamespace(HKEY_CURRENT_USER='user',HKEY_LOCAL_MACHINE='machine',KEY_READ=1,
        KEY_WOW64_64KEY=256,KEY_WOW64_32KEY=512,OpenKey=open_key,
        QueryValueEx=lambda key,name: ('C:/Synthetic' if name=='InstallLocation' else 'core',1),
        EnumKey=Mock(side_effect=AssertionError('must not enumerate')))
    monkeypatch.setitem(sys.modules,'winreg',fake)
    records=ins.read_uninstall_entries()
    assert len(records)==4
    assert set(opened)=={('user',257),('user',513),('machine',257),('machine',513)}
    fake.EnumKey.assert_not_called()


def test_owner_error_sentence_is_exact_without_exception_text(qapp):
    from samsara.ui import update_qt
    from PySide6.QtCore import QCoreApplication,QEvent
    dialog=update_qt._UpdateDialog(SimpleNamespace(config={}))
    try:
        dialog._on_check_finished(None,'synthetic private detail')
        assert dialog._status.text()=="Couldn’t check for updates. Samsara remains unchanged. Check your connection and try again."
    finally:
        dialog.close();dialog.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


@requires_installer
@pytest.mark.parametrize('missing',['installer','checksum'])
def test_missing_installer_pair_returns_link_instead_of_error(tmp_path,monkeypatch,qapp,missing):
    installed(tmp_path,monkeypatch)
    meta=payload()
    meta['assets']=[a for a in meta['assets'] if (a['name'].endswith('.sha256') if missing=='installer' else not a['name'].endswith('.sha256'))]
    offer=up.check_for_update('0.30.0',opener=opener({up.GITHUB_RELEASES_API:json.dumps(meta).encode()}))
    assert offer.installer_link_only
    from samsara.ui import update_qt
    from PySide6.QtCore import QCoreApplication,QEvent
    dialog=update_qt._UpdateDialog(SimpleNamespace(config={}))
    try:
        dialog._on_check_finished(offer,'')
        assert dialog._phase=='installer'
        assert dialog._status.text()==ins.GUARD_MESSAGE
        assert ins.RELEASES_URL in dialog._installer_note.text()
        opened=Mock();monkeypatch.setattr(update_qt.QDesktopServices,'openUrl',opened)
        dialog._primary_clicked()
        opened.assert_called_once()
    finally:
        dialog.close();dialog.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
