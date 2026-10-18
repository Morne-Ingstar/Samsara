"""Public catalogue and Settings model ownership, with no real HTTP or keys."""
import json
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from samsara import openrouter_catalog as catalog
from samsara.ui.settings.ava_cloud_qt import _OpenRouterPicker
from tests.test_cloud_consent_sticks_537 import harness as settings_harness, local_accepted


@pytest.fixture
def harness(settings_harness):
    make, dialogs, sent = settings_harness

    def make_ava(*args, **kwargs):
        window = make(*args, **kwargs)
        window.show_tab('Ava')  # Settings constructs this destination on demand.
        return window

    return make_ava, dialogs, sent


def model(model_id="example/paid", **extra):
    return {"id": model_id, "name": "Example model", "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "context_length": 32000, "supported_parameters": ["tools", "response_format"], **extra}


def response(rows):
    payload = {"data": rows}
    return Mock(content=json.dumps(payload).encode(), json=lambda: payload, status_code=200)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(catalog.ai_transport.requests.Session, "request",
                        lambda *a, **k: pytest.fail("real HTTP forbidden"))


def test_malformed_entries_are_safe_and_metadata_is_bounded():
    payload = {"data": [None, "x", {}, model(), model(), model("broken", name=None, pricing=[],
                     context_length=True, expiration_date="oops", supported_parameters={}),
                     model("x" * 257), model("bad\nid"), model("sentinel", pricing={"prompt": "-1", "completion": "NaN"})]}
    rows = catalog.parse_models(payload)
    assert [row["id"] for row in rows] == ["example/paid", "broken", "sentinel"]
    assert rows[1]["name"] == "broken" and rows[1]["context_length"] is None
    assert rows[1]["expiration_date"] is None and rows[1]["supported_parameters"] == []
    assert rows[2]["pricing"] == {"prompt": None, "completion": None}


@pytest.mark.parametrize("rows", [[], [model(str(i)) for i in range(5000)]])
def test_empty_and_5000_entries_are_capped(rows):
    assert len(catalog.parse_models({"data": rows})) == min(len(rows), catalog.MAX_ENTRIES)


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}])
def test_invalid_envelopes_are_rejected(payload):
    with pytest.raises(ValueError):
        catalog.parse_models(payload)


def test_filters_search_name_id_free_suffix_zero_price_and_both_capabilities():
    rows = catalog.parse_models({"data": [model(), model("example/free:free"), model("example/zero", pricing={"prompt": "0.0", "completion": "0"}),
        model("tools-only", supported_parameters=["tools"]), model("json-only", supported_parameters=["structured_outputs"])]})
    assert catalog.matches(rows[0], "EXAMPLE MODEL") and catalog.matches(rows[0], "example/paid")
    assert not catalog.matches(rows[0], free_only=True)
    assert catalog.matches(rows[1], free_only=True) and catalog.matches(rows[2], free_only=True)
    assert catalog.matches(rows[0], capabilities=True)
    assert not catalog.matches(rows[3], capabilities=True) and not catalog.matches(rows[4], capabilities=True)


def test_public_transport_no_headers_daily_cache_refresh_and_age(monkeypatch, tmp_path):
    get = Mock(return_value=response([model()]))
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", get)
    path = tmp_path / "catalogue.json"
    first = catalog.load_catalogue(path, now=100000)
    get.assert_called_once_with("https://openrouter.ai/api/v1/models", timeout=8)
    get.return_value.close.assert_called_once()
    assert first.age_hours(103600) == 1
    assert catalog.load_catalogue(path, now=103600) == first and get.call_count == 1
    catalog.load_catalogue(path, refresh=True, now=103601)
    assert get.call_count == 2
    catalog.load_catalogue(path, now=103601 + catalog.DAY)
    assert get.call_count == 3
    assert len(list(tmp_path.iterdir())) == 1


