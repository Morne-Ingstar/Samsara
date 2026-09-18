"""Queue 220 D: bounded, local correction proposals."""
from samsara.correction_candidates import (
    MAX_PHONETIC_TERMS,
    ApprovedCorrection,
    build_snapshot,
    suggest_candidates,
)


def replacements(text, **kwargs):
    return [candidate.replacement for candidate in suggest_candidates(text, (0, len(text)), **kwargs)]


def test_empty_personal_dictionary_uses_bundled_homophones_and_confusions():
    assert {"too", "two"}.issubset(replacements("to"))
    assert {"their", "they're"}.issubset(replacements("there"))
    assert {"your", "you're"}.issubset(replacements("youre"))
    assert "it's" in replacements("its")
    assert "won" in replacements("one")
    assert "2" in replacements("to")


def test_case_apostrophe_number_and_split_join_variants():
    assert {"TO", "To"}.issubset(replacements("to"))
    assert "they're" in replacements("theyre")
    assert "42" in replacements("forty two")
    assert "forty two" in replacements("42")
    assert "-forty two" in replacements("-42")
    assert "alot" in replacements("a lot")
    assert "a lot" in replacements("alot")
    assert replacements("12/31") == []
    assert replacements("3.14") == []


def test_split_join_uses_complete_adjacent_span():
    candidates = suggest_candidates(
        "a", (0, 1), adjacent_tokens={"right": {"text": "lot", "span": (2, 5)}}
    )
    assert any(candidate.replacement == "alot" and candidate.span == (0, 5) for candidate in candidates)


def test_non_english_draft_gets_only_matching_personal_mappings():
    snapshot = build_snapshot([
        {"wrong": "hola", "right": "ola", "language": "es", "approved_at": 3},
        {"wrong": "hola", "right": "hello", "language": "en", "approved_at": 4},
    ])
    candidates = suggest_candidates("hola", (4, 8), language="es", snapshot=snapshot)
    assert [(item.replacement, item.tier) for item in candidates] == [("ola", 1)]


def test_personal_approval_is_newest_first_and_original_is_not_a_candidate():
    snapshot = build_snapshot([
        ApprovedCorrection("teh", "the older", "en", 1),
        ApprovedCorrection("teh", "the newer", "en", 2),
    ])
    candidates = suggest_candidates("teh", (0, 3), snapshot=snapshot)
    assert [item.replacement for item in candidates[:2]] == ["the newer", "the older"]
    assert all(item.replacement != "teh" for item in candidates)


def test_dedup_is_exact_replacement_and_span_and_order_is_repeatable():
    snapshot = build_snapshot({"to": {"right": "too", "approved_at": 10}})
    first = suggest_candidates("to", (0, 2), snapshot=snapshot)
    second = suggest_candidates("to", (0, 2), snapshot=snapshot)
    assert first == second
    assert len({(item.replacement, item.span) for item in first}) == len(first)
    assert sum(item.replacement == "too" for item in first) == 1


def test_phonetic_work_is_bounded_to_snapshot_terms(monkeypatch):
    seen = {}

    def fake_neighbours(word, terms, limit=5):
        seen["terms"] = tuple(terms)
        return []

    monkeypatch.setattr("samsara.correction_candidates.phonetic_neighbours", fake_neighbours)
    snapshot = build_snapshot(taught_vocabulary=[f"Name{i}" for i in range(MAX_PHONETIC_TERMS + 20)])
    suggest_candidates("Morn", (0, 4), snapshot=snapshot)
    assert len(seen["terms"]) == MAX_PHONETIC_TERMS


def test_unsupported_token_returns_no_proposals():
    assert suggest_candidates("@@@", (10, 13)) == []
