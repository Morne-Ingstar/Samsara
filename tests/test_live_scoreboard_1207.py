"""Synthetic fixtures only: never inspect an app, real corpus, model or Git."""
import json

import numpy as np
import pytest

from tools.voice_corpus import live_scoreboard as live
from tools.voice_corpus.storage import digest, write_wav


def pair(root, name="take", side=None):
    root.mkdir(parents=True, exist_ok=True)
    wav = root / (name + ".wav")
    write_wav(wav, np.zeros(32000, dtype=np.int16))
    wav.with_suffix(".json").write_text(json.dumps(side or {}), encoding="utf-8")
    return dict(id=name, wav=str(wav), audio_sha256=digest(wav))


def test_pairing_editions_and_incomplete_pairs(tmp_path):
    pair(tmp_path / "private")
    pair(tmp_path / "daily")
    write_wav(tmp_path / "orphan.wav", np.zeros(160, dtype=np.int16))
    (tmp_path / "pending.json").write_text("{}")
    rows, skipped = live.discover(tmp_path)
    assert len(rows) == 2
    assert {r["edition"] for r in rows} == {"daily", "private"}
    assert {r["reason"] for r in skipped} == {"missing sidecar", "missing WAV"}
    assert len(live.discover(tmp_path, allow_unpaired=True)[0]) == 3


def test_cache_hash_and_model_policy(tmp_path, monkeypatch):
    # The runner's test temp lives under reports; these are synthetic strings.
    monkeypatch.setattr(live, "cache_path_allowed", lambda path: path.is_relative_to(tmp_path))
    row = pair(tmp_path)
    calls = []
    def decode(r):
        calls.append(r["id"])
        return dict(text="synthetic", words=[])
    cache = live.ReferenceCache({"model.bin": "one"})
    assert cache.get(row, decode)["text"] == "synthetic"
    cache.get(row, decode)
    live.ReferenceCache({"model.bin": "one"}).get(row, decode)
    assert len(calls) == 1
    live.ReferenceCache({"model.bin": "two"}).get(row, decode)
    live.ReferenceCache({"model.bin": "one"}, {"beam_size": 1}).get(row, decode)
    changed = dict(row, audio_sha256="different")
    cache.get(changed, decode)
    assert len(calls) == 4
    assert live.discover(tmp_path)[1] == []  # Cache JSON is not an orphan sidecar.


def test_memory_only_cache_never_writes_text(tmp_path):
    row = pair(tmp_path)
    live.ReferenceCache({}, memory_only=True).get(row, lambda r: dict(text="synthetic", words=[]))
    assert not (tmp_path / ".live_scoreboard_refs").exists()


def test_cache_rejects_reports_and_repositories(tmp_path):
    for directory in (tmp_path / "reports", tmp_path / "repo"):
        directory.mkdir()
        if directory.name == "repo":
            (directory / ".git").mkdir()
        row = pair(directory)
        with pytest.raises(ValueError, match="cache cannot live"):
            live.ReferenceCache({}).get(row, lambda r: {})


@pytest.mark.parametrize("side, expected", [
    ({"release_audio_ms": 800, "captured_audio_ms": 2000, "admitted_tail_ms": 500}, (.8, "release_audio_ms")),
    ({"captured_audio_ms": 2000, "admitted_tail_ms": 300}, (1.7, "captured_audio_ms-admitted_tail_ms")),
    ({"release_s": .7}, (.7, "release_s")),
])
def test_release_point_uses_audio_time(side, expected):
    assert live.release_point(side, 2) == expected


@pytest.mark.parametrize("side", [{}, {"captured_audio_ms": 2000, "release_to_stop_ms": 400}])
def test_unknown_release_requires_explicit_assumption(side):
    with pytest.raises(ValueError, match="Unknown release"):
        live.release_point(side, 2)
    assert live.release_point(side, 2, allow_unknown=True) == (2, "assumed-file-end")


@pytest.mark.parametrize("value", [-1, 2200, float("nan"), float("inf")])
def test_invalid_release_not_clamped(value):
    with pytest.raises(ValueError, match="outside retained"):
        live.release_point({"release_audio_ms": value}, 2, allow_unknown=True)


