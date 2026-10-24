from types import SimpleNamespace

from samsara import cloud_llm, openrouter_catalog
from tools.gen_ai_models import build_snapshot


def _model(model_id, name, *, output=("text",), prompt="0", completion="0", tools=True):
    return {
        "id": model_id,
        "name": name,
        "created": 1,
        "architecture": {"output_modalities": list(output)},
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"] if tools else [],
        "context_length": 4096,
    }


def _snapshot_fixture():
    return {"data": [
        _model("deepseek/deepseek-v4.1-flash", "DeepSeek Flash", prompt="0.1", completion="0.2"),
        _model("openai/gpt-6-luna", "Luna", prompt="0.1", completion="0.2"),
        _model("anthropic/claude-haiku-5.5", "Haiku", prompt="0.1", completion="0.2"),
        _model("x/y:free", "Zulu free"),
        _model("a/b:free", "Alpha free"),
        _model("x/y:nitro", "Nitro"),
        _model("audio/audio-maker", "Audio Maker", output=("audio",)),
        _model("x/paid", "Paid", prompt="0.1", completion="0.2"),
    ]}


def test_generator_keeps_openrouter_free_text_models_and_orders_them():
    snapshot = build_snapshot(_snapshot_fixture())
    openrouter = snapshot["openrouter"]["models"]
    assert any(row["id"] == "x/y:free" for row in openrouter)
    assert not any(row["id"] in {"x/y:nitro", "audio/audio-maker"} for row in openrouter)
    assert openrouter[0]["id"] == snapshot["openrouter"]["default"]
    remaining = [row for row in openrouter[1:] if row["id"] != "openrouter/auto"]
    assert [row["display_name"].casefold() for row in remaining] == sorted(
        row["display_name"].casefold() for row in remaining)


def test_free_filter_keeps_every_zero_price_text_model():
    catalogue = openrouter_catalog.parse_models({"data": _snapshot_fixture()["data"]})
    free = [row["id"] for row in catalogue if openrouter_catalog.matches(row, free_only=True)]
    assert set(free) == {"x/y:free", "a/b:free"}


def test_empty_cloud_reply_becomes_a_user_facing_failure_not_empty_text(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}

        def raise_for_status(self):
            pass

    payloads = []

    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", post)
    app = SimpleNamespace(config={"cloud_llm": {
        "provider": "openrouter", "api_key": "synthetic", "model": "x/y:free",
    }})
    reply = cloud_llm.send("system", "question", app)
    assert isinstance(reply, cloud_llm.CloudError)
    assert reply.failure.kind == "empty_reply"
    assert "empty answer" in str(reply)
    assert payloads[0]["reasoning"] == {"effort": "low"}
    assert payloads[0]["max_tokens"] == cloud_llm.DEFAULT_MAX_TOKENS


def test_reasoning_only_length_reply_is_classified_as_cut_off(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {
                "content": None, "reasoning_content": "private reasoning text",
            }}]}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", lambda *_a, **_k: Response())
    app = SimpleNamespace(config={"cloud_llm": {
        "provider": "deepseek", "api_key": "synthetic", "model": "deepseek-flash",
    }})
    reply = cloud_llm.send("system", "question", app)
    assert isinstance(reply, cloud_llm.CloudError)
    assert reply.failure.kind == "answer_cut_off"
