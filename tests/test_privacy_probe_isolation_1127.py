"""1127: delivery without History must not leak native work into Settings."""
from types import SimpleNamespace

import pytest

from samsara import recent_privacy as privacy


@pytest.mark.parametrize("fields", [{}, {"history_db": None, "history": None}])
def test_delivery_without_history_does_not_probe_and_keeps_later_words_hidden(monkeypatch, fields):
    app = SimpleNamespace(**fields)

    def unwanted_probe():
        pytest.fail("delivery with no History must not start native UIA")

    monkeypatch.setattr(privacy, "focused_target", unwanted_probe)
    assert privacy.before_delivery(app, "synthetic")
    assert app._recent_delivery_privacy[1].reason == "unverified_target"
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy(""))
    assert privacy.legacy_text(app, "synthetic") == ""


@pytest.mark.parametrize("database", [False, True])
def test_delivery_with_history_still_checks_and_redacts(monkeypatch, database):
    calls = []
    monkeypatch.setattr(privacy, "focused_target", lambda: (
        calls.append(True) or privacy.TargetPrivacy("password_field")))
    app = SimpleNamespace(history=[("synthetic", "synthetic", False)])
    concealed = []
    if database:
        def conceal(text, reason, target):
            concealed.append((text, reason, target))
            return [1]
        app.history_db = SimpleNamespace(conceal_delivery=conceal)
    assert privacy.before_delivery(app, "synthetic")
    assert calls == [True]
    assert app.history == [("synthetic", "", False)]
    assert app._recent_delivery_privacy[1].reason == "password_field"
    if database:
        assert concealed == [("synthetic", "password_field", "")]
        assert app._recent_sensitive_ids == {1: "password_field"}
