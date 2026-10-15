"""Public benchmark evidence keeps measurements without corpus text.

Walk the exported folder directly: this guard also works without Git metadata.
Report locations only, never the private values that caused a failure.
"""

import json
from pathlib import Path

import pytest


PRIVATE_KEYS = {
    "reference", "hypothesis", "text", "transcript", "tail",
    "ref", "hyp", "truth", "live", "tails", "tail_live_result",
    "decodes", "logged", "target_had", "pending_checks",
}
ROOT = Path(__file__).resolve().parents[1]


def private_locations(value, location="$", private=False):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from private_locations(
                item, f"{location}.{key}", private or key.lower() in PRIVATE_KEYS
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from private_locations(item, f"{location}[{index}]", private)
    elif isinstance(value, str):
        if (private and value) or "/users/" in value.replace("\\", "/").lower():
            yield location


def test_no_private_benchmark_text():
    # A superset of tracked JSONs; includes new exports before they are staged.
    paths = sorted((ROOT / "perf_artifacts").rglob("*.json"))
    assert paths, "benchmark evidence directory is missing"
    problems = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        problems.extend(
            f"{path.relative_to(ROOT)}:{location}"
            for location in private_locations(data)
        )
    assert not problems, "Private benchmark fields at:\n" + "\n".join(problems)


@pytest.mark.parametrize("key", sorted(PRIVATE_KEYS))
def test_guard_catches_nested_private_fields(key):
    assert list(private_locations({"rows": [{key: {"value": "synthetic"}}]}))


@pytest.mark.parametrize("path", ["C:\\Users\\example\\clip.wav", "/Users/example/clip.wav"])
def test_guard_catches_personal_paths(path):
    assert list(private_locations({"rows": [{"audio": path}]}))


def test_guard_accepts_numeric_evidence_and_removed_text():
    assert not list(private_locations({
        "text": "", "tail": None, "pending_checks": [[None, 3, 5]],
        "model": "small", "wer": 0.12, "wav": "<owner-clip-001>",
    }))