def test_shared_transport_refuses_redirects_and_environment_proxies(monkeypatch, tmp_path):
    fake = Mock()
    fake.status_code = 302
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.request.return_value = fake
    monkeypatch.setattr(catalog.ai_transport.requests, "Session", lambda: session)
    result = catalog.load_catalogue(tmp_path / "cache", now=100000)
    assert result.reason == "offline"
    assert session.trust_env is False
    session.request.assert_called_once_with("GET", catalog.URL, timeout=8, allow_redirects=False)
    fake.close.assert_called_once()


def test_offline_keeps_stale_cache_and_throttles_failed_attempts(monkeypatch, tmp_path):
    path = tmp_path / "cache"
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", lambda *a, **k: response([model()]))
    old = catalog.load_catalogue(path, now=100000)
    failed = Mock(side_effect=catalog.ai_transport.requests.exceptions.ConnectionError())
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", failed)
    offline = catalog.load_catalogue(path, now=200000)
    assert offline.models == old.models and offline.fetched_at == old.fetched_at and offline.reason == "offline"
    assert catalog.load_catalogue(path, now=200001) == offline and failed.call_count == 1
    catalog.load_catalogue(path, refresh=True, now=200002)
    assert failed.call_count == 2


def test_corrupt_oversize_and_future_cache_do_not_suppress_fetch(monkeypatch, tmp_path):
    path = tmp_path / "cache"
    get = Mock(return_value=response([]))
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", get)
    for raw in (b"not json", b"x" * (catalog.MAX_BYTES + 1), json.dumps({"data": [], "attempted_at": 200000}).encode(),
                b"[" * 2000 + b"0" + b"]" * 2000,
                json.dumps({"data": [], "attempted_at": 10 ** 1000}).encode()):
        path.write_bytes(raw)
        assert catalog.load_catalogue(path, now=100000).fetched_at == 100000
    assert get.call_count == 5


@pytest.mark.parametrize("bad", [Mock(content=b"x" * (catalog.MAX_BYTES + 1)), Mock(content=b"{}", json=lambda: {})])
def test_invalid_response_is_short_reason_and_daily_throttled(monkeypatch, tmp_path, bad):
    get = Mock(return_value=bad)
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", get)
    path = tmp_path / "cache"
    assert catalog.load_catalogue(path, now=100000).reason == "invalid"
    assert catalog.load_catalogue(path, now=100001).reason == "invalid" and get.call_count == 1


def test_atomic_replace_failure_preserves_old_cache_and_cleans_temp(monkeypatch, tmp_path):
    path = tmp_path / "cache"
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", lambda *a, **k: response([model()]))
    catalog.load_catalogue(path, now=100000)
    original = path.read_bytes()
    monkeypatch.setattr(catalog.os, "replace", Mock(side_effect=OSError()))
    result = catalog.load_catalogue(path, refresh=True, now=100001)
    assert result.reason == "cache" and result.models
    assert path.read_bytes() == original and len(list(tmp_path.iterdir())) == 1


def test_unwritable_cache_still_throttles_automatic_requests_in_session(monkeypatch, tmp_path):
    get = Mock(return_value=response([model()]))
    monkeypatch.setattr(catalog.ai_transport, "get_cloud", get)
    monkeypatch.setattr(catalog.os, "replace", Mock(side_effect=OSError()))
    path = tmp_path / "cache"
    first = catalog.load_catalogue(path, now=100000)
    assert first.reason == "cache"
    assert catalog.load_catalogue(path, now=100001) == first and get.call_count == 1
    catalog.load_catalogue(path, refresh=True, now=100002)
    assert get.call_count == 2


@pytest.fixture
def picker(qapp):
    entry = QLineEdit()
    entry.setText("custom/kept")
    widget = _OpenRouterPicker(entry, lambda: True)
    yield widget
    widget.deleteLater()
    entry.deleteLater()


