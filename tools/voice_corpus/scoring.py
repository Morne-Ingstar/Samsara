"""Levenshtein WER with numeric-only results; decoder text is never persisted."""
from collections import Counter, defaultdict
import re


def words(text):
    # Historical strict tokenisation, retained for comparison with old runs.
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text.casefold(), flags=re.UNICODE)


_SMALL = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINALS = dict(zip(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth hundredth thousandth millionth billionth".split(),
    _SMALL[1:] + _TENS[2:] + ["hundred", "thousand", "million", "billion"]))
_PUNCTUATION = re.compile(
    r"\b(?:exclamation (?:mark|point)|question mark|full stop|new (?:line|paragraph)|"
    r"(?:open|close) (?:quote|parenthesis|bracket)|quotation marks?|"
    r"comma|period|semicolon|colon|hyphen|dash)\b")


def _integer_words(value):
    if value < 20:
        return [_SMALL[value]]
    if value < 100:
        return [_TENS[value // 10]] + (_integer_words(value % 10) if value % 10 else [])
    for scale, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand"), (100, "hundred")):
        if value >= scale:
            return _integer_words(value // scale) + [name] + (_integer_words(value % scale) if value % scale else [])


def normalised_words(text, tags=()):
    """English scoring policy; punctuation-tagged lines retain historical scoring."""
    if "punctuation" in tags:
        return words(text)
    text = text.casefold()
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"\d+(?:\.\d+)?(?:(?:st|nd|rd|th)\b)?", lambda m: " " + " ".join(
        _integer_words(int(re.sub(r"(?:st|nd|rd|th)$", "", m[0]).split(".")[0])) +
        (["point"] + [_SMALL[int(c)] for c in m[0].split(".")[1]] if "." in m[0] else [])) + " ", text)
    text = _PUNCTUATION.sub(" ", text)
    tokens = [_ORDINALS.get(token, token) for token in words(text)]
    # British number conjunction: one hundred and four == 104.
    return [token for i, token in enumerate(tokens)
            if not (token == "and" and i and tokens[i-1] in {"hundred", "thousand", "million", "billion"}
                    and i + 1 < len(tokens) and tokens[i+1] in _SMALL + _TENS)]


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


def _measure(a, b):
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
                )


def measure(reference, hypothesis, tags=()):
    """Return numeric-only normalised and historical strict metrics; never text."""
    a, b = normalised_words(reference, tags), normalised_words(hypothesis, tags)
    result = _measure(a, b)
    overlap = sum((Counter(a) & Counter(b)).values()) / len(a) if a else 1.0
    return dict(result, exact_text=reference.strip() == hypothesis.strip(),
                strict=_measure(words(reference), words(hypothesis)),
                off_script=bool(a) and overlap < .5, reference_overlap=overlap,
                scoring_policy="normalised-v1", punctuation_strict="punctuation" in tags)


def aggregate(rows):
    keys = ("reference_words", "hypothesis_words", "substitutions", "deletions", "insertions",
            "missing_start_words", "missing_end_words", "doubled_words")
    counts = Counter(dict.fromkeys(keys, 0))
    included = [row for row in rows if not row.get("off_script", False)]
    for row in included:
        for key in keys:
            counts[key] += row[key]
    counts["wer"] = (counts["substitutions"] + counts["deletions"] + counts["insertions"]) / max(1, counts["reference_words"])
    counts["takes"] = len(rows)
    counts["scored_takes"] = len(included)
    counts["off_script_takes"] = len(rows) - len(included)
    strict = [row.get("strict", row) for row in included]
    strict_errors = sum(r["substitutions"] + r["deletions"] + r["insertions"] for r in strict)
    counts["strict_reference_words"] = sum(r["reference_words"] for r in strict)
    counts["strict_wer"] = strict_errors / max(1, counts["strict_reference_words"])
    counts["strict_errors"] = strict_errors
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
    policy = "normalised-v1" if all(r.get("scoring_policy") == "normalised-v1" for r in rows) else "legacy-or-mixed; not fully normalised"
    summary = (f"{len(rows)} takes: {total['wer']:.1%} word error rate "
               f"({policy}; strict {total['strict_wer']:.1%}; {total['off_script_takes']} off_script excluded), "
               f"{total['insertions']} stray words, {total['missing_end_words']} missing ending words, "
               f"{total['command_failures']} routing failures. "
               f"Slowest release to fake text: {total['slowest_release_to_text_ms']:.0f} ms.")
    return dict(run, summary=summary, totals=total,
                off_script=[dict(id=r["id"], line_id=r["line_id"], reference_overlap=r["reference_overlap"])
                            for r in rows if r.get("off_script")],
                tags={tag: aggregate(items) for tag, items in sorted(tags.items())})


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
                  if not (row.get("off_script") or prior.get("off_script")) and row[key] > prior[key]]
        if row.get("off_script", False) != prior.get("off_script", False):
            fields.append("off_script")
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
