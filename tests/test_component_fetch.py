"""samsara.components.download_component and component_state, against an
in-memory transport that honours Range requests. No real network."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import components

URL = "https://example.invalid/releases/download/v9.9.9/Samsara-Windows-v9.9.9.zip"


def _component(data: bytes, **over) -> dict:
    c = {
        "id": "core", "name": "Samsara", "description": "The app.", "kind": "core",
        "available": True, "filename": "Samsara-Windows-v9.9.9.zip", "url": URL,
        "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "min_disk_bytes": 2 * len(data), "install_dir": ".", "requires": [], "default": True,
        "source": "built",
    }
    c.update(over)
    return c


class _Response:
    def __init__(self, status, body: bytes, total: int, start: int, cut_at=None):
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        if status == 206:
            self.headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"
        self._body = body if cut_at is None else body[:cut_at]
        self._pos = 0
        self.closed = False

    def read(self, n: int) -> bytes:
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class Server:
    """An asset server: serves `data`, optionally ignoring Range, optionally
    cutting the connection after `cut_after` bytes on the first request."""

    def __init__(self, data: bytes, *, honour_range=True, cut_after=None):
        self.data = data
        self.honour_range = honour_range
        self.cut_after = cut_after
        self.requests = []

    def open(self, url, headers):
        self.requests.append((url, dict(headers)))
        rng = headers.get("Range")
        cut = self.cut_after if len(self.requests) == 1 else None
        if rng and self.honour_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(self.data):
                return _Response(416, b"", len(self.data), start)
            return _Response(206, self.data[start:], len(self.data), start, cut_at=cut)
        return _Response(200, self.data, len(self.data), 0, cut_at=cut)


DATA = bytes(range(256)) * 40   # 10240 bytes, > 4 chunks of 2048


class TestDownload:
    def test_verifies_hash_reports_progress_and_writes_sidecar(self, tmp_path):
        server = Server(DATA)
        comp = _component(DATA)
        seen = []
        final = components.download_component(comp, tmp_path, progress=lambda d, t: seen.append((d, t)),
                                              opener=server.open, chunk_size=2048)
        assert final == tmp_path / comp["filename"] and final.read_bytes() == DATA
        assert not (tmp_path / (comp["filename"] + ".part")).exists()
        assert (tmp_path / (comp["filename"] + ".sha256")).read_text().split()[0] == comp["sha256"]
        assert seen[0] == (0, len(DATA)) and seen[-1] == (len(DATA), len(DATA))
        assert [d for d, _ in seen] == sorted(d for d, _ in seen)
        assert len(seen) == 1 + len(DATA) // 2048
        assert components.component_state(comp, tmp_path) == "present"

    def test_mismatch_deletes_the_partial_file_and_raises(self, tmp_path):
        server = Server(DATA)
        comp = _component(DATA, sha256="0" * 64)
        with pytest.raises(components.HashMismatch, match="partial file deleted"):
            components.download_component(comp, tmp_path, opener=server.open)
        assert list(tmp_path.iterdir()) == []
        assert components.component_state(comp, tmp_path) == "missing"

    def test_truncated_transfer_keeps_the_part_and_resumes_with_range(self, tmp_path):
        server = Server(DATA, cut_after=3000)
        comp = _component(DATA)
        with pytest.raises(components.DownloadError, match="connection ended at 3000"):
            components.download_component(comp, tmp_path, opener=server.open, chunk_size=1000)
        part = tmp_path / (comp["filename"] + ".part")
        assert part.stat().st_size == 3000 and not (tmp_path / comp["filename"]).exists()

        seen = []
        final = components.download_component(comp, tmp_path, opener=server.open, chunk_size=1000,
                                              progress=lambda d, t: seen.append(d))
        assert final.read_bytes() == DATA
        assert server.requests[1][1] == {"Range": "bytes=3000-"}
        assert seen[0] == 3000, "progress starts from the resumed offset"
        assert len(server.requests) == 2

    def test_server_ignoring_range_restarts_from_zero(self, tmp_path):
        comp = _component(DATA)
        part = tmp_path / (comp["filename"] + ".part")
        part.write_bytes(b"x" * 3000)          # a stale partial from some other run
        server = Server(DATA, honour_range=False)
        final = components.download_component(comp, tmp_path, opener=server.open)
        assert final.read_bytes() == DATA and not part.exists()
        assert server.requests[0][1] == {"Range": "bytes=3000-"} and len(server.requests) == 1

    def test_overlong_part_is_discarded(self, tmp_path):
        comp = _component(DATA)
        part = tmp_path / (comp["filename"] + ".part")
        part.write_bytes(b"x" * (len(DATA) + 5))
        server = Server(DATA)
        final = components.download_component(comp, tmp_path, opener=server.open)
        assert final.read_bytes() == DATA
        assert server.requests[0][1] == {}, "no Range for a part that cannot be trusted"

    def test_size_disagreement_is_refused_before_writing(self, tmp_path):
        server = Server(DATA)
        comp = _component(DATA, size_bytes=len(DATA) + 1, min_disk_bytes=2 * len(DATA) + 2)
        with pytest.raises(components.DownloadError, match="server offers"):
            components.download_component(comp, tmp_path, opener=server.open)
        assert list(tmp_path.iterdir()) == []

    def test_unavailable_component_is_never_fetched(self, tmp_path):
        server = Server(DATA)
        comp = _component(DATA, available=False, url=None, sha256=None, size_bytes=None, min_disk_bytes=None)
        with pytest.raises(components.ComponentUnavailable):
            components.download_component(comp, tmp_path, opener=server.open)
        assert server.requests == []

    def test_non_https_is_refused(self, tmp_path):
        server = Server(DATA)
        comp = _component(DATA, url="http://example.invalid/x.zip")
        with pytest.raises(components.DownloadError, match="non-https"):
            components.download_component(comp, tmp_path, opener=server.open)
        assert server.requests == []

    def test_complete_part_is_verified_without_a_request(self, tmp_path):
        comp = _component(DATA)
        (tmp_path / (comp["filename"] + ".part")).write_bytes(DATA)
        server = Server(DATA)
        final = components.download_component(comp, tmp_path, opener=server.open)
        assert final.read_bytes() == DATA and server.requests == []


class TestState:
    def test_present_missing_unavailable(self, tmp_path):
        comp = _component(DATA)
        assert components.component_state(comp, tmp_path) == "missing"
        final = tmp_path / comp["filename"]
        final.write_bytes(DATA)
        assert components.component_state(comp, tmp_path) == "missing", "no verified-hash sidecar yet"
        (tmp_path / (comp["filename"] + ".sha256")).write_text(f"{comp['sha256']}  {comp['filename']}\n")
        assert components.component_state(comp, tmp_path) == "present"
        final.write_bytes(DATA[:-1])
        assert components.component_state(comp, tmp_path) == "missing", "size mismatch"
        placeholder = _component(DATA, id="command-model", available=False, url=None, sha256=None,
                                 size_bytes=None, min_disk_bytes=None, source="pinned")
        assert components.component_state(placeholder, tmp_path) == "unavailable"

    def test_list_components_pairs_each_with_its_state(self, tmp_path):
        core = _component(DATA)
        placeholder = _component(DATA, id="command-model", available=False, url=None, sha256=None,
                                 size_bytes=None, min_disk_bytes=None, source="pinned")
        manifest = {"components": [core, placeholder]}
        assert [(c["id"], s) for c, s in components.list_components(manifest, tmp_path)] == [
            ("core", "missing"), ("command-model", "unavailable")]

    def test_sha256_file_matches_hashlib(self, tmp_path):
        path = tmp_path / "blob"
        path.write_bytes(DATA)
        assert components.sha256_file(path) == hashlib.sha256(DATA).hexdigest()
        assert components._sha256_fallback(path) == hashlib.sha256(DATA).hexdigest()


def test_import_does_no_network_and_no_download(monkeypatch):
    import importlib
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    importlib.reload(components)
