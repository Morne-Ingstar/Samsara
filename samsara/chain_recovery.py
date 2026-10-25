"""Recover a short command-lane join only at complete, unambiguous boundaries."""
from samsara import grammar
from functools import lru_cache

JOIN_WORDS = frozenset({'and', 'then', 'in', 'an', 'end', 'n', 'than'})


def _has_text_slot(parsed, by_id):
    return any(a.get('type', 'text') == 'text' and a['name'] in values
               for cid, values, _span, _confidence in parsed for a in by_id[cid].get('args', ()))


def _explicit_chain(text, records):
    """806: cut arguments only where a complete known command follows.

    A joiner inside an app/control name remains content unless its suffix
    parses completely. Exact catalog aliases containing 'and' stay intact.
    Original token spans are returned so execution keeps argument spelling.
    """
    words = grammar._tokens(text)
    if not records or not any(word in {'and', 'then'} for word in words):
        return []
    index = grammar._catalog_index(records, records)

    @lru_cache(None)
    def one(start, end):
        part = words[start:end]
        parsed = grammar._parse_one(part, index, start)
        if parsed:
            return parsed
        polite = 1 if part[:1] == ['please'] else 0
        part = part[polite:]
        candidates = []
        for record, alias, order in index.get(part[0] if part else '', ()):
            args = record.get('args') or []
            if (len(args) != 1 or args[0].get('type', 'text') not in {'text', 'app_name'}
                    or part[:len(alias)] != alias):
                continue
            rest = part[len(alias):]
            if not rest or (args[0].get('type') == 'app_name' and len(rest) > 4):
                continue
            candidates.append((len(alias), -order,
                               (record['canonical_id'], {args[0]['name']: ' '.join(rest)},
                                (start, end), .97)))
        return [max(candidates, key=lambda item: item[:2])[2]] if candidates else []

    @lru_cache(None)
    def search(start):
        whole = one(start, len(words))
        # Named apps such as "open Salt and Pepper" are indivisible aliases.
        if whole and not next(r for r in records if r['canonical_id'] == whole[0][0]).get('args'):
            return whole
        for cut in range(start + 1, len(words) - 1):
            if words[cut] not in {'and', 'then'}:
                continue
            skip = 2 if words[cut:cut + 2] == ['and', 'then'] else 1
            if cut + skip >= len(words):
                continue
            # Avoid parsing arbitrary prose to the right of every 'and'.
            first = cut + skip
            if words[first] == 'please' and first + 1 < len(words):
                first += 1
            if words[first] not in index:
                continue
            left = one(start, cut)
            if left:
                right = search(cut + skip)
                if right:
                    return left + right
        return whole

    result = search(0)
    return result if len(result) > 1 else []


def parse_command_chain(text, records):
    """Called only from the executor's explicit command-lane precheck.

    Explicit joiners can end argument slots at complete command boundaries.
    A phonetically recovered application argument must still be a named alias.
    The original parser and original token offsets remain authoritative.
    """
    original = grammar.parse(text, records)
    if len(original) > 1:
        return original
    # 887: "cube tile three and four" is one complete numeric-list command.
    # Recovering another boundary would reinterpret its last number as a pick.
    if len(original) == 1:
        cid, values, _span, _confidence = original[0]
        record = next(r for r in records if r['canonical_id'] == cid)
        if any(arg.get('type') == 'int' and isinstance(values.get(arg['name']), list)
               for arg in record.get('args', ())):
            return original
    explicit = _explicit_chain(text, records)
    if explicit:
        return explicit
    words = grammar._tokens(text)
    if not (any(word in JOIN_WORDS - {'and', 'then'} for word in words)
            or any(words[i:i+2] == ['and', 'then'] for i in range(len(words)-1))):
        return original  # Keep ordinary and/then misses on the existing fast path.
    by_id = {r['canonical_id']: r for r in records}
    if _has_text_slot(original, by_id):
        return original
    index = grammar._catalog_index(records, records)
    named_apps = {
        ' '.join(grammar._tokens(alias)[1:])
        for r in records if not r.get('args')
        for alias in r.get('aliases', ())
        if grammar._tokens(alias)[:1] == ['open'] and len(grammar._tokens(alias)) > 1
    }

    def one(start, end):
        parsed = grammar._parse_one(words[start:end], index, start)
        if not parsed or _has_text_slot(parsed, by_id):
            return []
        cid, values, _span, _confidence = parsed[0]
        for arg in by_id[cid].get('args', ()):
            if arg.get('type') == 'app_name' and arg['name'] in values:
                if values[arg['name']].casefold() not in named_apps:
                    return []
        return parsed

    solutions = []

    def search(start, steps):
        if len(steps) > 4 or len(solutions) > 1:
            return
        tail = one(start, len(words))
        if tail and steps:
            solutions.append(steps + tail)
        for i in range(start+1, len(words)-1):
            if words[i] not in JOIN_WORDS:
                continue
            skip = 2 if words[i:i+2] == ['and', 'then'] else 1
            left = one(start, i)
            if left and i+skip < len(words):
                search(i+skip, steps+left)

    search(0, [])
    return solutions[0] if len(solutions) == 1 else original


def argument_reopens_rejected_prefix(parsed, by_id):
    """A stricter named command cannot fall back to open with ignored words."""
    return any(a.get('type', 'text') in {'text', 'app_name', 'monitor'} and a['name'] in values
               for cid, values, _span, _confidence in parsed
               for a in by_id[cid][0].get('args', ()))


def align_named_app_prefix(parsed, by_id, prefix):
    """Use the existing exact app launcher when it names the same first step."""
    if prefix is None or not parsed:
        return parsed
    cid, values, span, confidence = parsed[0]
    if prefix.phrase.casefold() != 'open ' + str(values.get('app_name', '')).casefold():
        return parsed
    exact = next((key for key, (record, phrase) in by_id.items()
                  if phrase == prefix.phrase and not record.get('args')), None)
    return [(exact, {}, span, confidence), *parsed[1:]] if exact else parsed


def align_exact_steps(text, parsed, by_id, matcher, context):
    """Use the same exact command alone and inside a chain (e.g. tab one).

    Only replace a generic argument reading when the registry recognizes the
    complete segment as an argument-free command. Conflicting fixed commands
    still reach the executor's original conflict check.
    """
    tokens = list(grammar._TOKEN_RE.finditer(text))
    aligned = []
    for cid, values, (start, end), confidence in parsed:
        if values:
            segment = text[tokens[start].start():tokens[end - 1].end()]
            entry, remainder = matcher.match(segment, context)
            if entry is not None and not remainder:
                exact = next((key for key, (record, phrase) in by_id.items()
                              if phrase == entry.phrase and not record.get('args')), None)
                if exact:
                    cid, values = exact, {}
        aligned.append((cid, values, (start, end), confidence))
    return aligned