def fixture_row(name="take"):
    return dict(id=name, edition="private", microphone="fixture", duration_s=6,
                release_s=5, release_source="release_audio_ms", audio_sha256="fixture")


def fixture_reference():
    return dict(text="alpha beta middle delta omega", words=[
        dict(word=word, start=start, end=start + .3)
        for word, start in zip("alpha beta middle delta omega".split(), [0, 1, 3, 4.5, 5])])


def test_three_lists_use_deleted_words_and_reference_speech_times():
    ref = fixture_reference()
    rows = [live.score_take(fixture_row("head"), ref, "alpha middle delta omega"),
            live.score_take(fixture_row("tail"), ref, "alpha beta middle omega"),
            live.score_take(fixture_row("insert"), ref, "alpha beta middle delta omega extra"),
            live.score_take(fixture_row("substitute"), ref, "alpha beta middle delta other")]
    run = live.summarize(rows)
    assert run["lost_first_1_5s"] == ["head"]
    assert run["lost_last_1_5s"] == ["tail"]
    assert run["inserted"] == ["insert"]
    assert run["totals"]["deletions"] == 2
    assert run["totals"]["substitutions"] == 1
    assert run["groups"]["microphone"]["fixture"]["takes"] == 4
    assert "alpha" not in json.dumps(run)  # No reference/hypothesis text in reports.


def test_speech_edge_is_not_file_edge_and_empty_candidate_is_included():
    ref = fixture_reference()
    ref["words"] = [dict(w, start=w["start"] + 10, end=w["end"] + 10) for w in ref["words"]]
    row = live.score_take(fixture_row(), ref, "")
    run = live.summarize([row])
    assert row["lost_head"] and row["lost_tail"]
    assert run["totals"]["reference_words"] == 5
    assert run["totals"]["wer"] == 1
    assert run["totals"]["off_script_flags"] == 1


def test_empty_reference_insertions_and_unknown_word_timing():
    row = live.score_take(fixture_row(), dict(text="", words=[]), "extra")
    assert row["insertions"] == 1
    assert not row["lost_head"] and not row["lost_tail"]
    bad = live.score_take(fixture_row(), dict(text="alpha", words=[]), "")
    assert live.summarize([bad])["unknown_word_timing"] == ["take"]


def test_numeric_and_multiword_punctuation_time_mapping():
    ref = dict(text="item 104 question mark end", words=[
        dict(word=w, start=i, end=i + .2)
        for i, w in enumerate("item 104 question mark end".split())])
    assert len(live.timed_tokens(ref)) == 5
    row = live.score_take(fixture_row(), ref, "item one hundred four end")
    assert row["wer"] == 0


def test_prepare_duplicate_stems_and_microphone(tmp_path):
    pair(tmp_path / "private", side={"release_audio_ms": 1000, "device_name": "fixture"})
    pair(tmp_path / "daily", side={"release_audio_ms": 900})
    rows = live.prepare(live.discover(tmp_path)[0])
    assert len({r["id"] for r in rows}) == 2
    assert {r["microphone"] for r in rows} == {"fixture", "unknown"}


def test_alignment_extension_preserves_existing_scorer_result():
    plain = live._measure(["a", "b", "c"], ["a", "c"])
    aligned = live._measure(["a", "b", "c"], ["a", "c"], alignment=True)
    assert aligned.pop("deleted_reference_indices") == [1]
    assert aligned == plain


def test_fallback_proxy_caveat_reaches_every_group():
    row = live.score_take(fixture_row(), fixture_reference(), "alpha beta middle delta omega")
    row["caveat"] = "vs turbo proxy, not human truth"
    run = live.summarize([row])
    assert run["caveat"] == row["caveat"]
    assert run["totals"]["caveat"] == row["caveat"]
    assert all(v["caveat"] == row["caveat"] for g in run["groups"].values() for v in g.values())


def test_duplicate_archives_do_not_weight_totals_twice():
    a = fixture_row("source")
    copy = dict(a, id="archive")
    other_release = dict(a, id="other-release", release_s=4)
    selected, duplicates = live.deduplicate([a, copy, other_release])
    assert [r["id"] for r in selected] == ["source", "other-release"]
    assert duplicates == [dict(id="archive", same_as="source")]
