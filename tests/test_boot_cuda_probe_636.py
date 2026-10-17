"""Boot device selection without app bootstrap, native models, or audio."""
import ast
import builtins
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import cuda_detect


@pytest.fixture(autouse=True)
def clean_probe_cache():
    cuda_detect._reset_cache_for_test()
    yield
    cuda_detect._reset_cache_for_test()


@pytest.fixture
def choose_device():
    # Execute only the actual device-decision statements, never dictation's
    # bootstrap, model factory, or the rest of the startup worker.
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "DictationApp")
    method = next(n for n in app.body if isinstance(n, ast.FunctionDef)
                  and n.name == "load_model_async")
    worker = next(n for n in method.body if isinstance(n, ast.FunctionDef))
    body = worker.body[0].body
    assignments = {n.targets[0].id: i for i, n in enumerate(body)
                   if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    fragment = ast.Module(body=body[assignments["device"]:assignments["compute_type"] + 1],
                          type_ignores=[])
    code = compile(fragment, "<boot-device-decision-636>", "exec")

    def choose(configured):
        namespace = {"self": SimpleNamespace(config={"device": configured}),
                     "logger": logging.getLogger(__name__)}
        exec(code, namespace)
        return namespace["device"], namespace["compute_type"]

    return choose


def guard_native_import(monkeypatch, events, backend=None):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "ctranslate2":
            events.append("native import")
            assert backend is not None, "negative/CPU selection imported native backend"
            return backend
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_auto_missing_pack_skips_native_probe(choose_device, monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(cuda_detect, "_probable_search_paths", lambda: [tmp_path])
    guard_native_import(monkeypatch, events)
    assert choose_device("auto") == ("cpu", "int8")
    assert events == []
    assert cuda_detect.is_cuda_available._cache is False
    assert cuda_detect.is_cuda_available._missing_dlls == cuda_detect._REQUIRED_CUDA_DLLS


def test_explicit_cpu_skips_even_dll_discovery(choose_device, monkeypatch):
    events = []
    monkeypatch.setattr(cuda_detect, "_missing_cuda_dlls",
                        lambda: pytest.fail("explicit CPU must not probe DLLs"))
    guard_native_import(monkeypatch, events)
    assert choose_device("cpu") == ("cpu", "int8")
    assert events == []


@pytest.mark.parametrize("count,types,expected,calls", [
    (1, {"float16"}, ("cuda", "float16"), ["count", "types"]),
    (0, {"float16"}, ("cpu", "int8"), ["count"]),
    (1, set(), ("cpu", "int8"), ["count", "types"]),
])
def test_complete_pack_preserves_backend_checks(
        choose_device, monkeypatch, tmp_path, count, types, expected, calls):
    for dll in cuda_detect._REQUIRED_CUDA_DLLS:
        (tmp_path / dll).touch()
    events = []

    def paths():
        events.append("DLL check")
        return [tmp_path]

    def device_count():
        events.append("count")
        return count

    def compute_types(device):
        assert device == "cuda"
        events.append("types")
        return types

    monkeypatch.setattr(cuda_detect, "_probable_search_paths", paths)
    guard_native_import(monkeypatch, events, SimpleNamespace(
        get_cuda_device_count=device_count, get_supported_compute_types=compute_types))
    assert choose_device("auto") == expected
    assert events == ["DLL check", "native import", *calls]


def test_backend_failure_still_falls_back_to_cpu(choose_device, monkeypatch):
    events = []
    monkeypatch.setattr(cuda_detect, "_missing_cuda_dlls", lambda: ())

    def fail():
        raise RuntimeError("synthetic unavailable GPU")

    guard_native_import(monkeypatch, events, SimpleNamespace(get_cuda_device_count=fail))
    assert choose_device("auto") == ("cpu", "int8")
    assert events == ["native import"]


@pytest.mark.parametrize("missing,expected", [((), True), (("cudart64_12.dll",), False)])
def test_probe_caches_both_answers_until_reset(monkeypatch, missing, expected):
    calls, imports = [], []

    def probe():
        calls.append(True)
        return missing

    monkeypatch.setattr(cuda_detect, "_missing_cuda_dlls", probe)
    guard_native_import(monkeypatch, imports)
    assert cuda_detect.is_cuda_available() is expected
    assert cuda_detect.is_cuda_available() is expected
    assert cuda_detect.is_cuda_available._missing_dlls == missing
    assert len(calls) == 1
    cuda_detect._reset_cache_for_test()
    assert cuda_detect.is_cuda_available() is expected
    assert len(calls) == 2
    assert imports == []


@pytest.mark.parametrize("available,expected", [(True, ("cuda", "float16")),
                                                (False, ("cpu", "int8"))])
def test_explicit_cuda_preserves_pack_fallback(choose_device, monkeypatch, available, expected):
    events = []
    monkeypatch.setattr(cuda_detect, "_missing_cuda_dlls",
                        lambda: () if available else ("cudart64_12.dll",))
    guard_native_import(monkeypatch, events)
    assert choose_device("cuda") == expected
    assert events == []
