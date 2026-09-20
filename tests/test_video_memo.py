"""Core-safe video memo lifecycle tests with synthetic camera/audio backends."""
from __future__ import annotations

import ast
import importlib
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from samsara.audio_engine.ring import EMPTY
from samsara.quick_memo import load_memos, video_path_for
from samsara.video_memo import (
    CAMERA_PACK_NOTICE, CameraUnavailable, VideoMemoCapture, VideoMemoError,
)


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _Reader:
    def __init__(self, frames=()):
        self.frames = list(frames)

    def read_next(self):
        return self.frames.pop(0) if self.frames else EMPTY


class _Engine:
    _running = True

    def __init__(self, reader):
        self.reader = reader
        self.registered = []
        self.unregistered = []

    def register_consumer(self, name):
        self.registered.append(name)
        return self.reader

    def unregister_consumer(self, reader):
        self.unregistered.append(reader)


class _Camera:
    def __init__(self, clock=None, advance=0.0, results=None):
        self.clock = clock
        self.advance = advance
        self.results = list(results or [])
        self.released = False
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.clock is not None:
            self.clock.value += self.advance
        if self.results:
            return self.results.pop(0)
        return True, np.zeros((16, 16, 3), dtype=np.uint8)

    def release(self):
        self.released = True


class _Writer:
    def __init__(self, path, *, fail_on_video=None):
        self.path = path
        self.path.write_bytes(b"mp4-header")
        self.videos = 0
        self.audios = []
        self.fail_on_video = fail_on_video
        self.closed = False

    def write_video(self, _image, _pts):
        self.videos += 1
        if self.videos == self.fail_on_video:
            raise OSError("synthetic encoder failure")

    def write_audio(self, pcm):
        self.audios.append(np.asarray(pcm).copy())

    def close(self):
        self.closed = True


class _ManualThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def _audio_frame(epoch=4):
    return SimpleNamespace(pcm=np.ones(160, dtype=np.int16), device_epoch=epoch)


def _capture(tmp_path, *, camera=None, reader=None, clock=None, fail_on_video=None,
             max_duration_s=300):
    reader = reader or _Reader([_audio_frame()])
    engine = _Engine(reader)
    camera = camera or _Camera(clock=clock)
    writers = []

    def writer_factory(path, _frame):
        writer = _Writer(path, fail_on_video=fail_on_video)
        writers.append(writer)
        return writer

    capture = VideoMemoCapture(
        engine, home=tmp_path, max_duration_s=max_duration_s,
        camera_factory=lambda: camera, writer_factory=writer_factory,
        clock=clock or _Clock(), thread_factory=_ManualThread,
    )
    return capture, engine, camera, writers


def test_start_stop_uses_one_ace_reader_and_indexes_the_mp4(tmp_path):
    capture, engine, camera, writers = _capture(tmp_path)

    assert capture.start() is True
    assert capture.active
    assert len(engine.registered) == 1
    capture._writer.write_video(capture._first_frame, 0)
    capture._write_audio()
    result = capture.stop()

    assert result.path is not None and result.path.exists()
    assert result.path.parent == tmp_path / "memos" / "video"
    assert result.path.name.endswith(".mp4") and "_partial" not in result.path.name
    assert result.record["source"] == "video"
    assert video_path_for(result.record, tmp_path) == result.path.resolve()
    assert load_memos(tmp_path)[-1]["video"]
    assert engine.unregistered == [engine.reader]
    assert camera.released and writers[0].closed


def test_hard_duration_cap_finishes_as_a_complete_memo(tmp_path):
    clock = _Clock()
    camera = _Camera(clock=clock, advance=0.6)
    capture, _engine, _camera, _writers = _capture(
        tmp_path, camera=camera, clock=clock, max_duration_s=1,
    )
    capture.start()

    capture._worker.target()

    assert capture.result.path is not None
    assert capture.result.partial is False
    assert camera.reads <= 4


