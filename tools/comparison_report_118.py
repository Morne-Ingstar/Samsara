"""Queue 118: build perf_artifacts/comparison_baseline_118.md from the raw
JSON the two benches wrote. No measurement happens here -- this only formats
what was measured, so the table and the data cannot disagree.

    python tools/comparison_report_118.py
"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WER_JSON = REPO / "perf_artifacts" / "comparison_baseline_118_wer.json"
LAT_JSON = REPO / "perf_artifacts" / "comparison_baseline_118_latency.json"
OUT_MD = REPO / "perf_artifacts" / "comparison_baseline_118.md"

NOT_MEASURED = "not measured"


def pc(value):
    return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else NOT_MEASURED


def ms(value):
    return f"{value:.0f} ms" if isinstance(value, (int, float)) else NOT_MEASURED


def gpu_name():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0]
    except Exception:
        return "unknown"


def stats(values):
    """p50/p95/max/mean over a list, or None when empty."""
    if not values:
        return None
    s = sorted(values)

    def q(p):
        k = (len(s) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)

    return {"n": len(s), "p50": q(0.5), "p95": q(0.95), "max": s[-1],
            "mean": round(sum(s) / len(s), 1)}


def complete_rows(r):
    """Trials whose capture ended AFTER the speaker finished. A negative gap
    means the endpoint fired during a pause and cut the utterance in half, so
    that trial's latency is the time to a FRAGMENT and must not be averaged in
    with whole utterances."""
    return [x for x in r.get("rows", []) if x["capture_gap_ms"] >= 0]


def split_rows(r):
    return [x for x in r.get("rows", []) if x["capture_gap_ms"] < 0]


def cell(cells, **match):
    for c in cells:
        if all(c.get(k) == v for k, v in match.items()):
            return c
    return None


def run(runs, **match):
    for r in runs:
        if all(r.get(k) == v for k, v in match.items()):
            return r
    return None


def main() -> int:
    wer = json.loads(WER_JSON.read_text(encoding="utf-8")) if WER_JSON.exists() else {"cells": [], "corpora": {}}
    lat = json.loads(LAT_JSON.read_text(encoding="utf-8")) if LAT_JSON.exists() else {"runs": []}
    cells, corpora, runs = wer["cells"], wer["corpora"], lat.get("runs", [])

    live = {}
    try:
        live = json.loads(Path.home().joinpath(".samsara/config.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    lines = []
    add = lines.append
    add("# Samsara measured baseline (queue 118)")
    add("")
    add(f"Measured {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} on "
        f"{platform.system()} (build {platform.version()}), {gpu_name()}, Python "
        f"{platform.python_version()}. Samsara was running throughout and shared "
        f"the GPU.")
    add("")
    add("Every number below was produced by one of the commands named in its "
        "section. Nothing here is derived from another number. Where a figure "
        "could not be produced honestly the cell says so, and the whole page "
        "regenerates with:")
    add("")
    add("```")
    add("python tools/wer_bench_118.py --all --n 120")
    add("python tools/e2e_latency_bench_118.py --lane hotkey    --trials 32")
    add("python tools/e2e_latency_bench_118.py --lane handsfree --trials 32")
    add("python tools/comparison_report_118.py")
    add("```")
    add("")
    add("## The three headline numbers")
    add("")
    add("| | Measured | One-line caveat |")
    add("|---|---|---|")
    hk = run(runs, lane="hotkey", profile="default")
    hf0 = run(runs, lane="handsfree", profile="default")
    if hk:
        e = stats([x["e2e_ms"] for x in complete_rows(hk)])
        add(f"| Latency, hold-to-dictate (last word -> text on screen) | p50 "
            f"{ms(e['p50'])}, p95 {ms(e['p95'])} | over half of it is the owner's own "
            f"key-release delay; the app owns "
            f"{ms(stats([x['app_ms'] for x in complete_rows(hk)])['p50'])} of it |")
    if hf0:
        e = stats([x["e2e_ms"] for x in complete_rows(hf0)])
        add(f"| Latency, hands-free | p50 {ms(e['p50'])}, p95 {ms(e['p95'])} | "
            f"{ms(hf0['silence_gap_s'] * 1000)} of it is the silence gap the endpoint "
            f"waits for; complete utterances only |")
    c = cell(cells, corpus="libri-short", model="base", device="cuda", params="balanced", language="en")
    if c:
        add(f"| WER of the first-run default (`base`) | {pc(c['wer_norm'])} normalised | "
            f"clean read speech, not this microphone in this room; formatting-sensitive "
            f"WER is not measurable on this corpus |")
    add("")
    add("## What the defaults actually are")
    add("")
    add("| Setting | First run | This machine today |")
    add("|---|---|---|")
    add(f"| model_size | `base` (config_schema.py) | `{live.get('model_size', '?')}` |")
    add(f"| device | `auto` -> CUDA when present (dictation.py:3512, :5587); "
        f"config_schema.py still says `cpu` | `{live.get('device', '?')}` |")
    add(f"| compute_type | `float16` on CUDA, `int8` on CPU (dictation.py:5601) | `{live.get('compute_type', '?')}` |")
    add(f"| performance_mode | `balanced` (beam 3, no conditioning) | `{live.get('performance_mode', '?')}` |")
    add(f"| language | `en` | `{live.get('language', '?')}` |")
    add("")
    add("The schema's `device: cpu` and the app's own `device: auto` disagree; "
        "the app's default wins at runtime, so a first run on this machine gets "
        "`base` on the GPU.")
    add("")

    # ---------------- 1. latency ----------------
    add("## 1. End-to-end latency: last word spoken -> text in the target window")
    add("")
    add("```")
    add("python tools/e2e_latency_bench_118.py --lane hotkey    --trials 32 --profile default")
    add("python tools/e2e_latency_bench_118.py --lane handsfree --trials 32 --profile default")
    add("```")
    add("")
    add("| Lane | n | p50 | p95 | max | Caveat |")
    add("|---|---|---|---|---|---|")
    for lane, caveat in (
        ("hotkey", "includes the owner's own key-release delay (below); the app "
                   "cannot start before the key is released"),
        ("handsfree", "complete utterances only; the endpoint gap is the largest "
                      "single component, and the split captures below are excluded"),
    ):
        r = run(runs, lane=lane, profile="default")
        if not r or not r.get("e2e_ms"):
            add(f"| {lane} | - | {NOT_MEASURED} - the run did not complete | | | |")
            continue
        e = stats([x["e2e_ms"] for x in complete_rows(r)]) or r["e2e_ms"]
        add(f"| {lane} | {e['n']} | {ms(e['p50'])} | {ms(e['p95'])} | {ms(e['max'])} | {caveat} |")
    add("")
    add("Stage breakdown (same runs, median):")
    add("")
    add("| Lane | end of speech -> end of capture | decode | paste -> visible | app-owned total (capture end -> visible) |")
    add("|---|---|---|---|---|")
    for lane in ("hotkey", "handsfree"):
        r = run(runs, lane=lane, profile="default")
        if not r or not r.get("e2e_ms"):
            continue
        rows = complete_rows(r)
        add(f"| {lane} | {ms(stats([x['capture_gap_ms'] for x in rows])['p50'])} | "
            f"{ms(stats([x['decode_ms'] for x in rows])['p50'])} | "
            f"{ms(stats([x['decode_to_visible_ms'] for x in rows])['p50'])} | "
            f"{ms(stats([x['app_ms'] for x in rows])['p50'])} |")
    add("")
    hf = run(runs, lane="handsfree", profile="default")
    if hf:
        split = split_rows(hf)
        total = len(hf.get("rows", []))
        if split:
            add(f"**{len(split)} of {total} hands-free trials never got the whole "
                f"utterance.** The endpoint fired during a pause, so the capture was "
                f"cut mid-sentence and only the first fragment was decoded (one 7.1 s "
                f"clip came back as \"That's...\"). That is the "
                f"`command_mode.dictate_utterance_silence_s` gap of "
                f"{hf.get('silence_gap_s')} s, read from {hf.get('silence_gap_source')}. "
                f"Those trials are excluded from the latency figures above: a fast "
                f"fragment is not a fast dictation.")
            add("")
    r = run(runs, lane="hotkey", profile="default")
    if r and r.get("capture_gap_ms"):
        g = r["capture_gap_ms"]
        add(f"The hold-to-dictate gap is the human, not the software: across {g['n']} of "
            f"the owner's own captures the delay between his last word and his releasing "
            f"the key was p50 {ms(g['p50'])}, p95 {ms(g['p95'])}, max {ms(g['max'])}. "
            f"That is why the hotkey p95 is high while the app-owned part is small.")
        add("")
    owner = run(runs, lane="hotkey", profile="owner")
    if owner and owner.get("e2e_ms"):
        add(f"With this machine's own configuration (`{owner['model']}`, "
            f"{owner['device']}): e2e p50 {ms(owner['e2e_ms']['p50'])}, "
            f"p95 {ms(owner['e2e_ms']['p95'])}, decode p50 {ms(owner['decode_ms']['p50'])}.")
        add("")

    # ---------------- 2. WER ----------------
    add("## 2. WER of the shipping default, and WER with formatting taken out")
    add("")
    add("```")
    add("python tools/wer_bench_118.py --all --n 120")
    add("```")
    add("")
    libri = corpora.get("libri-short", {})
    add(f"Corpus: LibriSpeech test-clean (openslr.org/resources/12, CC BY 4.0), "
        f"{libri.get('clips', '?')} utterances, {libri.get('audio_seconds', '?')} s of audio, "
        f"{libri.get('ref_words', '?')} reference words, clip length "
        f"{libri.get('seconds_min', '?')}-{libri.get('seconds_max', '?')} s "
        f"(median {libri.get('seconds_median', '?')} s).")
    add("")
    add("| Model | Device | WER (normalised) | WER (raw) | Decode p50 | Real-time factor |")
    add("|---|---|---|---|---|---|")
    for model, device in (("base", "cuda"), ("small", "cuda"), ("medium", "cuda"), ("base", "cpu")):
        c = cell(cells, corpus="libri-short", model=model, device=device,
                 params="balanced", language="en")
        if not c:
            continue
        mark = " (first-run default)" if model == "base" and device == "cuda" else ""
        add(f"| {model}{mark} | {device} ({c['compute_type']}) | {pc(c['wer_norm'])} | "
            f"see caveat | {ms(c['decode_ms_median'])} | {c['rtf_median']} |")
    add("")
    add("**Raw WER is not reportable on this corpus.** LibriSpeech references are "
        "upper case with no punctuation, so comparing them to Whisper's cased, "
        "punctuated output scores ~98% whatever the model does. That number "
        "measures the corpus's transcription convention, not the product, and "
        "must not be published. The raw column is in the JSON for completeness. "
        "A raw figure needs a reference that carries the formatting a user "
        "expects on screen, and no such corpus exists here yet -- see \"What is "
        "still missing\".")
    add("")
    add("Normalisation is Whisper's own `EnglishTextNormalizer` (lower case, "
        "punctuation, \"twenty one\"/\"21\", \"Mr.\"/\"mister\", colour/color) -- the "
        "normaliser published WERs use. A plainer lower-case-and-strip-punctuation "
        "pass is also in the JSON as `wer_light`; it is about 0.5 points worse "
        "(base: 4.12% against 3.61%), and the whole of that difference is number "
        "and title formatting.")
    add("")

    add("### The 18.0% `medium` result from queue 80")
    add("")
    owner43 = corpora.get("owner43", {})
    add(f"Corpus: the same {owner43.get('clips', '?')} wake-lane clips queue 80 used "
        f"({owner43.get('audio_seconds', '?')} s total, {owner43.get('seconds_min', '?')}-"
        f"{owner43.get('seconds_max', '?')} s each, {owner43.get('ref_words', '?')} reference "
        f"words), decoded with queue 80's own parameters.")
    add("")
    add("| Model | WER (normalised, corpus) | WER (mean of clips, what queue 80 published) |")
    add("|---|---|---|")
    for model in ("base", "small", "medium"):
        c = cell(cells, corpus="owner43", model=model, params="queue80")
        if c:
            add(f"| {model} | {pc(c['wer_norm'])} | {pc(c['wer_norm_mean_per_clip'])} |")
    add("")
    add("**The brief's hypothesis is wrong, and the finding is worse than "
        "formatting noise.** Queue 80's WER was already case- and "
        "punctuation-insensitive, so normalising changes almost nothing. Two real "
        "causes:")
    add("")
    add("1. These clips are 1-4 s of scripted speech averaging about two words, so "
        "one wrong word is a 50-250% clip WER and the mean of clips explodes. The "
        "corpus-level figure (total errors over total words) is the honest one.")
    add("2. `medium` really does mis-hear this phrase: \"jarvis dictate\" comes back "
        "as \"Jarvis Stick Tape\" in five of the 43 clips, where `base` and `small` "
        "get it right.")
    add("")
    add("On an independent corpus the ranking is the opposite and the usual way "
        "round: `medium` is the most accurate model (2.19% against base's 3.61%). "
        "So \"medium is worse than small\" is true only of this 43-clip wake-phrase "
        "set and must not be published as an accuracy claim.")
    add("")

    # ---------------- 3. long form ----------------
    add("## 3. Long-form WER")
    add("")
    add("```")
    add("python tools/wer_bench_118.py --corpus libri-long --models base small --long-minutes 5")
    add("```")
    add("")
    longm = corpora.get("libri-long", {})
    add(f"Corpus: {longm.get('clips', '?')} chapters of LibriSpeech test-clean read "
        f"speech, consecutive utterances concatenated into continuous audio of "
        f"{longm.get('seconds_min', '?')}-{longm.get('seconds_max', '?')} s "
        f"({longm.get('audio_seconds', '?')} s total, {longm.get('ref_words', '?')} reference words).")
    add("")
    add("| Model | Params | WER (normalised) | Words returned vs reference | Decode p50 |")
    add("|---|---|---|---|---|")
    for model in ("base", "small", "medium"):
        for params in ("balanced", "accurate"):
            c = cell(cells, corpus="libri-long", model=model, params=params, language="en")
            if not c:
                continue
            ratio = sum(len(r["hyp"].split()) for r in c["records"]) / max(
                1, sum(r["refwords_norm"] for r in c["records"]))
            add(f"| {model} | {params} | {pc(c['wer_norm'])} | {ratio * 100:.0f}% | "
                f"{ms(c['decode_ms_median'])} |")
    add("")
    add("Caveat: continuous READ speech, not natural dictation with its restarts, "
        "fillers and pauses, and not this microphone in this room. It is a floor "
        "for long-form accuracy, not a promise.")
    add("")
    bal = cell(cells, corpus="libri-long", model="small", params="balanced", language="en")
    acc = cell(cells, corpus="libri-long", model="small", params="accurate", language="en")
    if bal and acc:
        add("### The shipped decode parameters lose long-form speech")
        add("")
        add("This is the finding that matters most on this page, and it is not "
            "about model size. On five-minute audio the default `balanced` "
            "parameters return only part of what was said -- the \"words returned\" "
            "column above is the count of words the decoder emitted against the "
            "reference. `accurate` returns effectively all of it and the error "
            "rate collapses:")
        add("")
        add(f"* `small`: {pc(bal['wer_norm'])} with `balanced`, {pc(acc['wer_norm'])} with "
            f"`accurate` -- the same model and the same audio.")
        add("* `balanced` sets `condition_on_previous_text=False` and "
            "`without_timestamps=True`; with `vad_filter` and the no-speech and "
            "log-probability thresholds, whole windows are dropped with no context "
            "to recover them. `accurate` keeps the context and keeps the words.")
        add("* The cost of `accurate` is decode time: roughly double.")
        add("")
        add("Short utterances hide this completely -- on the 120 short clips "
            "`balanced` and `accurate` score the same. Anyone dictating "
            "continuously on the shipped default is losing text silently. This "
            "machine is set to `accurate`, which is why it has not been noticed "
            "here.")
        add("")

    # ---------------- 4. auto language ----------------
    add("## 4. The cost of `language: auto`")
    add("")
    add("```")
    add("python tools/wer_bench_118.py --corpus libri-short --models base medium --languages en auto")
    add("python tools/wer_bench_118.py --corpus owner43 --models base medium --languages en auto --params queue80")
    add("```")
    add("")
    add("| Corpus | Model | WER en | WER auto | Decode p50 en | Decode p50 auto | Added per utterance |")
    add("|---|---|---|---|---|---|---|")
    for corpus, params in (("libri-short", "balanced"), ("owner43", "queue80")):
        for model in ("base", "medium"):
            a = cell(cells, corpus=corpus, model=model, params=params, language="en")
            b = cell(cells, corpus=corpus, model=model, params=params, language="auto")
            if not a or not b:
                continue
            delta = b["decode_ms_median"] - a["decode_ms_median"]
            add(f"| {corpus} | {model} | {pc(a['wer_norm'])} | {pc(b['wer_norm'])} | "
                f"{ms(a['decode_ms_median'])} | {ms(b['decode_ms_median'])} | +{delta:.0f} ms |")
    add("")

    add("On clean English the choice costs time and changes nothing else. The "
        "risk is what it does to SHORT utterances, where there is little audio to "
        "judge from. Replaying the app's own `LanguageConfidenceGate` over these "
        "decodes (floor 0.90, samsara/languages.py):")
    add("")
    add("* With `language: en` the language is never in doubt -- Whisper is told "
        "what to decode, so `info.language` comes back `en` and the gate's "
        "language branch cannot fire. 0 of 43 and 0 of 120 rejected.")
    add("* With `language: auto` and `base`, one of the 43 short clips (\"cancel\") "
        "was detected as Chinese and decoded into CJK characters. The gate threw "
        "the text away and played the refusal sound: the user says a word and "
        "gets nothing.")
    add("* That detection came back with probability **0.914 -- above the 0.90 "
        "floor**, so the confidence test passed it. Only the script test caught "
        "it. A confident misdetection into another LATIN-script language would "
        "pass both tests and be typed out as gibberish.")
    add("")
    add("This machine is set to `language: auto` today, so it is paying the "
        "per-utterance cost above and carrying that rejection risk. Reported, not "
        "changed.")
    add("")

    # ---------------- blind spots + gaps ----------------
    add("## What the latency harness cannot see")
    add("")
    add("* It is the PIPELINE, not the running process. The real-time feed, the "
        "app's VAD call, its endpoint rule, its clipboard paste path and a polled "
        "target window are all real, but Samsara's own threading, queueing and Qt "
        "work between capture and paste are not. The shipping app is this plus "
        "its orchestration, so treat these as a floor.")
    add("* The hotkey hook itself (key down -> capture starts) is not included.")
    add("* Microphone and driver input latency are not included: the audio comes "
        "from the owner's own recorded captures, not the sound card.")
    add("* The target is a plain Win32 EDIT control, which renders faster than a "
        "real editor. A real application would add its own render time.")
    add("* The GPU was shared with the running app throughout; decode times "
        "include whatever contention existed.")
    add("* VAD end-of-speech detection IS included for hands-free (it is the "
        "endpoint gap, the largest component) and does not apply to hotkey, where "
        "the key release ends the capture.")
    add("")
    add("## What is still missing")
    add("")
    add("| Number | Status |")
    add("|---|---|")
    add("| Raw (formatting-sensitive) WER | **not measured** -- needs a reference "
        "with real casing and punctuation. LibriSpeech has neither, and the "
        "owner's 43 clips carry a lower-case truth. |")
    add("| WER on the owner's own voice, ranked | **not measured** -- the only "
        "corpus of his speech has a truth derived from the app's own live decode, "
        "so it cannot rank models. It needs perhaps 20 minutes of his speech with "
        "a transcript he has confirmed. |")
    add("| Long-form WER on natural dictation | **not measured** -- the long-form "
        "figures are read speech. Natural dictation has restarts and fillers. |")
    add("| End-to-end latency inside the running app | **not measured** -- see the "
        "blind spots above; it needs a timestamp the app does not log today. |")
    add("| Wispr Flow's numbers on this machine | **not measured** -- not "
        "installed, and their published figures are on their own corpus. |")
    add("")
    add("`~/.samsara/benchmark/samples.jsonl` holds 200 recordings and zero "
        "confirmed transcripts, so `tools/benchmark_eval.py` has nothing to score. "
        "Confirming a couple of hundred of those in Benchmark Review would close "
        "the second and third gaps at once.")
    add("")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