def ids(picker):
    return [picker.models.item(i).data(Qt.ItemDataRole.UserRole) for i in range(picker.models.count())]


def test_custom_auto_filters_accessibility_and_keyboard_choice(picker):
    picker.receive(catalog.Catalogue(catalog.parse_models({"data": [model(), model("example/free:free")]})))
    assert ids(picker) == ["openrouter/auto", "custom/kept", "example/paid", "example/free:free"]
    picker.search.setText("no match")
    picker.free.setChecked(True)
    picker.capabilities.setChecked(True)
    assert ids(picker) == ["openrouter/auto", "custom/kept"]
    assert picker.models.item(1).text() == "Custom: custom/kept"
    for control in (picker.search, picker.free, picker.capabilities, picker.models):
        assert control.accessibleName()
    picker.show()
    picker.models.setFocus()
    picker.models.setCurrentRow(0)
    QTest.keyClick(picker.models, Qt.Key.Key_Return)
    assert picker.entry.text() == "openrouter/auto"


def test_prices_context_and_near_past_expiration_are_visible(picker):
    for delta, warning in [(-1, "expired"), (7, "expires"), (60, None)]:
        row = catalog.parse_models({"data": [model(expiration_date=(date.today() + timedelta(days=delta)).isoformat())]})[0]
        text = picker.row_text(row)
        assert "in $1" in text and "out $2" in text and "32,000" in text
        assert ("Warning:" in text) == (warning is not None)
        if warning:
            assert warning in text


def test_request_runs_in_registry_worker_once_and_route_changes_block_choice(picker, monkeypatch):
    calls = []
    monkeypatch.setattr("samsara.ui.settings.ava_cloud_qt.thread_registry.spawn", lambda name, fn, **k: calls.append(fn))
    get = Mock(return_value=catalog.Catalogue(reason="offline"))
    monkeypatch.setattr(catalog, "load_catalogue", get)
    picker.load()
    picker.load()
    assert not get.called and len(calls) == 1
    picker.active = lambda: False
    calls[0]()
    picker.models.setCurrentRow(0)
    picker.select_model()
    picker.load(refresh=True)
    assert picker.entry.text() == "custom/kept" and len(calls) == 1
    assert "Could not reach" in picker.status.text()


def openrouter_config():
    config = local_accepted()
    config["ava"]["provider_policy"] = "cloud"
    config["cloud_llm"].update(provider="openrouter", enabled=True, model="custom/original")
    from samsara.ui import ava_consent_qt
    config["ava"]["consent"] = ava_consent_qt.accepted_consent(config, cloud_enabled=True)
    return config


def test_settings_start_and_other_routes_never_fetch_or_read_cache(harness, monkeypatch):
    get = Mock(side_effect=AssertionError("catalogue accessed before picker open"))
    monkeypatch.setattr(catalog, "load_catalogue", get)
    make, _, _ = harness
    window = make(openrouter_config())
    picker = window._widgets["openrouter_picker"]
    row = window._rows_by_widget["cloud_model"]
    assert picker.parent() is row and picker.isHidden()
    assert picker.free_label.text() == "Free only"
    assert picker.capabilities_label.text() == "Supports tools / JSON"
    window._widgets["providerRoute"].setCurrentIndex(1)
    window._open_openrouter_picker()
    window._widgets["cloud_provider"].setCurrentText("OpenAI")
    window._open_openrouter_picker()
    assert not get.called and picker.isHidden()


def test_picker_choice_stages_then_applies_same_model_key_for_openrouter(harness):
    make, _, _ = harness
    window = make(openrouter_config())
    picker = window._widgets["openrouter_picker"]
    picker.receive(catalog.Catalogue(catalog.parse_models({"data": [model()]})))
    picker.models.setCurrentRow(2)
    picker.select_model()
    assert window._widgets["cloud_model"].text() == "example/paid"
    assert window.app.config["cloud_llm"]["model"] == "custom/original"
    window._apply_and_close()
    assert window.app.config["cloud_llm"]["provider"] == "openrouter"
    assert window.app.config["cloud_llm"]["model"] == "example/paid"


