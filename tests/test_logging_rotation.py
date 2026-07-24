"""Regression tests for the 2026-07-20 silent-logging-death incident.

samsara.log froze at exactly 5,242,880 bytes (maxBytes) for days with zero
output; samsara.log.3 existed (also exactly 5MB) but .1/.2 did not --
evidence that a rollover got partway through its rename chain and then
died, silently, forever (see dictation.py's _SafeRotatingFileHandler
docstring for the full failure-mode analysis).

This file exercises _SafeRotatingFileHandler directly against a tmp_path
log file (a stand-in for a temp SAMSARA_HOME) with a tiny maxBytes, plus
_verify_logging_self_check(), the boot-time marker-write check.
"""
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


def _make_handler(tmp_path, maxBytes=300, backupCount=3):
    log_file = tmp_path / "samsara.log"
    handler = dictation._SafeRotatingFileHandler(
        log_file, maxBytes=maxBytes, backupCount=backupCount, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler, log_file


def _make_logger(handler, name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    logger.propagate = False
    return logger


class TestNormalRotationContinues:
    def test_multiple_rollovers_occur_and_logging_continues(self, tmp_path):
        handler, log_file = _make_handler(tmp_path, maxBytes=300, backupCount=3)
        logger = _make_logger(handler, "test-rotation-normal")
        try:
            for i in range(400):
                logger.info(f"line {i} " + "x" * 20)
        finally:
            handler.close()

        assert log_file.exists()
        assert (tmp_path / "samsara.log.1").exists()
        assert (tmp_path / "samsara.log.2").exists()
        assert (tmp_path / "samsara.log.3").exists(), (
            "expected at least 3 successful rollovers with this many small "
            "writes at maxBytes=300"
        )

    def test_backup_files_stay_bounded_no_runaway_growth(self, tmp_path):
        handler, log_file = _make_handler(tmp_path, maxBytes=300, backupCount=3)
        logger = _make_logger(handler, "test-rotation-bounded")
        try:
            for i in range(400):
                logger.info(f"line {i} " + "x" * 20)
        finally:
            handler.close()

        for suffix in ("", ".1", ".2", ".3"):
            f = tmp_path / f"samsara.log{suffix}"
            if f.exists():
                assert f.stat().st_size < 300 * 3


class TestRolloverFailureFallsBackToTruncateAndContinue:
    """Reproduces the actual incident: a rename inside doRollover's chain
    raises (e.g. PermissionError from a second process holding a backup
    file open without FILE_SHARE_DELETE) -- verified against the real
    failure mode observed live in this repo (a concurrent second process
    initializing its own logging against the same samsara.log path hit
    this exact PermissionError on os.rename during a G1 replay run)."""

    def test_rollover_failure_does_not_wedge_the_handler(self, tmp_path, monkeypatch):
        # maxBytes smaller than a single record: the very first write
        # already crosses it, so the rollover it triggers is deterministic
        # (no dependence on formatter/terminator byte-counting).
        handler, log_file = _make_handler(tmp_path, maxBytes=50, backupCount=3)
        logger = _make_logger(handler, "test-rotation-failure")
        try:
            real_rename = logging.handlers.os.rename
            calls = {"n": 0}

            def _flaky_rename(src, dst):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(
                        "simulated: file in use by another process (WinError 32)"
                    )
                return real_rename(src, dst)

            monkeypatch.setattr(logging.handlers.os, "rename", _flaky_rename)

            # First write already exceeds maxBytes=50 -> triggers
            # doRollover() -> hits the simulated PermissionError on its
            # rename (first-ever rollover: base -> base+".1").
            logger.info("trigger rollover " + "x" * 100)

            assert calls["n"] >= 1, "the injected failure must actually have been hit"
            assert handler.stream is not None, (
                "handler must not be left with stream=None after a rollover failure"
            )
            assert not handler.stream.closed

            monkeypatch.undo()

            before_size = log_file.stat().st_size
            before_mtime = log_file.stat().st_mtime
            time.sleep(0.01)
            logger.info("still alive after the failed rollover")
            handler.flush()
            after_size = log_file.stat().st_size
            after_mtime = log_file.stat().st_mtime

            assert (after_size, after_mtime) != (before_size, before_mtime), (
                "logging must keep working after a rollover failure, not "
                "freeze silently forever (the actual 2026-07-20 bug)"
            )
        finally:
            handler.close()

    def test_rollover_failure_is_never_raised_to_the_caller(self, tmp_path, monkeypatch):
        """A log call must never crash the app just because rotation failed."""
        handler, log_file = _make_handler(tmp_path, maxBytes=50, backupCount=3)
        logger = _make_logger(handler, "test-rotation-no-raise")
        try:
            def _always_fail(src, dst):
                raise PermissionError("simulated: always fails")

            monkeypatch.setattr(logging.handlers.os, "rename", _always_fail)

            # Must not raise, even across repeated rollover attempts.
            logger.info("trigger rollover " + "x" * 100)
            logger.info("and again " + "x" * 100)
        finally:
            handler.close()


class TestLoggingSelfCheck:
    def test_passes_when_marker_write_moves_the_file(self, tmp_path, monkeypatch):
        handler, log_file = _make_handler(tmp_path, maxBytes=10_000_000, backupCount=3)
        throwaway_logger = _make_logger(handler, f"test-self-check-ok-{id(handler)}")
        monkeypatch.setattr(dictation, "logger", throwaway_logger)
        monkeypatch.setattr(dictation, "LOG_FILE", log_file)
        monkeypatch.setattr(dictation, "file_handler", handler)
        try:
            assert dictation._verify_logging_self_check() is True
        finally:
            handler.close()

    def test_fails_when_the_file_never_moves(self, tmp_path, monkeypatch):
        log_file = tmp_path / "samsara.log"
        log_file.write_bytes(b"frozen content\n")
        noop_logger = _make_logger(logging.NullHandler(), f"test-self-check-noop-{id(log_file)}")
        monkeypatch.setattr(dictation, "logger", noop_logger)
        monkeypatch.setattr(dictation, "LOG_FILE", log_file)
        monkeypatch.setattr(dictation, "file_handler", logging.NullHandler())

        assert dictation._verify_logging_self_check() is False

    def test_never_raises_even_on_internal_error(self, monkeypatch):
        monkeypatch.setattr(dictation, "LOG_FILE", Mock(side_effect=RuntimeError("boom")))
        # LOG_FILE.exists() etc. will raise AttributeError/TypeError on a
        # plain Mock() misuse above -- the point is _verify_logging_self_check
        # must swallow it and return False, never propagate.
        assert dictation._verify_logging_self_check() is False
