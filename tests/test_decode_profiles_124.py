"""Queue 124: no decode profile may silently drop dictated text.

The defect: `balanced` shipped with `without_timestamps=True`, and on five
minutes of continuous speech it returned 94% of the words on `base` and 66%
on `small` -- with no warning. Short utterances hide it completely, which is
why every bench before queue 118 missed it.

The mechanism, isolated one parameter at a time (see the queue 124 report)
and then read out of faster_whisper/transcribe.py: with no timestamp tokens
the decoder can only advance `seek` in whole 30-second blocks, so a window
culled by no_speech_threshold/log_prob_threshold takes 30 seconds of speech
with it and a partially-decoded window cannot resume at the last word.
`condition_on_previous_text` -- which the 118 report blamed -- is not
involved: flipping it alone changed nothing.

Two kinds of test here:

  * Contract tests, no model. They read the REAL parameter dicts out of
    dictation.py's own `get_transcription_params` by AST. These are the
    regression guard the codebase did not have: nothing asserted this
    parameter before.
  * Live decode tests, `slow`, skipped unless the LibriSpeech corpus is on
    this machine. They are the only honest way to assert "returns 95% of the
    words", so they decode real audio with the real model.

Nothing here imports dictation.py.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DICTATION = REPO / "dictation.py"
#: The corpus the 118 baseline and the queue 124 measurements used.
LIBRI = Path("D:/samsara-bench/LibriSpeech/test-clean")
LONG_CACHE = Path("D:/samsara-bench/longform_cache")
BENCH = REPO / "tools" / "wer_bench_118.py"

#: Measured on `base` with the shipped default (`accurate`): 99.7% of the
#: reference words over 610.8 s of continuous speech. The gate is 95%, well
#: clear of the measurement and far under the 94.4% the old default returned.
MIN_WORDS_RETURNED = 0.95
#: Measured 4.46% (light normalisation) on 120 LibriSpeech clips with the
#: shipped default, against 4.12% for the old one -- 3.91% and 3.61% on
#: Whisper's own normaliser. The ceiling allows that 0.34-point cost and no
#: more.
#:
#: Light normalisation, not Whisper's EnglishTextNormalizer, on purpose:
#: importing it pulls in torch, and samsara/torch_guard.py blocks torch once
#: dictation has been imported in the process. Depending on it would make
#: these tests pass or fail according to which OTHER test file ran first.
MAX_SHORT_WER = 0.050
#: Long-form, same normalisation: measured 2.90% with the shipped default
#: against 8.21% with the old one.
MAX_LONG_WER = 0.050


# ---------------------------------------------------------------------------
# Reading the real profiles, without importing dictation
# ---------------------------------------------------------------------------

def _literal(node: ast.Dict) -> dict:
    out = {}
    for key, value in zip(node.keys, node.values):
        if key is None:                      # **base_params
            continue
        if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "dict":
            out[ast.literal_eval(key)] = {kw.arg: ast.literal_eval(kw.value)
                                          for kw in value.keywords}
            continue
        out[ast.literal_eval(key)] = ast.literal_eval(value)
    return out


def shipped_profiles() -> dict:
    """{'fast'|'balanced'|'accurate': params} from dictation.py's source."""
    tree = ast.parse(DICTATION.read_text(encoding="utf-8", errors="replace"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_transcription_params")
    out = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            params = _literal(node.value)
            name = {1: "fast", 3: "balanced", 5: "accurate"}.get(params.get("beam_size"))
            if name:
                out[name] = params
    return out


def app_config_default(key: str):
    """One value from dictation.py's own default-config dict."""
    source = DICTATION.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == key
                    and isinstance(v, ast.Constant)
                    # the defaults dict also carries model_size/microphone
                    and any(isinstance(other, ast.Constant) and other.value == "model_size"
                            for other in node.keys)):
                found.append(v.value)
    assert found, f"no default-config entry for {key!r} in dictation.py"
    return found[0]


PROFILES = shipped_profiles()


# ---------------------------------------------------------------------------
# The contract: no profile may drop long-form speech
# ---------------------------------------------------------------------------

def test_all_three_profiles_are_present():
    assert set(PROFILES) == {"fast", "balanced", "accurate"}, PROFILES.keys()


@pytest.mark.parametrize("name", ["fast", "balanced", "accurate"])
def test_no_profile_asks_the_decoder_to_drop_timestamps(name):
    """THE regression guard. `without_timestamps=True` is what lost the text:
    with no timestamp tokens faster-whisper can only advance in whole 30 s
    blocks, so a culled window costs 30 seconds of speech outright.

    If you are here because this failed: read the queue 124 report before
    changing it back. Short-clip benches will not show you the damage."""
    assert PROFILES[name]["without_timestamps"] is False, (
        f"{name} would silently drop long-form speech")


@pytest.mark.parametrize("name", ["fast", "balanced", "accurate"])
def test_word_timestamps_stays_off(name):
    """Timestamps at SEGMENT level are the fix; per-WORD timestamps are a
    separate and much more expensive feature, and nothing here needs them."""
    assert PROFILES[name]["word_timestamps"] is False


def test_the_profiles_still_differ_in_the_ways_they_are_meant_to():
    """Fixing one shared parameter must not collapse three profiles into one:
    beam size is what the user is actually choosing between."""
    assert PROFILES["fast"]["beam_size"] == 1
    assert PROFILES["balanced"]["beam_size"] == 3
    assert PROFILES["accurate"]["beam_size"] == 5
    assert PROFILES["accurate"]["condition_on_previous_text"] is True
    assert PROFILES["balanced"]["condition_on_previous_text"] is False


# ---------------------------------------------------------------------------
# The defaults, and the schema that describes them
# ---------------------------------------------------------------------------

def test_the_shipped_default_profile_is_accurate():
    from samsara import config_defaults
    from samsara.config_schema import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["performance_mode"]["default"] == "accurate"
    assert config_defaults.DEFAULTS["performance_mode"] == "accurate"
    assert "accurate" in SETTINGS_SCHEMA["performance_mode"]["options"]


def test_the_schema_device_default_matches_the_app_so_they_cannot_drift():
    """Queue 118 found the schema saying `cpu` while the app shipped `auto`,
    and the app wins at runtime -- so the schema described a first run that
    never happened. Asserted against dictation.py's OWN default dict, so the
    two cannot drift apart again without this failing."""
    from samsara import config_defaults
    from samsara.config_schema import SETTINGS_SCHEMA

    app_default = app_config_default("device")
    assert app_default == "auto"
    assert SETTINGS_SCHEMA["device"]["default"] == app_default
    assert config_defaults.DEFAULTS["device"] == app_default
    # And the enum has to be able to express it.
    assert app_default in SETTINGS_SCHEMA["device"]["options"]


def test_the_default_model_is_still_base():
    """The measurements in the queue 124 report are on `base`. If the shipped
    model changes, they need redoing."""
    from samsara.config_schema import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["model_size"]["default"] == "base"


# ---------------------------------------------------------------------------
# Live decode. The only honest way to assert "returns 95% of the words".
# ---------------------------------------------------------------------------

_corpus_missing = pytest.mark.skipif(
    not LIBRI.exists() or not BENCH.exists(),
    reason="LibriSpeech test-clean corpus not on this machine")


@pytest.fixture(scope="module")
def bench():
    import importlib.util

    spec = importlib.util.spec_from_file_location("wer_bench_118", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shipped_params(bench):
    """The shipped default's parameters, registered with the bench harness so
    the decode under test is the one the app performs."""
    from samsara.config_schema import SETTINGS_SCHEMA

    default = SETTINGS_SCHEMA["performance_mode"]["default"]
    bench.PARAMS["ship"] = dict(PROFILES[default])
    return "ship"


def _words_returned(bench, cell) -> float:
    ref = sum(len(bench.light_norm(r["ref"]).split()) for r in cell["records"])
    hyp = sum(len(bench.light_norm(r["hyp"]).split()) for r in cell["records"])
    return hyp / ref if ref else 0.0


@pytest.mark.slow
@_corpus_missing
def test_a_long_form_fixture_returns_almost_all_of_its_words(bench, shipped_params):
    """The defect, as a test. On the OLD default this returned 94.4% on base
    and 66.1% on small; the gate is 95%."""
    clips = bench.load_libri_long(LIBRI, 5.0, 1, LONG_CACHE)
    if not clips:
        pytest.skip("no chapter long enough for a 5-minute clip")
    cell = bench.run_cell(clips, "base", "cuda", shipped_params, "en", bench.light_norm)

    returned = _words_returned(bench, cell)
    assert returned >= MIN_WORDS_RETURNED, (
        f"the shipped default returned {returned:.1%} of the reference words "
        f"on {cell['audio_seconds']:.0f}s of continuous speech")
    assert cell["wer_light"] <= MAX_LONG_WER, cell["wer_light"]


@pytest.mark.slow
@_corpus_missing
def test_short_clip_accuracy_does_not_regress(bench, shipped_params):
    """The cost side of the trade. Measured 3.91% against the old default's
    3.61%; the ceiling allows that 0.30-point cost and no more."""
    clips = bench.load_libri_short(LIBRI, 60)
    cell = bench.run_cell(clips, "base", "cuda", shipped_params, "en", bench.light_norm)

    assert cell["wer_light"] <= MAX_SHORT_WER, (
        f"short-clip WER {cell['wer_light']:.2%} is past the {MAX_SHORT_WER:.1%} ceiling")
    assert _words_returned(bench, cell) >= MIN_WORDS_RETURNED
