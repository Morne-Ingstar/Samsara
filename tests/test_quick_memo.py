import re
import threading
import wave

import pytest

import samsara.quick_memo as quick_memo
from samsara.quick_memo import append_memo, memo_dir, memo_file


def test_append_creates_header_and_entry(tmp_path):
    path = append_memo("hello", source="voice", home=tmp_path)
    assert path == tmp_path / "memos" / "memos.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Memos\n\n## ")
    assert text.endswith("hello\n\n")


def test_two_appends_preserve_order_and_timestamp(tmp_path):
    path = append_memo("first", "voice", home=tmp_path)
    append_memo("second", "voice", home=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.index("first") < text.index("second")
    assert len(re.findall(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text, re.M)) == 2


def test_unicode_and_multiline_text_survive(tmp_path):
    path = append_memo("café 漢字\nline two", "typed", home=tmp_path)
    assert "café 漢字\nline two" in path.read_text(encoding="utf-8")


def test_empty_text_is_refused(tmp_path):
    with pytest.raises(ValueError):
        append_memo("  \n", "voice", home=tmp_path)


def test_audio_line_only_when_audio_is_retained(tmp_path):
    path = append_memo("plain", "voice", home=tmp_path)
    audio = tmp_path / "memos" / "audio" / "capture.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"wav")
    append_memo("with audio", "voice", audio_path=audio, home=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.count("[audio: audio/capture.wav]") == 1


def test_memo_file_honours_custom_path(tmp_path):
    custom = tmp_path / "custom.md"
    assert memo_file(custom) == custom
    append_memo("custom", "typed", home=custom)
    assert custom.exists()
    assert not (tmp_path / "memos" / "memos.md").exists()


def test_memo_dir_resolves_under_home(tmp_path):
    assert memo_dir(tmp_path) == tmp_path / "memos"


def test_concurrent_appends_do_not_interleave_or_lose_entries(tmp_path, capsys):
    for iteration in range(5):
        home = tmp_path / f"run-{iteration}"
        errors = []
        start = threading.Barrier(20)

        def write(index):
            try:
                start.wait(timeout=10)
                append_memo(f"memo-{index}", "voice", home=home)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [], f"iteration {iteration + 1}: {errors!r}"
        text = (home / "memos" / "memos.md").read_text(encoding="utf-8")
        entries = re.findall(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}\n(memo-\d+)\n\n", text, re.M)
        assert sorted(entries) == sorted(f"memo-{i}" for i in range(20))
        assert len(re.findall(r"^## ", text, re.M)) == 20
    with capsys.disabled():
        print("concurrency: 5 iterations passed")


def test_header_written_once_when_threads_create_file(tmp_path):
    errors = []
    start = threading.Barrier(20)

    def write(index):
        try:
            start.wait(timeout=10)
            append_memo(f"header-race-{index}", "voice", home=tmp_path)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    text = (tmp_path / "memos" / "memos.md").read_text(encoding="utf-8")
    assert text.count("# Memos\n\n") == 1
    assert len(re.findall(r"^## ", text, re.M)) == 20


def test_replace_with_retry_recovers_from_scanner_lock(monkeypatch, tmp_path):
    calls = []
    delays = []
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.wav"
    source.write_bytes(b"new audio")
    destination.write_bytes(b"old audio")
    real_replace = quick_memo.os.replace

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) < 3:
            raise PermissionError("scanner lock")
        return real_replace(src, dst)

    monkeypatch.setattr(quick_memo.os, "replace", replace)
    monkeypatch.setattr(quick_memo.time, "sleep", delays.append)
    assert quick_memo._replace_with_retry(source, destination) is None
    assert calls == [(source, destination)] * 3
    assert delays == [0.02, 0.04]
    assert destination.read_bytes() == b"new audio"
    assert not source.exists()


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_replace_with_retry_reraises_after_last_attempt(monkeypatch, tmp_path, error_type):
    calls = []
    delays = []
    error = error_type("scanner lock")

    def replace(src, dst):
        calls.append(1)
        raise error

    monkeypatch.setattr(quick_memo.os, "replace", replace)
    monkeypatch.setattr(quick_memo.time, "sleep", delays.append)
    with pytest.raises(error_type) as caught:
        quick_memo._replace_with_retry(tmp_path / "source", tmp_path / "destination")
    assert caught.value is error
    assert len(calls) == 6
    assert delays == pytest.approx([0.02, 0.04, 0.06, 0.08, 0.10])


@pytest.mark.parametrize("operation", ["mkdir", "open", "fsync"])
def test_append_logs_and_wraps_filesystem_errors(tmp_path, monkeypatch, caplog, operation):
    error = PermissionError("filesystem temporarily unavailable")

    def fail(*args, **kwargs):
        raise error

    target = quick_memo.os if operation == "fsync" else quick_memo.Path
    with monkeypatch.context() as patch:
        patch.setattr(target, operation, fail)
        with pytest.raises(quick_memo.MemoWriteError, match="could not append memo to") as caught:
            append_memo("save this", "voice", home=tmp_path)
    assert caught.value.__cause__ is error
    assert "[MEMO] Could not append memo to" in caplog.text
    assert str(memo_file(tmp_path)) in caplog.text


def test_retain_audio_retries_replace_and_preserves_wav(tmp_path, monkeypatch):
    calls = []
    real_replace = quick_memo.os.replace

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) < 3:
            raise PermissionError("scanner lock")
        return real_replace(src, dst)

    monkeypatch.setattr(quick_memo.os, "replace", replace)
    monkeypatch.setattr(quick_memo.time, "sleep", lambda delay: None)
    path = quick_memo.retain_audio([0.0, 1.0, -1.0], 16000, home=tmp_path)
    assert len(calls) == 3
    assert all(dst == path for _, dst in calls)
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(3) == b"\x00\x00\xff\x7f\x01\x80"
    assert list(path.parent.iterdir()) == [path]
