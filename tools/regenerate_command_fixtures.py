"""Regenerate portable command snapshots from shipped plugins (899).

Run with the test dependencies installed. The chain fixture uses its inert
handler rig: no command effects, personal plugins, application or models run.
Historical chain rows and collected test IDs are preserved.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(relative, value, *, sort_keys=False):
    (ROOT / relative).write_text(json.dumps(value, indent=2, sort_keys=sort_keys) + '\n',
                                 encoding='utf-8', newline='\n')


def main():
    import pytest
    from tools import gen_command_catalog, gen_intent_eval
    from samsara.intent.resolve import load_records
    from samsara.command_catalog import canonical_phrase
    from tests.test_several_commands_399 import rig, RETIRED_TEXT_SLOTS

    if gen_command_catalog.main([]):
        raise RuntimeError('catalog generation failed')
    records = load_records()
    write_json('tests/fixtures/426/registry_snapshot.json', records)
    catalog, reserved = gen_intent_eval.load_catalog()
    lines = gen_intent_eval.build_lines(catalog, reserved)
    (ROOT / 'tests/fixtures/intent_eval.jsonl').write_text(
        gen_intent_eval.render(lines), encoding='utf-8', newline='\n')
    rows = defaultdict(list)
    for line in lines:
        rows[line['id']].append(line)
    write_json('tests/fixtures/426/intent_eval_sha256.json', {
        cid: hashlib.sha256(gen_intent_eval.render(values).encode('utf-8')).hexdigest()
        for cid, values in rows.items()}, sort_keys=True)

    path = ROOT / 'tests/fixtures/several_commands_399_text_slots.json'
    slots = json.loads(path.read_text(encoding='utf-8'))
    added = []
    with pytest.MonkeyPatch.context() as patch:
        executor, app, calls = rig.__wrapped__(patch)
        for record in records:
            cid = record['canonical_id']
            if cid in slots or cid in RETIRED_TEXT_SLOTS or not any(
                    arg.get('type', 'text') == 'text' for arg in record.get('args', [])):
                continue
            text = canonical_phrase(record) + ' alpha and volume down'
            calls.clear()
            result = executor.process_text(text, force_commands=True)
            slots[cid] = {'text': text, 'state': result.state.value,
                          'calls': [list(call) for call in calls], 'detail': result.detail,
                          'generated_by': '890'}
            added.append(cid)
    write_json('tests/fixtures/several_commands_399_text_slots.json', slots, sort_keys=True)
    print(f'Regenerated six shipped snapshots; added {len(added)} text-slot cases.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
