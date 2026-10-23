"""Settings controls for spoken agent replies."""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QCheckBox

from samsara.ui.settings import agents_section as ui


@pytest.fixture
def controls(qapp, monkeypatch):
    server = SimpleNamespace(status="off", port=47832, enabled=False, typing_enabled=False,
                             sync=lambda: None, connection_details=lambda: None,
                             new_token=lambda: False, cancel_typing=lambda: None)
    monkeypatch.setattr(ui, "for_app", lambda _app: server)
    app = SimpleNamespace(config={"command_packs": {"agents": True}}, save_config=lambda: None)
    panel = ui.AgentsControls(SimpleNamespace(app=app), QCheckBox())
    yield panel, app
    panel.deleteLater()


def test_digest_switch_persists_auto_and_off(controls):
    panel, app = controls
    panel.digest_choice.setCurrentIndex(panel.digest_choice.findData("off"))
    assert app.config["agent_voice"]["digest"] == "off"
    panel.digest_choice.setCurrentIndex(panel.digest_choice.findData("auto"))
    assert app.config["agent_voice"]["digest"] == "auto"


@pytest.mark.parametrize(
    "model,ready,provider,expected",
    [
        ("llama3", True, "ollama", "Ready - local model on this PC"),
        (None, False, "ollama", "Local model not set up - replies are cut to 60 words."),
        ("llama3", False, "ollama", "Local model is set up, but not ready yet."),
    ],
)
def test_digest_status_readiness_states(controls, monkeypatch, model, ready, provider, expected):
    panel, app = controls
    from samsara import ava_readiness

    app.config["ollama"] = {"enabled": True, "model": model}
    monkeypatch.setattr(ava_readiness, "configured_provider", lambda _app: provider)
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=ready))
    panel._refresh_digest_status()
    assert expected in panel.digest_status.text()


def test_try_digest_speaks_once(controls, monkeypatch):
    panel, app = controls
    spoken = []
    monkeypatch.setattr("samsara.agent_channel.spoken_digest.first_reply",
                        lambda _app, reply, source: SimpleNamespace(spoken="Short summary.") )
    app.audio_coordinator = SimpleNamespace(speak=lambda text, **kwargs: spoken.append((text, kwargs)))
    panel.try_digest()
    assert spoken == [("Short summary.", {"voice_id": None, "speed": 1, "volume": .8, "category": "readback"})]
    assert len(spoken) == 1
