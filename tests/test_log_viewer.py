"""Tests for samsara.log_tailer.LogTailer: the pure tail/rotation logic
behind the "View Live Log" viewer window.

Pure-logic only -- no Qt. Exercises the real LogTailer against tmp files;
no monkeypatching of file I/O.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.log_tailer import LogTailer


# ============================================================================
# Initial tail -- last ~200KB, discard the first partial line
# ============================================================================

class TestInitialTail:
    def test_short_file_returns_all_lines(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("line one\nline two\nline three\n", encoding="utf-8")

        tailer = LogTailer(log_path)
        lines = tailer.initial_tail()

        assert lines == ["line one", "line two", "line three"]

    def test_missing_file_returns_empty_list(self, tmp_path):
        tailer = LogTailer(tmp_path / "does_not_exist.log")

        assert tailer.initial_tail() == []

    def test_large_file_tails_last_200kb_and_discards_partial_first_line(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        # Written in binary mode with explicit \n (no platform newline
        # translation) and a FIXED-width zero-pad (:05d, not :04d -- which
        # would silently stop zero-padding but not truncate past 9999,
        # making lines >= 10000 one byte longer) so every line is exactly
        # the same number of bytes and the tail math below is exact.
        line_bytes = b""
        for i in range(30000):
            line_bytes += f"line{i:05d}\n".encode("utf-8")
        log_path.write_bytes(line_bytes)
        bytes_per_line = len(line_bytes) // 30000  # 10

        tailer = LogTailer(log_path)
        lines = tailer.initial_tail()

        # Must NOT include the very start of the file -- proves it tailed
        # rather than reading from byte 0.
        assert "line00000" not in lines
        # Must end with the true last line of the file, uncorrupted.
        assert lines[-1] == "line29999"
        # Roughly the tail window's worth of lines, give or take the one
        # discarded partial line.
        expected = LogTailer.INITIAL_TAIL_BYTES // bytes_per_line
        assert abs(len(lines) - expected) <= 2

    def test_offset_set_to_eof_after_initial_tail(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        content = b"line one\nline two\n"
        log_path.write_bytes(content)

        tailer = LogTailer(log_path)
        tailer.initial_tail()

        assert tailer._offset == len(content)


# ============================================================================
# Incremental reads via poll()
# ============================================================================

class TestIncrementalPoll:
    def test_poll_returns_only_newly_appended_lines(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("line one\nline two\n", encoding="utf-8")

        tailer = LogTailer(log_path)
        tailer.initial_tail()

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("line three\nline four\n")

        assert tailer.poll() == ["line three", "line four"]

    def test_poll_with_nothing_new_returns_empty_list(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("line one\n", encoding="utf-8")

        tailer = LogTailer(log_path)
        tailer.initial_tail()

        assert tailer.poll() == []

    def test_multiple_polls_each_return_only_their_own_new_lines(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("start\n", encoding="utf-8")
        tailer = LogTailer(log_path)
        tailer.initial_tail()

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("a\n")
        assert tailer.poll() == ["a"]

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("b\nc\n")
        assert tailer.poll() == ["b", "c"]

    def test_poll_on_missing_file_returns_empty_list(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("line one\n", encoding="utf-8")
        tailer = LogTailer(log_path)
        tailer.initial_tail()
        log_path.unlink()

        assert tailer.poll() == []


# ============================================================================
# Rotation detection (RotatingFileHandler: truncate + rewrite smaller)
# ============================================================================

class TestRotationDetection:
    def test_rotation_resets_offset_and_emits_separator(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        # Original file, large enough that the post-rotation file is
        # unambiguously smaller than our already-read offset.
        log_path.write_text("old line one\nold line two\nold line three\n", encoding="utf-8")

        tailer = LogTailer(log_path)
        tailer.initial_tail()

        # Simulate RotatingFileHandler's rollover: old content rolled away,
        # a fresh (much smaller) file written in its place.
        log_path.write_text("new line one\n", encoding="utf-8")

        result = tailer.poll()

        assert result[0] == LogTailer.ROTATION_SEPARATOR
        assert result[1:] == ["new line one"]

    def test_offset_reset_to_zero_on_rotation(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_bytes(b"old line one\nold line two\nold line three\n")
        tailer = LogTailer(log_path)
        tailer.initial_tail()

        log_path.write_bytes(b"x\n")
        tailer.poll()

        assert tailer._offset == len(b"x\n")

    def test_post_rotation_polls_continue_normally(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("old line one\nold line two\nold line three\n", encoding="utf-8")
        tailer = LogTailer(log_path)
        tailer.initial_tail()

        log_path.write_text("new line one\n", encoding="utf-8")
        tailer.poll()  # consumes the rotation

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("new line two\n")

        assert tailer.poll() == ["new line two"]

    def test_no_false_positive_rotation_on_normal_growth(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("line one\n", encoding="utf-8")
        tailer = LogTailer(log_path)
        tailer.initial_tail()

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("line two\n")

        result = tailer.poll()

        assert LogTailer.ROTATION_SEPARATOR not in result
        assert result == ["line two"]


# ============================================================================
# UTF-8 with errors="replace" -- must never raise on invalid bytes
# ============================================================================

class TestInvalidBytesNeverRaise:
    def test_invalid_byte_in_initial_tail_does_not_raise(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        with open(log_path, "wb") as f:
            f.write(b"good line\n\x9d\x9d\x9d invalid byte line\nmore good text\n")

        tailer = LogTailer(log_path)
        lines = tailer.initial_tail()  # must not raise

        assert len(lines) == 3
        assert "good line" in lines[0]
        assert "�" in lines[1]
        assert "more good text" in lines[2]

    def test_invalid_byte_in_poll_does_not_raise(self, tmp_path):
        log_path = tmp_path / "samsara.log"
        log_path.write_text("clean start\n", encoding="utf-8")
        tailer = LogTailer(log_path)
        tailer.initial_tail()

        with open(log_path, "ab") as f:
            f.write(b"\x9d bad bytes appended\n")

        lines = tailer.poll()  # must not raise

        assert len(lines) == 1
        assert "�" in lines[0]

    def test_invalid_bytes_at_arbitrary_seek_point_in_large_file(self, tmp_path):
        """The initial-tail seek lands at an arbitrary byte offset (not
        necessarily a line/character boundary) -- errors="replace" must
        absorb a seek landing mid multi-byte sequence too, not just a
        genuinely corrupt byte."""
        log_path = tmp_path / "samsara.log"
        with open(log_path, "wb") as f:
            for i in range(30000):
                f.write(f"line{i:04d} café é\n".encode("utf-8"))

        tailer = LogTailer(log_path)
        lines = tailer.initial_tail()  # must not raise regardless of seek alignment

        assert len(lines) > 0
