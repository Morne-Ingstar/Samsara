"""Unit tests for tools/frozen_smoke.py's log-parsing and process-check
helpers. No real build or EXE launch -- these exercise pure functions and
the log-polling loop against a plain temp file."""
import importlib.util
import threading
import time
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "frozen_smoke.py"
_spec = importlib.util.spec_from_file_location("frozen_smoke", _MODULE_PATH)
frozen_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frozen_smoke)


def test_make_min_config_skips_wizard():
    cfg = frozen_smoke.make_min_config()
    assert cfg["first_run_complete"] is True
    assert cfg["microphone"] is not None


def test_read_new_lines_missing_file_returns_empty(tmp_path):
    log_path = tmp_path / "nope.log"
    lines, offset = frozen_smoke.read_new_lines(log_path, 0)
    assert lines == []
    assert offset == 0


def test_read_new_lines_incremental(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text("line1\nline2\n", encoding="utf-8")
    lines, offset = frozen_smoke.read_new_lines(log_path, 0)
    assert lines == ["line1", "line2"]

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("line3\n")
    more, offset2 = frozen_smoke.read_new_lines(log_path, offset)
    assert more == ["line3"]
    assert offset2 > offset


def test_read_new_lines_no_new_data_keeps_offset(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text("line1\n", encoding="utf-8")
    _, offset = frozen_smoke.read_new_lines(log_path, 0)
    lines, offset2 = frozen_smoke.read_new_lines(log_path, offset)
    assert lines == []
    assert offset2 == offset


def test_find_marker():
    lines = ["a", "b - CRITICAL - boom", "c"]
    assert frozen_smoke.find_marker(lines, "CRITICAL") == "b - CRITICAL - boom"
    assert frozen_smoke.find_marker(lines, "nope") is None


def test_find_any_marker():
    lines = ["ok", "Traceback (most recent call last):"]
    assert frozen_smoke.find_any_marker(lines, ("CRITICAL", "Traceback")) == lines[1]
    assert frozen_smoke.find_any_marker(["ok"], ("CRITICAL", "Traceback")) is None


def test_count_occurrences():
    text = "x\n[INIT] Startup complete.\ny\n[INIT] Startup complete.\n"
    assert frozen_smoke.count_occurrences(text, "[INIT] Startup complete.") == 2
    assert frozen_smoke.count_occurrences(text, "nope") == 0


def test_find_self_respawn_matches_case_insensitively():
    names = ["Samsara.exe", "conhost.exe", "SAMSARA.EXE"]
    assert frozen_smoke.find_self_respawn(names) == ["Samsara.exe", "SAMSARA.EXE"]
    assert frozen_smoke.find_self_respawn(["explorer.exe"]) == []


def test_log_growth_exceeds_bound():
    assert frozen_smoke.log_growth_exceeds_bound(200, 200) is True
    assert frozen_smoke.log_growth_exceeds_bound(199, 200) is False


def test_check_ok_and_fail_line_formatting():
    ok_check = frozen_smoke.Check("boot").ok("details here")
    assert ok_check.passed is True
    assert ok_check.line() == "[PASS] boot -- details here"

    fail_check = frozen_smoke.Check("boot").fail("bad stuff")
    assert fail_check.passed is False
    assert fail_check.line() == "[FAIL] boot -- bad stuff"

    bare = frozen_smoke.Check("thing").ok()
    assert bare.line() == "[PASS] thing"


def test_check_bundled_vad_accepts_local_onnx_ready_marker():
    check = frozen_smoke.check_bundled_vad(
        "INFO - [BOOT-DIAG] Bundled Silero VAD ONNX load returned: 21ms\n"
    )
    assert check.passed is True
    assert "ONNX Runtime" in check.detail


def test_check_bundled_vad_rejects_rms_fallback():
    check = frozen_smoke.check_bundled_vad(
        "WARNING - [VAD] Silero VAD ONNX unavailable, falling back to RMS: missing\n"
    )
    assert check.passed is False
    assert "fell back to RMS" in check.detail


def test_check_bundled_vad_rejects_missing_marker():
    check = frozen_smoke.check_bundled_vad(
        "INFO - [INIT] Startup complete.\n"
    )
    assert check.passed is False
    assert "no VAD load marker" in check.detail


def test_wait_for_boot_detects_marker(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text("", encoding="utf-8")

    def _writer():
        time.sleep(0.2)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("2026-01-01 00:00:00,000 - INFO - [INIT] Startup complete.\n")

    threading.Thread(target=_writer, daemon=True).start()
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=3.0)
    assert outcome == "boot"
    assert "[INIT] Startup complete." in detail
    assert offset > 0


def test_wait_for_boot_detects_failure_marker(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text(
        "2026-01-01 00:00:00,000 - CRITICAL - Uncaught exception\n", encoding="utf-8"
    )
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=1.0)
    assert outcome == "fail"
    assert "CRITICAL" in detail


def test_wait_for_boot_detects_already_running(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text(
        "2026-01-01 00:00:00,000 - WARNING - [WARN] Samsara is already running (PID: 123)\n",
        encoding="utf-8",
    )
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=1.0)
    assert outcome == "already_running"


def test_wait_for_boot_wizard_expected_finds_start_marker(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text(
        "2026-01-01 00:00:00,000 - INFO - First run detected - launching setup wizard...\n"
        "2026-01-01 00:00:00,001 - DEBUG - [WIZ-DIAG] calling wizard.run()\n",
        encoding="utf-8",
    )
    # [WIZ-DIAG] must NOT be treated as a failure marker when a wizard is expected.
    outcome, detail, offset = frozen_smoke.wait_for_boot(
        log_path, timeout_s=1.0, wizard_expected=True
    )
    assert outcome == "wizard"


def test_wait_for_boot_wiz_diag_alone_is_not_a_failure(tmp_path):
    # qt_runtime.py's post() helper logs "[WIZ-DIAG] post(): ..." on every
    # call regardless of caller (the splash screen uses it too), so its mere
    # presence must NOT be treated as "the wizard fired" -- only
    # WIZARD_START_MARKER is unambiguous. Confirm the boot marker still wins.
    log_path = tmp_path / "samsara.log"
    log_path.write_text(
        "2026-01-01 00:00:00,000 - DEBUG - [WIZ-DIAG] post(): state=RUNNING\n"
        "2026-01-01 00:00:00,100 - INFO - [INIT] Startup complete.\n",
        encoding="utf-8",
    )
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=1.0)
    assert outcome == "boot"


def test_wait_for_boot_normal_boot_fails_on_unexpected_wizard_start(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text(
        "2026-01-01 00:00:00,000 - INFO - First run detected - launching setup wizard...\n",
        encoding="utf-8",
    )
    # On a normal boot (pre-seeded, already-complete config), the wizard
    # firing at all is a regression.
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=1.0)
    assert outcome == "fail"
    assert "launching setup wizard" in detail


def test_wait_for_boot_timeout(tmp_path):
    log_path = tmp_path / "samsara.log"
    log_path.write_text("2026-01-01 00:00:00,000 - INFO - still loading\n", encoding="utf-8")
    outcome, detail, offset = frozen_smoke.wait_for_boot(log_path, timeout_s=0.5)
    assert outcome == "timeout"
    assert detail is None


def test_check_no_self_respawn_missing_psutil(monkeypatch):
    monkeypatch.setattr(frozen_smoke, "psutil", None)
    check = frozen_smoke.check_no_self_respawn(999999)
    assert check.passed is False
    assert "psutil" in check.detail


def test_check_no_self_respawn_no_such_process(monkeypatch):
    import psutil as real_psutil

    class _FakePsutil:
        NoSuchProcess = real_psutil.NoSuchProcess
        Error = real_psutil.Error

        @staticmethod
        def Process(pid):
            raise real_psutil.NoSuchProcess(pid)

    monkeypatch.setattr(frozen_smoke, "psutil", _FakePsutil)
    check = frozen_smoke.check_no_self_respawn(999999)
    assert check.passed is False
    assert "not found" in check.detail


def test_check_no_self_respawn_detects_child_copy(monkeypatch):
    import psutil as real_psutil

    class _FakeChild:
        def __init__(self, name):
            self._name = name

        def name(self):
            return self._name

    class _FakeProc:
        def children(self, recursive=True):
            return [_FakeChild("Samsara.exe"), _FakeChild("conhost.exe")]

    class _FakePsutil:
        NoSuchProcess = real_psutil.NoSuchProcess
        Error = real_psutil.Error

        @staticmethod
        def Process(pid):
            return _FakeProc()

    monkeypatch.setattr(frozen_smoke, "psutil", _FakePsutil)
    check = frozen_smoke.check_no_self_respawn(999999)
    assert check.passed is False
    assert "self-spawned" in check.detail


def test_check_no_self_respawn_passes_with_no_copies(monkeypatch):
    import psutil as real_psutil

    class _FakeChild:
        def name(self):
            return "conhost.exe"

    class _FakeProc:
        def children(self, recursive=True):
            return [_FakeChild()]

    class _FakePsutil:
        NoSuchProcess = real_psutil.NoSuchProcess
        Error = real_psutil.Error

        @staticmethod
        def Process(pid):
            return _FakeProc()

    monkeypatch.setattr(frozen_smoke, "psutil", _FakePsutil)
    check = frozen_smoke.check_no_self_respawn(999999)
    assert check.passed is True
