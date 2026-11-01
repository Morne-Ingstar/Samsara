"""Offline uninstall, availability and missing-changelog regressions for 994."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import itertools
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import single_instance
from samsara.ui import first_run_qt as fq
from tools import write_manifest_defines


ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "installer/samsara.iss").read_text(encoding="utf-8")
PACKS = {
    "wake_word_models": "WakeWordModels",
    "cuda_pack": "CudaPack",
    "command_model": "CommandModel",
    "gesture_control": "GestureControl",
}


@pytest.fixture
def world(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "Samsara.exe").write_bytes(b"fixture")
    entries = []
    for cid, name in (("core", "Samsara"), ("wake-word-models", "Wake-word models"),
                      ("cuda-pack", "CUDA pack"), ("command-model", "Command model")):
        entries.append({
            "id": cid, "name": name, "available": cid != "command-model",
            "size_bytes": 123, "kind": "core" if cid == "core" else "model",
            "install_dir": "." if cid == "core" else "models/" + cid,
            "filename": cid + ".zip", "sha256": "0" * 64, "requires": [],
        })
    return {"manifest": {"components": entries}, "app_root": app_root,
            "downloads": tmp_path / "downloads"}


@pytest.mark.parametrize("flags", itertools.product((False, True), repeat=4))
def test_installer_offers_exactly_published_components(flags):
    """Evaluate the actual component guards for every publication combination."""
    values = dict(zip(PACKS.values(), flags))
    body = ISS.split("[Components]", 1)[1].split("[Files]", 1)[0]
    active = True
    offered = []
    for line in body.splitlines():
        if line.startswith("#if "):
            match = re.fullmatch(r"#if (\w+)Available != 0", line)
            assert match, line
            active = values[match[1]]
        elif line == "#endif":
            active = True
        elif active and line.startswith("Name:"):
            offered.append(re.search(r'Name: "([a-z_]+)"', line)[1])
    assert offered == ["core", *(name for name, define in PACKS.items() if values[define])]
    for define in PACKS.values():
        assert f"#ifndef {define}Available\n  #define {define}Available 0" in ISS


def test_manifest_defines_publish_and_withdraw_every_pack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "installer").mkdir()
    manifest = tmp_path / "manifest.json"
    for available in (False, True, False):
        manifest.write_text(json.dumps({
            "release_tag": "v9.9.9",
            "components": [{"id": cid, "available": available, "size_bytes": 123}
                           for cid in write_manifest_defines.NAMES],
        }), encoding="utf-8")
        write_manifest_defines.main(str(manifest))
        defines = (tmp_path / "installer/manifest_defines.iss").read_text()
        for name in PACKS.values():
            assert f"#define {name}Available {int(available)}" in defines


@pytest.mark.parametrize("include_installed", (False, True))
def test_addon_rows_follow_publication_on_refresh(qapp, world, include_installed):
    from PySide6.QtWidgets import QLabel, QWidget

    host = QWidget()
    page = fq.ComponentsPage(
        host, on_later=lambda: None, manifest_loader=lambda: world["manifest"],
        app_root=world["app_root"], downloads_dir=world["downloads"],
        gpu_probe=lambda: True, run_in_thread=False, include_installed=include_installed,
    )
    try:
        command = world["manifest"]["components"][-1]
        cuda = world["manifest"]["components"][2]
        cuda["available"] = False
        for published in (False, True, False):
            command["available"] = published
            command["size_bytes"] = 123 if published else None
            page.refresh()
            qapp.processEvents()
            assert ("command-model" in page._rows) is published
            assert "cuda-pack" not in page._rows
            assert all(c["available"] for c in page.listed)
            assert not any("Coming soon" in label.text()
                           for label in host.findChildren(QLabel) if label.isVisibleTo(host))
    finally:
        host.close()
        host.deleteLater()


def test_missing_changelog_is_read_and_logged_only_once(tmp_path, monkeypatch):
    from samsara.ui import home_qt

    path = tmp_path / "missing-CHANGELOG.md"
    reads = Mock(side_effect=FileNotFoundError("missing changelog fixture"))
    log = Mock()
    monkeypatch.setattr(Path, "read_text", reads)
    monkeypatch.setattr(home_qt.logger, "debug", log)
    for _ in range(100):
        assert home_qt.changelog_summary(path) == (None, None)
    assert reads.call_count == 1
    assert log.call_count == 1


def test_quit_ipc_uses_draft_aware_exit_without_bypassing_it(qapp, monkeypatch):
    quit_app = Mock()
    monkeypatch.setattr(sys.modules["__main__"], "app", SimpleNamespace(quit_app=quit_app), raising=False)
    monkeypatch.setattr(single_instance, "_activation_receiver", None)
    ids = {single_instance._ACTIVATION_MESSAGE: 0xC100,
           single_instance.UNINSTALL_QUIT_MESSAGE: 0xC101}
    monkeypatch.setattr(single_instance, "_activation_user32",
                        lambda: SimpleNamespace(RegisterWindowMessageW=ids.__getitem__))
    single_instance._create_activation_receiver("994.fake.profile")
    receiver = single_instance._activation_receiver
    try:
        msg = wintypes.MSG()
        msg.message = ids[single_instance.UNINSTALL_QUIT_MESSAGE]
        assert receiver.nativeEvent(None, ctypes.addressof(msg)) == (True, 1)
        assert not quit_app.called  # quit is queued on Qt, as for a tray action
        qapp.processEvents()
        quit_app.assert_called_once_with()
    finally:
        receiver.deleteLater()


def test_uninstall_checks_exact_install_and_retains_retry_on_failure():
    assert f"AppMutex={single_instance.DEFAULT_MUTEX_NAME}" in ISS
    assert "CloseApplications=yes" in ISS
    assert "RestartApplications=no" in ISS
    assert single_instance.UNINSTALL_QUIT_MESSAGE in ISS
    assert "QueryFullProcessImageName(Handle, 0, Name, Size)" in ISS and "ExpandConstant('{app}\\{#AppExeName}')" in ISS
    assert "if UninstallSilent() then Abort" in ISS
    stop = ISS.split("procedure EnsureInstallStopped();", 1)[1].split("function CleanPayload", 1)[0]
    assert stop.index("RequestInstallQuit();") < stop.index("WaitForInstallExit()") < stop.index("TerminateProcess(Handle, 0)")
    handler = ISS.split("procedure CurUninstallStepChanged", 1)[1].split("end;", 1)[0]
    assert "usAppMutexCheck" in handler and "usUninstall" in handler
    assert handler.index("EnsureInstallStopped();") < handler.index("CleanPayload")
    assert handler.index("False, True") < handler.index("True, True")
    assert "Abort;" in handler
    assert "Close Samsara" in ISS and "Cancel" in ISS
