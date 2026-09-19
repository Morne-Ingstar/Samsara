from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import support_bundle as bundle


def _make_profile(home: Path, *, config: dict | None = None, log: str | None = "safe log\n") -> None:
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if log is not None:
        logs = home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "samsara.log").write_text(log, encoding="utf-8")


def _mock_system(monkeypatch):
    monkeypatch.setattr(bundle, "_monitor_layout", lambda: ["Monitor 1: 1920x1080 at (0, 0)"])
    monkeypatch.setattr(bundle, "_audio_device_names", lambda: ["Test microphone", "Test speakers"])


def _support_page():
    from PySide6.QtWidgets import QLabel, QVBoxLayout

    from samsara.ui.settings.help_qt import HelpPage

    class Page(HelpPage):
        def __init__(self):
            self.app = SimpleNamespace(config={})
            self._widgets = {}

        def _section_title(self, title):
            return QLabel(title)

        def _setting_row(self, title, description, control):
            layout = QVBoxLayout()
            layout.addWidget(QLabel(title))
            layout.addWidget(QLabel(description))
            layout.addWidget(control)
            return layout

        def _open_update_dialog(self):
            pass

    return Page()._build_support_tab()


def test_bundle_has_only_allowlisted_files_and_adds_previous_log(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Desktop"
    _make_profile(home, config={"model_size": "small"})
    previous = "".join(f"previous-{index}\n" for index in range(1205))
    (home / "logs" / "samsara.log.1").write_text(previous, encoding="utf-8")
    _mock_system(monkeypatch)

    result = bundle.build_bundle(
        home_dir=home,
        desktop_dir=desktop,
        now=datetime(2026, 9, 19, 12, 34),
    )

    assert result.parent == desktop
    assert result.name == "samsara-report_2026-09-19_12-34.zip"
    assert list(desktop.iterdir()) == [result]
    with zipfile.ZipFile(result) as archive:
        assert archive.namelist() == [
            "samsara.log", "samsara.log.1", "config.json", "versions.txt", "system.txt",
        ]
        assert archive.read("samsara.log.1").decode("utf-8").splitlines() == [
            f"previous-{index}" for index in range(1205)
        ]


def test_config_scrubbing_redacts_credentials_but_preserves_safe_structure():
    config = {
        "model_size": "medium",
        "hotkey": "ctrl+shift",
        "cloud_llm": {
            "provider": "deepseek",
            "api_key": "api-secret-sentinel",
            "credential": "cloud-credential-sentinel",
            "nested": {"refresh_token": "token-sentinel", "enabled": True},
        },
        "profile_path": r"C:\private\vault\location",
        "ava_memory": {"turns": ["private-ava-memory-sentinel"]},
        "transcript_cache": "private-transcript-sentinel",
        "last_dictation": "private-dictation-sentinel",
        "memos": ["private-memo-sentinel"],
        "ordinary": {"enabled": True, "retry_count": 3},
    }

    safe = bundle.scrub_config(config)
    rendered = json.dumps(safe)

    assert safe["model_size"] == "medium"
    assert safe["cloud_llm"]["provider"] == "deepseek"
    assert safe["cloud_llm"]["api_key"] == "<redacted>"
    assert safe["cloud_llm"]["credential"] == "<redacted>"
    assert safe["cloud_llm"]["nested"] == {"refresh_token": "<redacted>", "enabled": True}
    assert safe["hotkey"] == "<redacted>"
    assert safe["profile_path"] == "<redacted>"
    assert safe["ordinary"] == {"enabled": True, "retry_count": 3}
    for secret in (
        "api-secret-sentinel", "cloud-credential-sentinel", "token-sentinel",
        "private-ava-memory-sentinel", "private-transcript-sentinel", "private-memo-sentinel",
        "private-dictation-sentinel",
        r"C:\private\vault\location",
    ):
        assert secret not in rendered
    assert "ava_memory" not in safe
    assert "transcript_cache" not in safe
    assert "last_dictation" not in safe
    assert "memos" not in safe


def test_log_tail_is_bounded_to_last_1000_lines(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Desktop"
    lines = "".join(f"line-{index}\n" for index in range(1505))
    _make_profile(home, config={}, log=lines)
    _mock_system(monkeypatch)

    result = bundle.build_bundle(home_dir=home, desktop_dir=desktop, now=datetime(2026, 9, 19, 1, 2))
    with zipfile.ZipFile(result) as archive:
        tail = archive.read("samsara.log").decode("utf-8").splitlines()
    assert len(tail) == 1000
    assert tail[0] == "line-505"
    assert tail[-1] == "line-1504"


def test_missing_log_is_gracefully_represented(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Desktop"
    _make_profile(home, config={}, log=None)
    _mock_system(monkeypatch)

    result = bundle.build_bundle(home_dir=home, desktop_dir=desktop, now=datetime(2026, 9, 19, 1, 2))
    with zipfile.ZipFile(result) as archive:
        assert archive.namelist() == ["samsara.log", "config.json", "versions.txt", "system.txt"]
        assert b"No log file was available" in archive.read("samsara.log")


def test_default_home_and_log_paths_follow_samsara_home_dir(tmp_path, monkeypatch):
    home = tmp_path / "isolated-profile"
    desktop = tmp_path / "Desktop"
    _make_profile(home, config={"model_size": "small"}, log="from override\n")
    _mock_system(monkeypatch)
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))

    result = bundle.build_bundle(desktop_dir=desktop, now=datetime(2026, 9, 19, 1, 2))
    with zipfile.ZipFile(result) as archive:
        assert archive.read("samsara.log").decode("utf-8") == "from override\n"
        assert json.loads(archive.read("config.json"))["model_size"] == "small"


def test_default_output_uses_the_resolved_desktop(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Resolved Desktop"
    _make_profile(home, config={})
    _mock_system(monkeypatch)
    monkeypatch.setattr(bundle, "_desktop_directory", lambda: desktop)

    result = bundle.build_bundle(home_dir=home, now=datetime(2026, 9, 19, 1, 2))
    assert result.parent == desktop
    assert result.name == "samsara-report_2026-09-19_01-02.zip"


def test_bundle_excludes_private_stores_and_scrubs_private_log_lines(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Desktop"
    _make_profile(
        home,
        config={
            "safe": {"enabled": True},
            "vault_path": r"C:\Users\Tester\vault\private",
            "misc_setting": r"C:\Users\Tester\vault\secret-file",
            "ava_memory": {"turns": ["private-ava-memory-sentinel"]},
            "transcript": "private-transcript-sentinel",
            "memo": "private-memo-sentinel",
        },
        log=(
            "ordinary diagnostic line\n"
            "INFO transcript: private-transcript-log-sentinel\n"
            "INFO memo content: private-memo-log-sentinel\n"
            'INFO [HEAR] "private-heard-log-sentinel"\n'
            "INFO [STREAM] Partial: private-stream-log-sentinel\n"
            "INFO [MEMO] Mirrored to vault: C:\\Users\\Tester\\vault\\note.md\n"
            "vault path: C:\\Users\\Tester\\vault\\private\\index.md\n"
            "api_key=private-api-key-sentinel\n"
            "Authorization: Bearer private-auth-sentinel\n"
        ),
    )
    for relative, text in (
        ("transcripts/transcript.json", "private-transcript-file-sentinel"),
        ("memos/memo.json", "private-memo-file-sentinel"),
        ("ava_memory/turns.json", "private-ava-memory-file-sentinel"),
        ("vault/index.md", "private-vault-file-sentinel"),
    ):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _mock_system(monkeypatch)

    result = bundle.build_bundle(home_dir=home, desktop_dir=desktop, now=datetime(2026, 9, 19, 1, 2))

    with zipfile.ZipFile(result) as archive:
        names = archive.namelist()
        contents = b"\n".join(archive.read(name) for name in names).decode("utf-8")
    assert names == ["samsara.log", "config.json", "versions.txt", "system.txt"]
    for private in (
        "private-transcript-sentinel", "private-memo-sentinel", "private-ava-memory-sentinel",
        "private-transcript-log-sentinel", "private-memo-log-sentinel",
        "private-heard-log-sentinel", "private-stream-log-sentinel", "private-auth-sentinel",
        "private-api-key-sentinel", "private-transcript-file-sentinel", "private-memo-file-sentinel",
        "private-ava-memory-file-sentinel", "private-vault-file-sentinel", r"C:\Users\Tester\vault",
    ):
        assert private not in contents
    assert "ordinary diagnostic line" in contents
    assert "<redacted>" in contents
    assert "vault files/paths" in contents


def test_versions_and_system_include_requested_safe_facts(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    desktop = tmp_path / "Desktop"
    _make_profile(home, config={"device": "cuda", "model_size": "small.en", "compute_type": "float16"})
    _mock_system(monkeypatch)
    monkeypatch.setattr(bundle.platform, "system", lambda: "Windows")
    monkeypatch.setattr(bundle.platform, "release", lambda: "11")
    monkeypatch.setattr(bundle.platform, "version", lambda: "test-build-123")

    result = bundle.build_bundle(home_dir=home, desktop_dir=desktop, now=datetime(2026, 9, 19, 1, 2))
    with zipfile.ZipFile(result) as archive:
        versions = archive.read("versions.txt").decode("utf-8")
        system = archive.read("system.txt").decode("utf-8")
    assert "Samsara:" in versions
    assert "Python:" in versions
    assert "Compute mode: GPU requested (CUDA)" in versions
    assert "Speech model: small.en" in versions
    assert "Speech compute type: float16" in versions
    assert "PySide6:" in versions
    assert "Operating system: Windows 11" in system
    assert "OS build: test-build-123" in system
    assert "Monitor 1: 1920x1080" in system
    assert "Test microphone" in system
    assert "Test speakers" in system


def test_versions_ignore_malformed_config_values():
    text = bundle._version_text({
        "device": {"token": "private-device-token-sentinel"},
        "model_size": ["private-model-value-sentinel"],
        "compute_type": "private-compute-value-sentinel",
    })
    assert "Compute mode: Unknown" in text
    assert "Speech model: unknown" in text
    assert "Speech compute type: unknown" in text
    assert "private-device-token-sentinel" not in text
    assert "private-model-value-sentinel" not in text
    assert "private-compute-value-sentinel" not in text


def test_help_support_button_creates_bundle_and_opens_folder(qapp, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QLabel, QPushButton

    from samsara import support_bundle

    expected = tmp_path / "samsara-report_2026-09-19_12-34.zip"
    monkeypatch.setattr(support_bundle, "build_bundle", lambda: expected)
    opened = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    page = _support_page()
    try:
        report = page.findChild(QPushButton, "createSupportBundleButton")
        email = page.findChild(QPushButton, "betaFeedbackButton")
        open_folder = page.findChild(QPushButton, "openSupportBundleFolderButton")
        status = page.findChild(QLabel, "feedbackStatusLabel")
        assert report is not None and report.text() == "Report a problem"
        assert email is not None and email.text() == "Email the developer"
        assert open_folder is not None and not open_folder.isEnabled()
        report.click()
        assert str(expected) in status.text()
        assert open_folder.isEnabled()
        open_folder.click()
        assert len(opened) == 1
        assert Path(opened[0]) == expected.parent
        assert "Opened the report folder" in status.text()
    finally:
        page.deleteLater()


def test_help_support_button_reports_build_failure(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel, QPushButton

    from samsara import support_bundle

    def fail():
        raise OSError("desktop unavailable")

    monkeypatch.setattr(support_bundle, "build_bundle", fail)
    page = _support_page()
    try:
        report = page.findChild(QPushButton, "createSupportBundleButton")
        open_folder = page.findChild(QPushButton, "openSupportBundleFolderButton")
        status = page.findChild(QLabel, "feedbackStatusLabel")
        report.click()
        assert "Could not create the report bundle" in status.text()
        assert "desktop unavailable" in status.text()
        assert not open_folder.isEnabled()
    finally:
        page.deleteLater()
