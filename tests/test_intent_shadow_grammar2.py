"""Queue 197: the tier-2 grammar result is observer-only shadow evidence."""

from datetime import datetime

import pytest

from samsara.intent import shadow


@pytest.fixture(scope="module")
def resolver():
    from samsara import command_catalog as cc
    from samsara.intent.resolve import IntentResolver
    return IntentResolver(cc.load_catalog_json())


def test_logged_row_carries_the_independent_grammar_verdict(tmp_path, resolver):
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    # 265: next-tab is eligible only with a supported browser focused.
    observer = shadow.IntentShadow(lambda: resolver, lambda: config, pid_fn=lambda: None,
                                   name_fn=lambda _pid: "chrome.exe")

    entry = observer.record("next tab", "staged", datetime(2026, 9, 17, 12, 0), None)

    verdict = entry["grammar2"]
    assert verdict["count"] == 1
    assert verdict["canonical_id"] == "builtin.next_tab"
    assert verdict["args"] == {}
    assert isinstance(verdict["elapsed_us"], int) and verdict["elapsed_us"] >= 0
    # Existing resolver evidence remains independent and unmodified.
    assert entry["tier"] == "exact"
    assert entry["would"] == "command:builtin.next_tab"


def test_unknown_foreground_keeps_grammar2_independent_of_scope(tmp_path, resolver):
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    observer = shadow.IntentShadow(lambda: resolver, lambda: config, pid_fn=lambda: None,
                                   name_fn=lambda _pid: None)
    entry = observer.record("next tab", "staged", datetime(2026, 9, 17, 12, 0), None)

    assert entry["app"] is None
    assert entry["would"] != "command:builtin.next_tab"
    assert entry["grammar2"]["count"] == 1
    assert entry["grammar2"]["canonical_id"] == "builtin.next_tab"
    assert entry["grammar2"]["args"] == {}


def test_grammar2_parse_p95_stays_under_one_ms(resolver):
    # Warm the catalog index before timing the worker-side parse itself.
    shadow.grammar2_verdict("next tab", resolver.records)
    samples = [shadow.grammar2_verdict("next tab", resolver.records)["elapsed_us"]
               for _ in range(250)]
    p95_us = sorted(samples)[int(len(samples) * 0.95)]

    assert p95_us < 1_000, f"grammar2 p95 was {p95_us} us"