def test_missing_webcam_fails_before_creating_a_video_file(tmp_path):
    camera = _Camera(results=[(False, None)])
    capture, _engine, _camera, writers = _capture(tmp_path, camera=camera)

    with pytest.raises(CameraUnavailable, match="No camera"):
        capture.start()

    assert writers == []
    assert camera.released
    assert not (tmp_path / "memos" / "video").exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CameraUnavailable("No camera found"), "Say take a memo to record audio"),
        (VideoMemoError(CAMERA_PACK_NOTICE), CAMERA_PACK_NOTICE),
    ],
)
def test_start_failures_speak_and_show_a_notice_without_a_beep(error, expected):
    from plugins.commands.video_memo import VideoMemoController

    notices = []
    spoken = []
    shown = threading.Event()
    app = SimpleNamespace(
        audio_coordinator=SimpleNamespace(
            speak=lambda text, **_kwargs: spoken.append(text),
        ),
        _schedule_ui=lambda func, *args: func(*args),
        _show_outcome_chip=lambda text, kind: (notices.append((text, kind)), shown.set()),
        play_sound=lambda *_args, **_kwargs: pytest.fail("failure must not beep"),
    )
    VideoMemoController(app)._report_start_failure(error)

    assert shown.wait(1.0)
    assert expected in spoken[0]
    assert expected in notices[0][0]
    assert notices[0][1] == "warning"


def test_encoder_error_keeps_and_indexes_a_named_partial_file(tmp_path):
    capture, _engine, camera, _writers = _capture(
        tmp_path, camera=_Camera(results=[
            (True, np.zeros((16, 16, 3), dtype=np.uint8)),
        ]), fail_on_video=2,
    )
    capture.start()

    capture._worker.target()

    result = capture.result
    assert result.partial is True
    assert result.path is not None and result.path.exists()
    assert result.path.stem.endswith("_partial")
    assert result.record["partial"] is True
    assert video_path_for(load_memos(tmp_path)[-1], tmp_path) == result.path.resolve()
    assert camera.released


def test_video_module_import_does_not_load_optional_camera_dependencies():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "samsara" / "video_memo.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "av" not in imported
    assert "cv2" not in imported


def test_video_reference_rebuilds_from_markdown_and_delete_removes_media(tmp_path):
    from samsara import quick_memo

    path = tmp_path / "memos" / "video" / "capture.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic mp4")
    record = quick_memo.add_memo("Video memo", source="video", home=tmp_path,
                                 video_path=path, partial=True)
    quick_memo.index_file(tmp_path).unlink()

    rebuilt = quick_memo.load_memos(tmp_path)[0]
    assert rebuilt["video"] == "video/capture.mp4"
    assert rebuilt["partial"] is True
    assert quick_memo.delete_memo(rebuilt["id"], tmp_path)
    assert not path.exists()


def test_catalog_resolves_the_video_memo_command():
    from pathlib import Path
    from samsara import plugin_commands

    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "commands_catalog.json").read_text(encoding="utf-8"))
    row = next(spec for spec in document["commands"]
               if spec["canonical_id"] == "video_memo.record_video_memo")
    assert row["risk"] == "write"
    assert row["pack"] == "utilities"
    assert "video memo" in row["aliases"]

    plugin = importlib.import_module("plugins.commands.video_memo")
    entries = plugin_commands._MODULE_ENTRIES[plugin.__name__]
    entry = entries["record video memo"]
    assert entry["func"] is plugin.start_video_memo
    assert "video memo" in entry["aliases"]


def test_camera_pack_smoke_checks_pyav_but_core_check_rejects_it():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = (root / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    assert "'mediapipe', _CV2_DISTRIBUTION, 'av'" in spec
    assert 'find_spec("av") is not None' in spec
    assert 'import av' in spec
    assert 'container.add_stream("libx264", rate=30)' in spec
    assert 'container.add_stream("aac", rate=16000)' in spec


def test_memos_page_lists_and_opens_a_video_record(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from samsara import quick_memo
    from samsara.ui.memos_qt import MemosPage, OPEN_VIDEO

    path = tmp_path / "memos" / "video" / "capture.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic mp4")
    quick_memo.add_memo("Video memo", source="video", home=tmp_path, video_path=path)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    page = MemosPage(SimpleNamespace(config={}), home=tmp_path)
    try:
        page.show()
        qapp.processEvents()
        assert "Video" in page._list.item(0).text()
        assert page._play_btn.text() == OPEN_VIDEO
        assert page.play_selected() is True
        assert opened == [QUrl.fromLocalFile(str(path))]
    finally:
        page.close()
