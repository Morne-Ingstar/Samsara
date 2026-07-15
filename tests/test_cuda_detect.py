"""Focused tests for CUDA runtime-pack detection."""

from __future__ import annotations

from samsara import cuda_detect


def _use_search_paths(monkeypatch, *paths) -> None:
    monkeypatch.setattr(cuda_detect, "_probable_search_paths", lambda: list(paths))
    cuda_detect._reset_cache_for_test()


def test_complete_cuda_pack_is_available(tmp_path, monkeypatch):
    for dll in cuda_detect._REQUIRED_CUDA_DLLS:
        (tmp_path / dll).touch()
    _use_search_paths(monkeypatch, tmp_path)

    assert cuda_detect.is_cuda_available() is True
    assert cuda_detect.resolve_device("cuda") == "cuda"
    assert "CUDA detected" in cuda_detect.cuda_status_message()


def test_partial_cuda_pack_reports_every_missing_dll(tmp_path, monkeypatch):
    installed = {"cublas64_12.dll", "cublasLt64_12.dll"}
    for dll in installed:
        (tmp_path / dll).touch()
    _use_search_paths(monkeypatch, tmp_path)

    assert cuda_detect.is_cuda_available() is False
    assert cuda_detect.resolve_device("cuda") == "cpu"
    message = cuda_detect.cuda_status_message()
    expected_missing = set(cuda_detect._REQUIRED_CUDA_DLLS) - installed
    for dll in expected_missing:
        assert dll in message
    for dll in installed:
        assert dll not in message


def test_required_dlls_may_be_found_across_search_paths(tmp_path, monkeypatch):
    first = tmp_path / "ctranslate2"
    second = tmp_path / "system_cuda"
    first.mkdir()
    second.mkdir()
    midpoint = len(cuda_detect._REQUIRED_CUDA_DLLS) // 2
    for dll in cuda_detect._REQUIRED_CUDA_DLLS[:midpoint]:
        (first / dll).touch()
    for dll in cuda_detect._REQUIRED_CUDA_DLLS[midpoint:]:
        (second / dll).touch()
    _use_search_paths(monkeypatch, first, second)

    assert cuda_detect.is_cuda_available() is True
