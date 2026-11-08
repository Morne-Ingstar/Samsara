"""Useful reports, privacy, packaged identities and conservative exit reasons."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara.bug_report import capture, code_index, crash, metadata, session_exit, upload
from samsara.bug_report.build_data import report_datas
from samsara.bug_report.preview_qt import ReportPreview
from samsara.bug_report.redact import redact_fault, redact_lines


def test_last_200_events_retain_levels_errors_measurements_and_own_frames(tmp_path):
    source = [f"[AUDIO] elapsed_ms={i} samples=48000" for i in range(220)]
    source += ['2026-11-07 12:00:00,123 - ERROR - [AUDIO] PortAudioError elapsed_ms=32',
               '  File "C:/Users/ExamplePrivate/samsara/bug_report/capture.py", line 3, in snapshot',
               '  File "C:/Users/ExamplePrivate/plugins/commands/session_mode_commands.py", line 3, in <module>',
               '  File "C:/Users/ExamplePrivate/private.py", line 3, in private_method',
               '[ASR] transcript="private dictated sample"',
               '[FOCUS] title="Private document name"',
               '[MIC] device_name="Private device name"',
               '[PATH] file=C:/Users/ExamplePrivate/private.txt',
               '[MAIL] address=private@example.invalid']
    result = "\n".join(redact_lines(source))
    assert len(redact_lines(source)) == 200
    assert "elapsed_ms=0 " not in result and "elapsed_ms=219" in result
    assert "2026-11-07 12:00:00,123 - ERROR - [AUDIO] PortAudioError" in result
    assert "elapsed_ms=32" in result and "samples=48000" in result
    assert "<user>/samsara/bug_report/capture.py" in result
    assert "<user>/plugins/commands/session_mode_commands.py" in result
    for private in ("private dictated sample", "Private document name", "Private device name",
                    "ExamplePrivate", "private.txt", "private@example.invalid", "private_method"):
        assert private not in result


def test_shipped_templates_redact_dynamic_values_but_keep_events_and_timings(tmp_path, monkeypatch):
    root = tmp_path / "source"
    code = root / "samsara" / "audio.py"
    code.parent.mkdir(parents=True)
    code.write_text('def callback():\n logger.error(f"[AUDIO] callback failed elapsed_ms={elapsed}ms device={device}")\n logger.info(f"[ASR] Transcribed: {text}")\n')
    private_plugin = root / "plugins" / "local_example.py"
    private_plugin.parent.mkdir()
    private_plugin.write_text('def private_function(): pass\n')
    data = code_index.create_index(root)
    assert "plugins/local_example.py" not in data["frames"]
    monkeypatch.setattr(code_index, "index", lambda: data)
    code_index.templates.cache_clear()
    try:
        lines = redact_lines(['[AUDIO] callback failed elapsed_ms=52ms device=Private Microphone',
                              '[ASR] Transcribed: 12345'])
        assert lines[0] == '[AUDIO] callback failed elapsed_ms=52ms device=<value redacted>'
        assert lines[1] == '[ASR] Transcribed: <value redacted>'
        assert 'Private Microphone' not in "\n".join(lines) and '12345' not in "\n".join(lines)
    finally:
        code_index.templates.cache_clear()


def test_frozen_frame_validation_uses_shipped_index_without_source(tmp_path, monkeypatch):
    monkeypatch.setattr(code_index, "index", lambda: {"frames": {"plugins/commands/example.py": ["run"]}})
    assert redact_fault(['File "C:/Users/Private/plugins/commands/example.py", line 12, in run']) == (
        'File "<user>/plugins/commands/example.py", line 12 in run',)
    assert redact_fault(['File "C:/Users/Private/plugins/commands/example.py", line 12, in unknown']) == ()


def test_report_has_changed_keys_not_values_and_recent_catalog_names(tmp_path, monkeypatch):
    from samsara.outcome_ring import OutcomeRecord
    monkeypatch.setattr(metadata, "build_commit", lambda: "a" * 40)
    app = SimpleNamespace(config={"ui_scale": 1.3, "report_upload_contact": "private@example.invalid",
                                 "private_config_key": "private configuration value", "microphone": "Private Mic"},
                          _bug_report_log_dir=tmp_path,
                          _outcome_ring=[OutcomeRecord("private words", "success", 1, "command_executed", "ask_agent.ask_hermes"),
                                         OutcomeRecord("private words", "error", 2, "command_failed", "private name")])
    (tmp_path / "samsara.log").write_text('[AUDIO] elapsed_ms=12')
    report = capture.snapshot(app)
    assert "Commit: " + "a" * 40 in report and capture.DIAGNOSTICS_HEADING in report
    assert "ui_scale" in report and "report_upload_contact" in report
    assert "Recent commands: ask_agent.ask_hermes" in report
    assert "configured device (name omitted)" in report
    assert all(value not in report for value in ("private@example.invalid", "Private Mic", "private words", "private configuration value", "private_config_key"))


def test_packaged_daily_build_info_and_shipped_metadata_are_read_without_git(tmp_path):
    install = tmp_path / "install"
    internal = install / "_internal"
    (internal / "release").mkdir(parents=True)
    (install / "BUILD_INFO.txt").write_text("commit " + "b" * 40 + "\nsubject ignored\n")
    (internal / "release/build_info.json").write_text(json.dumps({"commit": "c" * 40}))
    assert metadata.build_commit(internal, install / "Samsara.exe", True) == "b" * 40
    (install / "BUILD_INFO.txt").unlink()
    assert metadata.build_commit(internal, install / "Samsara.exe", True) == "c" * 40
    (internal / "release/build_info.json").write_text('{"commit":"private text"}')
    assert metadata.build_commit(internal, install / "Samsara.exe", True) == "not recorded in this build"


@pytest.mark.parametrize("detached", [False, True])
def test_source_identity_reads_worktree_head_without_a_git_process(tmp_path, detached):
    root = tmp_path / "source"
    root.mkdir()
    gitdir = tmp_path / "git" if detached else root / ".git"
    gitdir.mkdir()
    if detached:
        (root / ".git").write_text("gitdir: " + str(gitdir))
        (gitdir / "HEAD").write_text("d" * 40)
    else:
        (gitdir / "HEAD").write_text("ref: refs/heads/main")
        (gitdir / "packed-refs").write_text("d" * 40 + " refs/heads/main\n")
    assert metadata.build_commit(root, root / "python.exe", False) == "d" * 40


def test_build_hook_packages_commit_code_index_and_optional_injection(tmp_path):
    root = tmp_path / "source"
    (root / ".git").mkdir(parents=True)
    (root / ".git/HEAD").write_text("e" * 40)
    injection = tmp_path / "injected.json"
    injection.write_text("{}")  # No credential is needed to verify inclusion by path.
    result = report_datas(root, tmp_path / "build", upload_config=injection)
    assert (str(injection), "samsara/data") in result
    assert json.loads((tmp_path / "build/build_info.json").read_text())["commit"] == "e" * 40
    assert (tmp_path / "build/report_code.json").is_file()
    assert not (root / "samsara/data/report_upload.json").exists()
    assert "SAMSARA_REPORT_UPLOAD_CONFIG" in (Path(__file__).resolve().parents[1] / "scripts/samsara.spec").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault,expected", [([], "restart"), (["Windows fatal exception: access violation"], "crash")])
def test_reboot_is_not_a_crash_but_real_fault_survives_a_later_reboot(fault, expected):
    started = "2026-11-07 12:00:00"
    boot = datetime.strptime(started, "%Y-%m-%d %H:%M:%S").timestamp() + 3600
    kind, message = session_exit.classify({"started": started}, fault, boot_time=boot)
    assert kind == expected
    if kind == "restart":
        assert message == "Samsara was closed by a Windows restart"


@pytest.mark.parametrize("previous", [{"started": "unknown"}, {"pid": 999}, {"started": "2026-11-07 12:00:00"}])
def test_unrecorded_termination_is_unknown_and_never_offers_a_crash(previous, monkeypatch):
    monkeypatch.setattr(session_exit, "windows_boot_time", lambda: 0)
    app = SimpleNamespace()
    catch = crash.CrashCatch(previous)
    assert catch.kind == "unknown" and catch.take() is None
    assert crash.offer(app, catch) is None
    assert "Task Manager" in app._bug_report_previous_exit


def test_reboot_reason_is_in_preview_without_a_crash_offer(monkeypatch):
    monkeypatch.setattr(session_exit, "windows_boot_time", lambda: datetime(2026, 11, 7, 13).timestamp())
    app = SimpleNamespace(config={})
    assert crash.offer(app, crash.CrashCatch({"started": "2026-11-07 12:00:00"})) is None
    assert "Previous session: Samsara was closed by a Windows restart" in capture.snapshot(app)


def test_save_and_copy_retain_exact_reviewed_zip_without_network(qapp, tmp_path, monkeypatch, qt_widget_scope):
    from PySide6.QtWidgets import QApplication
    copied = []
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    monkeypatch.setattr(upload, "BUILD_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(upload, "send_bundle", lambda *a, **kw: pytest.fail("Save/Copy sent a report"))
    with qt_widget_scope(ReportPreview("Synthetic diagnostics", directory=tmp_path)) as window:
        window.save.click()
        assert window.saved_path.read_bytes() == window.bundle.data
        assert str(window.saved_path) in window.status.text()
        assert window.open_folder.isHidden() is False
        window.copy.click()
        assert copied == [window.bundle.preview()]
        saved = window.saved_path
        window.close()
        assert saved.is_file()
