import ast
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock


def _load_model_loader(monkeypatch, *, resolve_cached, construct):
    source = Path(__file__).resolve().parents[1].joinpath("dictation.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_whisper_model"
    )
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "dictation.py", "exec"), namespace)

    whisper_module = types.ModuleType("faster_whisper")
    whisper_module.WhisperModel = construct
    utils_module = types.ModuleType("faster_whisper.utils")
    utils_module.download_model = resolve_cached
    monkeypatch.setitem(sys.modules, "faster_whisper", whisper_module)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils_module)
    return namespace["_create_whisper_model"]


def test_cached_model_load_is_local_and_never_calls_hub(monkeypatch, tmp_path):
    network_call = Mock()
    resolve = Mock(return_value=str(tmp_path / "cached-model"))

    def construct(model, **kwargs):
        if not kwargs.get("local_files_only", False):
            network_call()
        return (model, kwargs)

    load = _load_model_loader(monkeypatch, resolve_cached=resolve, construct=construct)
    result = load("base", device="cpu")

    assert resolve.call_args.kwargs["local_files_only"] is True
    assert result[0] == str(tmp_path / "cached-model")
    assert result[1]["local_files_only"] is True
    network_call.assert_not_called()


def test_missing_model_announces_before_download(monkeypatch):
    class CacheMiss(Exception):
        pass

    events = []

    def resolve(_model, **kwargs):
        assert kwargs["local_files_only"] is True
        raise CacheMiss("not cached")

    def construct(model, **kwargs):
        if not kwargs.get("local_files_only", False):
            events.append("hub download")
        return model

    load = _load_model_loader(monkeypatch, resolve_cached=resolve, construct=construct)
    load("base", _on_download=lambda: events.append("announced"))

    assert events == ["announced", "hub download"]


def test_corrupt_cached_model_announces_before_repair_download(monkeypatch, tmp_path):
    events = []

    def construct(model, **kwargs):
        if kwargs.get("local_files_only", False):
            raise ValueError("corrupt cached model")
        events.append("hub download")
        return model

    load = _load_model_loader(
        monkeypatch,
        resolve_cached=lambda *_args, **_kwargs: str(tmp_path / "cached-model"),
        construct=construct,
    )
    load("base", _on_download=lambda: events.append("announced"))

    assert events == ["announced", "hub download"]


def test_boot_disables_hub_telemetry_before_model_loader_import():
    boot_source = Path(__file__).resolve().parents[1].joinpath("samsara", "boot.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(boot_source)
    telemetry_assignment = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Subscript) for target in node.targets)
        and ast.unparse(node.targets[0]) == "os.environ['HF_HUB_DISABLE_TELEMETRY']"
    )
    assert ast.literal_eval(telemetry_assignment.value) == "1"
    dictation_source = Path(__file__).resolve().parents[1].joinpath("dictation.py").read_text(
        encoding="utf-8"
    )
    assert "import samsara.boot as _samsara_boot" in dictation_source
    assert dictation_source.index("import samsara.boot as _samsara_boot") < dictation_source.index(
        "from faster_whisper import WhisperModel"
    )