def test_outside_provider_change_parks_picker_choice_under_openrouter_only(harness):
    make, _, _ = harness
    window = make(openrouter_config())
    picker = window._widgets["openrouter_picker"]
    picker.models.setCurrentRow(0)
    picker.select_model()
    window.app.config["cloud_llm"].update(provider="openai", model="other/owned")
    window._apply_and_close()
    cloud = window.app.config["cloud_llm"]
    assert cloud["provider"] == "openai" and cloud["model"] == "other/owned"
    assert cloud["provider_settings"]["openrouter"]["model"] == "openrouter/auto"


def test_offline_manual_id_still_saves_and_auto_default_is_unchanged(harness):
    make, _, _ = harness
    window = make(openrouter_config())
    window._widgets["openrouter_picker"].receive(catalog.Catalogue(reason="offline"))
    window._widgets["cloud_model"].setText("custom/manual")
    window._apply_and_close()
    assert window.app.config["cloud_llm"]["model"] == "custom/manual"
    from samsara import cloud_llm
    assert cloud_llm.BUILTIN_PROVIDERS["openrouter"]["model"] == "google/gemini-3.1-flash-lite"


def test_auto_applies_without_pinning_provider_default(harness, monkeypatch):
    from samsara import cloud_llm
    make, _, _ = harness
    window = make(openrouter_config())
    picker = window._widgets["openrouter_picker"]
    picker.models.setCurrentRow(0)
    picker.select_model()
    window._apply_and_close()
    assert window.app.config["cloud_llm"]["model"] == "openrouter/auto"
    monkeypatch.setitem(cloud_llm.BUILTIN_PROVIDERS["openrouter"], "model", "future/default")
    assert cloud_llm._get_provider_config(window.app)[2] == "openrouter/auto"


def test_unexpected_fetch_exception_returns_picker_to_idle_and_allows_retry(picker, monkeypatch, tmp_path):
    workers = []
    monkeypatch.setattr("samsara.ui.settings.ava_cloud_qt.samsara_home_dir", lambda: tmp_path)
    monkeypatch.setattr("samsara.ui.settings.ava_cloud_qt.thread_registry.spawn",
                        lambda name, fn, **k: workers.append(fn))
    fetch = Mock(side_effect=MemoryError())
    monkeypatch.setattr(catalog, "load_catalogue", fetch)
    picker.load()
    assert picker.busy and not picker.refresh.isEnabled()
    workers.pop()()
    assert not picker.busy and picker.refresh.isEnabled()
    assert picker.catalogue.reason == "offline" and "Could not reach" in picker.status.text()
    assert picker.entry.text() == "custom/kept"
    fetch.side_effect = None
    fetch.return_value = catalog.Catalogue(catalog.parse_models({"data": [model()]}))
    picker.refresh.click()
    workers.pop()()
    assert not picker.busy and "example/paid" in ids(picker)


def test_status_without_reason_has_single_spaces(picker):
    picker.receive(catalog.Catalogue(catalog.parse_models({"data": [model()]}), fetched_at=1))
    assert "  " not in picker.status.text()
    assert ". You can enter" in picker.status.text()


def test_free_filter_includes_prices_displayed_as_zero(picker):
    rows = catalog.parse_models({"data": [
        model("tiny/free", pricing={"prompt": "0.00000000001", "completion": "0.00000000001"}),
        model("tiny/paid", pricing={"prompt": "0.00000000006", "completion": "0"}),
        model("unknown", pricing={}),
    ]})
    picker.receive(catalog.Catalogue(rows))
    assert "in $0 · out $0" in picker.row_text(rows[0])
    picker.free.setChecked(True)
    assert ids(picker) == ["openrouter/auto", "custom/kept", "tiny/free"]
