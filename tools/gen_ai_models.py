"""Generate the offline Ava catalogue from a downloaded OpenRouter JSON file.

Usage: python tools/gen_ai_models.py path/to/openrouter_models_YYYYMMDD.json
No network or application imports. Output is importable Python data so the
existing freezer includes it without a separate resource-copy rule.

Mapping rules:
* OpenRouter keeps every unsuffixed id and its `:free` variants, plus Auto.
* Direct providers keep text-output chat models from their vendor namespace.
  Image/audio generators, embeddings, moderation and legacy completions are out.
* OpenAI/DeepSeek native ids strip the vendor prefix; Anthropic also changes
  decimal version dots to dashes. These are the brief's catalogue mapping rules,
  not an assertion that every historical id is still served by a native API.
* Dates, preview, exp and vision-exp identify variants of the same family.
  Prefer an undated stable entry; otherwise retain only the newest variant,
  with its actual id (never invent an unavailable stable alias).
* Alphabetical display order; a provider's recommended model is pinned first.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re


DEFAULTS = {
    'deepseek': 'deepseek-v4.1-flash',
    'openai': 'gpt-6-luna',
    'anthropic': 'claude-haiku-5-5',
    'openrouter': 'openai/gpt-6-luna',
}
_DATE = re.compile(r'-(?:20\d{2}-\d{2}-\d{2}|20\d{6}|\d{4}|\d{2}-\d{2})(?=-|$)')
_VARIANT = re.compile(r'-(?:vision-exp|preview|experimental|exp)(?=-|$)')
_NON_CHAT = re.compile(r'(?:^|-)(?:image|audio|realtime|embedding|moderation|safeguard|instruct)(?:-|$)')


def native_id(provider, model_id):
    prefix = provider + '/'
    if not model_id.startswith(prefix):
        raise ValueError('model does not belong to provider')
    result = model_id[len(prefix):]
    return re.sub(r'(?<=\d)\.(?=\d)', '-', result) if provider == 'anthropic' else result


def variant_family(model_id):
    return _VARIANT.sub('', _DATE.sub('', model_id))


def is_chat(row):
    name = row['id'].split('/', 1)[1]
    output = row.get('architecture', {}).get('output_modalities')
    return not _NON_CHAT.search(name) and (output is None or output == ['text'])


def price(value):
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def model_record(row, provider):
    prices = {key: price(row.get('pricing', {}).get(key)) for key in ('prompt', 'completion')}
    known = all(value is not None for value in prices.values())
    parameters = row.get('supported_parameters', [])
    return {
        'id': row['id'] if provider == 'openrouter' else native_id(provider, row['id']),
        'display_name': row.get('name') or row['id'],
        'free': all(Decimal(value) == 0 for value in prices.values()) if known else None,
        'tools': 'tools' in parameters,
        'json': bool({'response_format', 'structured_outputs'} & set(parameters)),
        'context': row.get('context_length'),
        'created': row.get('created', 0),
        'pricing': prices,
    }


def sort_key(row):
    return (row['display_name'].casefold(), row['id'].casefold())


def build_snapshot(payload):
    # Resolve duplicate identical ids deterministically, independent of input order.
    source = {}
    for row in sorted(payload['data'], key=lambda item: json.dumps(item, sort_keys=True)):
        model_id = row['id']
        if ':' not in model_id or model_id.endswith(':free'):
            source[row['id']] = row
    snapshot = {}
    for provider, default in DEFAULTS.items():
        if provider == 'openrouter':
            selected = [row for row in source.values() if is_chat(row)]
        else:
            families = {}
            for row in source.values():
                if row['id'].startswith(provider + '/') and is_chat(row):
                    families.setdefault(variant_family(row['id']), []).append(row)
            selected = []
            for family, rows in families.items():
                stable = next((row for row in rows if row['id'] == family), None)
                selected.append(stable or max(rows, key=lambda row: (row.get('created', 0), row['id'])))
        models = sorted((model_record(row, provider) for row in selected), key=sort_key)
        if provider == 'openrouter' and not any(r['id'] == 'openrouter/auto' for r in models):
            models.append({'id': 'openrouter/auto', 'display_name': 'Auto (OpenRouter chooses)',
                           'free': None, 'tools': False, 'json': False, 'context': None,
                           'created': 0, 'pricing': {'prompt': None, 'completion': None}})
        if provider == 'openrouter':
            next(row for row in models if row['id'] == 'openrouter/auto')['display_name'] = 'Auto (OpenRouter chooses)'
        recommended = next((row for row in models if row['id'] == default), None)
        if recommended is not None:
            models.remove(recommended)
            models.insert(0, recommended)
        snapshot[provider] = {'default': default, 'models': models}
    return snapshot


def render_module(payload, source_name, source_sha256):
    snapshot = build_snapshot(payload)
    for provider, entry in snapshot.items():
        chosen = next((row for row in entry['models'] if row['id'] == entry['default']), None)
        if not chosen or not chosen['tools']:
            raise ValueError(f'{provider}: recommended tool-capable model missing; review DEFAULTS')
    metadata = {'source': source_name, 'sha256': source_sha256, 'input_count': len(payload['data'])}
    # Keep generated source compact: one complete model record per physical line.
    # repr preserves the insertion order that defines the public snapshot data.
    lines = [
        '"""Generated by tools/gen_ai_models.py; do not edit by hand."""',
        '',
        'SOURCE = ' + repr(metadata),
        '',
        'SNAPSHOT = {',
    ]
    for provider, entry in snapshot.items():
        lines.append(f'    {provider!r}: {{')
        lines.append(f"        'default': {entry['default']!r},")
        lines.append("        'models': [")
        lines.extend('            ' + repr(model) + ',' for model in entry['models'])
        lines.append('        ],')
        lines.append('    },')
    lines.extend(['}', ''])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalogue', type=Path)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'samsara' / 'data' / 'ai_models.py')
    args = parser.parse_args(argv)
    raw = args.catalogue.read_bytes()
    result = render_module(json.loads(raw), args.catalogue.name, hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding='utf-8', newline='\n')
    print(f'Generated {args.output}')


if __name__ == '__main__':
    main()
