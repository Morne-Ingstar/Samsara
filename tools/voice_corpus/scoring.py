"""Levenshtein WER with numeric-only results; decoder text is never persisted."""
from collections import Counter, defaultdict
import re


def words(text):
    # Keep number spelling policy explicit: punctuation/case tolerant, digits
    # versus spoken numbers remain distinct (and visible as word errors).
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text.casefold(), flags=re.UNICODE)


def _repeated(tokens):
    repeated = 0
    index = 0
    while index < len(tokens):
        width = next((n for n in range(min(8, (len(tokens) - index) // 2), 0, -1)
                      if tokens[index:index+n] == tokens[index+n:index+2*n]), 0)
        if width:
            repeated += width
            index += width * 2
        else:
            index += 1
    return repeated


def measure(reference, hypothesis):
    a, b = words(reference), words(hypothesis)
    grid = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        grid[i][0] = i
    grid[0] = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        for j, y in enumerate(b, 1):
            grid[i][j] = min(grid[i-1][j-1] + (x != y), grid[i-1][j] + 1, grid[i][j-1] + 1)
    i, j = len(a), len(b)
    matched, missing = [], []
    substitutions = deletions = insertions = 0
    while i or j:
        if i and j and grid[i][j] == grid[i-1][j-1] + (a[i-1] != b[j-1]):
            if a[i-1] == b[j-1]:
                matched.append(i - 1)
            else:
                missing.append(i - 1)
                substitutions += 1
            i, j = i - 1, j - 1
        elif i and grid[i][j] == grid[i-1][j] + 1:
            missing.append(i - 1)
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    matched = sorted(matched)
    return dict(reference_words=len(a), hypothesis_words=len(b),
                substitutions=substitutions, deletions=deletions, insertions=insertions,
                wer=grid[len(a)][len(b)] / max(1, len(a)),
                missing_start_words=matched[0] if matched else len(a),
                missing_end_words=len(a) - 1 - matched[-1] if matched else len(a),
                doubled_words=min(insertions, max(0, _repeated(b) - _repeated(a))),
                exact_text=reference.strip() == hypothesis.strip())


def aggregate(rows):
    counts = Counter()
    for row in rows:
        for key in ("reference_words", "hypothesis_words", "substitutions", "deletions", "insertions",
                    "missing_start_words", "missing_end_words", "doubled_words"):
            counts[key] += row[key]
    counts["wer"] = (counts["substitutions"] + counts["deletions"] + counts["insertions"]) / max(1, counts["reference_words"])
    counts["takes"] = len(rows)
    counts["command_failures"] = sum(r.get("command_routed_correctly") is False for r in rows)
    counts["decode_failures"] = sum(bool(r.get("decode_failed")) for r in rows)
    times = sorted(r["release_to_text_ms"] for r in rows)
    counts["slowest_release_to_text_ms"] = max(times, default=0)
    counts["median_release_to_text_ms"] = times[len(times) // 2] if times else 0
    return dict(counts)


def score(run):
    rows = run["takes"]
    if not rows:
        raise ValueError("No takes to score")
    tags = defaultdict(list)
    for row in rows:
        for tag in row["tags"]:
            tags[tag].append(row)
    total = aggregate(rows)
    summary = (f"{len(rows)} takes: {total['wer']:.1%} word error rate, "
               f"{total['insertions']} stray words, {total['missing_end_words']} missing ending words, "
               f"{total['command_failures']} routing failures. "
               f"Slowest release to fake text: {total['slowest_release_to_text_ms']:.0f} ms.")
    return dict(run, summary=summary, totals=total, tags={tag: aggregate(items) for tag, items in sorted(tags.items())})


def compare(before, after, latency_ms=100):
    if before["corpus_sha256"] != after["corpus_sha256"]:
        raise ValueError("Corpus/audio/references changed; compare identical selected takes")
    if before["settings_sha256"] != after["settings_sha256"]:
        raise ValueError("Model or settings changed; use the same settings for a build comparison")
    old, new = ({r["id"]: r for r in run["takes"]} for run in (before, after))
    if old.keys() != new.keys():
        raise ValueError("Take selection changed")
    regressions = []
    for take_id, row in new.items():
        prior = old[take_id]
        fields = [key for key in ("wer", "insertions", "missing_start_words", "missing_end_words", "doubled_words")
                  if row[key] > prior[key]]
        if prior.get("command_routed_correctly") is True and row.get("command_routed_correctly") is not True:
            fields.append("command_routed_correctly")
        if not prior.get("decode_failed") and row.get("decode_failed"):
            fields.append("decode_failed")
        if row["release_to_text_ms"] > prior["release_to_text_ms"] + latency_ms:
            fields.append("release_to_text_ms")
        if fields:
            regressions.append(dict(id=take_id, line_id=row["line_id"], regressed=fields))
    return dict(summary=f"{len(regressions)} takes regressed from {before['build_id']} to {after['build_id']}.",
                regressions=regressions, latency_tolerance_ms=latency_ms)
