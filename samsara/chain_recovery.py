"""Recover a short command-lane join only at complete, unambiguous boundaries."""
from samsara import grammar

JOIN_WORDS = frozenset({'and', 'then', 'in', 'an', 'end', 'n', 'than'})


def _has_text_slot(parsed, by_id):
    return any(a.get('type', 'text') == 'text' and a['name'] in values
               for cid, values, _span, _confidence in parsed for a in by_id[cid].get('args', ()))


def parse_command_chain(text, records):
    """Called only from the executor's explicit command-lane precheck.

    Never split a free-text slot. A recovered application argument must be
    an existing named-app alias, not arbitrary words that merely fit app_name.
    The original parser and original token offsets remain authoritative.
    """
    original = grammar.parse(text, records)
    if len(original) > 1:
        return original
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
