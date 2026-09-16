"""Queue 128: the output-text quality gates moved out of dictation.py.

samsara/transcript_gates.py holds the checks that decide whether a finished
decode is real speech, plus the queue-106 context-tail policy that feeds the
next one. The move was verbatim -- this file pins the *seam*, not the
behaviour those gates implement (that is already pinned by
test_hallucination_blacklist, test_quality_exhaustion_guard,
test_long_dictation_quality, test_context_prompt_contamination_106 and
test_command_mode_utterance_hallucination, which all still reach for the
names through ``dictation``).

Three things have to stay true or the extraction has changed something:

  1. The module imports on its own, with no app, no Qt and no model.
  2. It never imports dictation. A cycle here is how this class of change
     breaks silently.
  3. Every name it now owns still resolves as ``dictation.<name>``, and is
     the SAME object -- so the twenty-odd call sites outside this file that
     reach through dictation are reaching the code that actually runs.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parents[1]

#: Every public-to-the-rest-of-the-tree name the seam took with it.
MOVED_NAMES = (
    "_COMPRESSION_RATIO_THRESHOLD",
    "_HALLUCINATION_STRING_BLACKLIST",
    "_is_hallucinated_segments",
    "_TRAILING_GARBAGE_RUN_RE",
    "_trim_trailing_garbage_run",
    "_drop_trailing_garbage_segments",
    "_CONTEXT_WORD_STRIP",
    "_context_words",
    "_ECHO_MIN_WORDS",
    "_CONTEXT_ECHO_CHIP",
    "_is_context_echo",
    "_TAIL_REPEAT_RUN",
    "_CONTEXT_TAIL_CHARS",
    "_SENTENCE_START_RE",
    "_sanitise_context_tail",
    "_is_quality_exhausted",
    "_keep_low_confidence_long_chunk",
    "_apply_segment_quality_gates",
)


def test_the_module_imports_standalone():
    """No app, no Qt, no model -- the gates are pure functions over text and
    segment telemetry, and importing them must cost nothing else."""
    import samsara.transcript_gates as gates

    assert gates.__name__ == "samsara.transcript_gates"


def test_the_module_does_not_import_dictation():
    """Read at the source level, so the check holds even when dictation is
    already in sys.modules because another test imported it first."""
    tree = ast.parse((ROOT / "samsara" / "transcript_gates.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "dictation" not in imported


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_the_name_still_resolves_through_dictation(name):
    import dictation
    import samsara.transcript_gates as gates

    assert hasattr(dictation, name), f"dictation.{name} no longer resolves"
    assert getattr(dictation, name) is getattr(gates, name), (
        f"dictation.{name} is a different object from the one that runs"
    )


def test_the_gates_still_run_through_the_dictation_path():
    """One call per moved function, through dictation, on the shapes their
    own test files already pin -- a smoke check that the re-export is a live
    binding and not a stale copy."""
    import dictation

    assert dictation._is_hallucinated_segments([], "Thank you for watching!") is True
    assert dictation._trim_trailing_garbage_run("the " + "_" * 20) == "the"
    assert dictation._drop_trailing_garbage_segments([]) == []
    assert dictation._context_words('"Fuck, no."') == ["fuck", "no"]
    assert dictation._is_context_echo("a b c d", "x y z a b c d") is True
    assert dictation._sanitise_context_tail("real words here ha ha ha") == "real words here"
    assert dictation._is_quality_exhausted([], {}) is False
    assert dictation._keep_low_confidence_long_chunk([], "", 0.0) is False
    assert dictation._apply_segment_quality_gates([], {}, 0.0) == ("", False)


def test_hf_bench_can_still_find_the_gates_it_sources():
    """tools/hf_bench.py builds its offline adapter by scraping definitions
    out of the source, not by importing -- so dictation's re-export is
    invisible to it and it has to read the gates from the file they moved
    to. It raises RuntimeError naming the missing helpers when it cannot,
    which is the failure this guards."""
    sys.path.insert(0, str(ROOT / "tools"))
    import hf_bench

    production = hf_bench.load_production_adapter()
    for name in ("_is_hallucinated_segments", "_is_quality_exhausted",
                 "_keep_low_confidence_long_chunk", "_apply_segment_quality_gates",
                 "_COMPRESSION_RATIO_THRESHOLD", "_HALLUCINATION_STRING_BLACKLIST"):
        assert hasattr(production, name), f"hf_bench lost {name} in the move"
