from pathlib import Path
import threading

from samsara.model_load_feedback import is_model_loading_status, load_model_with_feedback
from tools.cpu_first_start_893 import render_markdown, run_measurements


FIXTURE = Path(__file__).parent / "fixtures" / "893" / "five_seconds.wav.b64"


def test_cpu_benchmark_runs_fixture_and_writes_timing_table_without_gpu(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")

    class FakeModel:
        def transcribe(self, samples, **_kwargs):
            assert len(samples) == 80000
            return iter(()), object()

    records = run_measurements(
        model_factory=lambda _model, _compute: FakeModel(),
        fixture=FIXTURE,
        audio_decoder=lambda _path: [0.0] * 80000,
        models=("base.en",),
        compute_types=("int8",),
        runs=1,
    )
    table = render_markdown(records, default_model="base", default_compute="int8")
    assert "Default without CUDA: **base / int8**" in table
    assert "| base.en | int8 |" in table
    assert "First 5s transcription" in table
    assert "Warm 5s transcription" in table
    assert "| 1/1 | ok |" in table
    assert records[0]["audio"] == 5.0


def test_splash_shows_loading_while_fake_model_loader_blocks_then_ready():
    entered = threading.Event()
    release = threading.Event()
    updates = []

    def blocked_loader():
        entered.set()
        assert release.wait(5)
        return object()

    result = []
    worker = threading.Thread(target=lambda: result.append(load_model_with_feedback(
        lambda status, _progress, _detail: updates.append(status),
        blocked_loader,
        ready_detail=lambda _elapsed: "Loaded on CPU",
    )))
    worker.start()
    assert entered.wait(5)
    assert updates[-1] == "Loading the speech model - first start can take a minute"
    assert is_model_loading_status(updates[-1])
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert updates[-1] == "Speech model ready..."
    assert not is_model_loading_status(updates[-1])
    assert result

