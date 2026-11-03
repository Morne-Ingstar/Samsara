"""Live, read-only Ava discovery and request-pinned offers (1036 C1/C2).

No plugin loading, effect resolution, confirmation, or execution lives here.
The protocol consumer carries the CapabilityRequest with its queued response,
calls initial()/lookup(), then bind() before execute_canonical(). A successful
bind is an invocation to reauthorize, never permission to call a handler.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
import uuid

from . import execution_policy as policy

SCHEMA_VERSION = 1
NORMALIZER_VERSION = 1
PAGE_SIZE = 12
DESCRIPTOR_BUDGET = 6000
INDEX_BUDGET = 2000
FULL_DESCRIPTOR_BUDGET = 16000
LOOKUP_ROUNDS = 3
STOP_WORDS = frozenset('a an the please can could you me my for to of in is do'.split())
INFLECTIONS = {'commands': 'command', 'windows': 'window', 'editing': 'edit',
               'selected': 'selection', 'cleaning': 'cleanup', 'splitting': 'split'}
SYNONYMS = {'punctuation': 'cleanup', 'clean': 'cleanup', 'loudness': 'volume',
            'louder': 'volume', 'quieter': 'volume'}


def _describe_capability(command_id, *, executor, app=None, context=None):
    """Policy's discovery seam: explain without args, effects or approval."""
    entry = executor._registry_entry(command_id)
    if entry is None:
        return {'reason': 'unknown_command', 'schema': None, 'risk': policy.RISK_UNKNOWN}
    reason, detail = '', ''
    blocked = executor.command_availability(command_id, context)
    if blocked:
        reason, detail = blocked
    elif not executor._ai_visible(entry):
        reason = 'ai_hidden'
    runtime_registry = getattr(executor, '_plugin_registry', None)
    if (not reason and entry.source == 'plugin' and runtime_registry is not None
            and runtime_registry.get(command_id) is not policy._plugin_entry(command_id)):
        reason = 'policy_registry_mismatch'
    risk, reversible, schema = policy.classify(command_id, executor=executor, app=app,
                                              declared_only=True)
    if not reason and not policy.model_may_call(command_id, executor=executor, app=app, risk=risk):
        reason = 'not_allowed_for_model'
    return dict(reason=reason, detail=detail, schema=schema, risk=risk,
                reversible=reversible, approval_possible=risk not in (policy.RISK_READ, policy.RISK_UI),
                effect_hook='samsara.click_effect_policy.handling_for')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _normal(text):
    return ' '.join(re.findall(r'\w+', text.casefold(), re.UNICODE))


def _tokens(text):
    out = set()
    for word in _normal(text).split():
        if word in STOP_WORDS:
            continue
        word = INFLECTIONS.get(word, word)
        out.add(word)
        if word in SYNONYMS:
            out.add(SYNONYMS[word])
    return out


def schema_problem(schema):
    """Check the whole declared contract, including optional/unbound slots."""
    if schema is policy.NO_ARGS_SCHEMA:
        return ''
    if not isinstance(schema, dict) or not schema:
        return 'unvalidated'
    for name, spec in schema.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            return 'invalid_schema'
        typ = spec.get('type')
        if not isinstance(typ, str):
            return 'invalid_schema'
        if typ not in {'str', 'int', 'float', 'bool'}:
            return 'unsupported_argument_type'
        if set(spec) - {'type', 'required', 'min', 'max', 'max_len', 'choices', 'units', 'description'}:
            return 'unsupported_schema_constraint'
        if 'required' in spec and not isinstance(spec['required'], bool):
            return 'invalid_schema'
        if any(key in spec and not isinstance(spec[key], str) for key in ('units', 'description')):
            return 'invalid_schema'
        if 'max_len' in spec and (typ != 'str' or type(spec['max_len']) is not int or spec['max_len'] < 1):
            return 'invalid_schema'
        for bound in ('min', 'max'):
            if bound in spec and (typ not in {'int', 'float'} or not policy._type_ok(typ, spec[bound])
                                  or not math.isfinite(spec[bound])):
                return 'invalid_schema'
        if 'min' in spec and 'max' in spec and spec['min'] > spec['max']:
            return 'invalid_schema'
        if 'choices' in spec:
            choices = spec['choices']
            if not isinstance(choices, (list, tuple)) or not choices:
                return 'invalid_schema'
            for value in choices:
                if policy.validate_args({name: spec}, {name: value}, route=policy.Route.MODEL):
                    return 'invalid_schema'
    try:
        _json(schema)
    except (TypeError, ValueError):
        return 'invalid_schema'
    return ''


class CapabilityError(ValueError):
    """Machine-readable refusal; protocol copy is owned by the caller."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Descriptor:
    """Store JSON internally so a consumer cannot mutate a pinned contract."""
    id: str
    payload: str

    def record(self):
        return json.loads(self.payload)


class CapabilityIndex:
    def __init__(self, records=(), *, generation=0, identities=(), error=''):
        ordered = sorted(records, key=lambda r: r['id'])
        self.records = tuple(Descriptor(r['id'], _json(r)) for r in ordered)
        self.by_id = MappingProxyType({r.id: r for r in self.records})
        self.generation = generation
        self.error = error
        self.revision = _digest([SCHEMA_VERSION, NORMALIZER_VERSION, generation, identities, error, ordered])
        self.executor_ids = MappingProxyType({r['id']: r['canonical_phrase'] for r in ordered
                                             if r.get('provenance') == 'live_registry'})
        doc_map = defaultdict(list)
        for r in ordered:
            if r['documentation_id'] and r.get('provenance') == 'live_registry':
                doc_map[r['documentation_id']].append(r['canonical_phrase'])
        self.documentation_ids = MappingProxyType({k: v[0] for k, v in doc_map.items() if len(v) == 1})
        fields = ('names', 'description', 'category')
        self.postings = {f: defaultdict(set) for f in fields}
        self.exact = defaultdict(set)
        for record in ordered:
            cid = record['id']
            for name in [record['canonical_phrase'], *record['aliases']]:
                self.exact[_normal(name)].add(cid)
            texts = (record['canonical_phrase'] + ' ' + ' '.join(record['aliases']),
                     record['description'],
                     record['category'] + ' ' + ' '.join(record['argument_schema'] or {}))
            for field, value in zip(fields, texts):
                for token in _tokens(value):
                    self.postings[field][token].add(cid)
        counts = defaultdict(int)
        for record in ordered:
            counts[record['category']] += 1
        self.categories = tuple({'id': key, 'count': value} for key, value in sorted(counts.items()))

    @classmethod
    def from_executor(cls, executor, *, app=None, context=None, documentation=()):
        """Take one loaded matcher snapshot. A broken registry has no fallback."""
        from . import command_catalog, plugin_commands
        effective_app = app if app is not None else getattr(executor, '_app', None)
        try:
            matcher = executor._matcher
            if matcher is None:
                raise ValueError('missing matcher')
            if context is None:
                context = matcher.current_context()
            entries = {id(e): e for e in [*matcher._sorted, *matcher._entries.values()]}
            entries = sorted(entries.values(), key=lambda e: e.phrase)
            registry = getattr(executor, '_plugin_registry', None)
            registry = plugin_commands._REGISTRY if registry is None else registry
            rows = [dict(phrase=e.phrase, aliases=e.aliases, source=e.source,
                         description=e.description, pack=e.pack, param_schema=e.param_schema)
                    for e in entries]
            metadata = command_catalog.capability_metadata(rows, builtin_commands=executor.commands,
                                                           plugin_registry=registry)
            joined = defaultdict(list)
            for record in metadata:
                joined[record['phrase']].append(record)
            records, identities = [], []
            for entry in entries:
                matches = joined[entry.phrase]
                record = matches[0] if len(matches) == 1 else {}
                desc = policy.describe_capability(entry.phrase, executor=executor,
                                                  app=effective_app, context=context)
                schema = desc['schema']
                reason = desc['reason']
                problem = schema_problem(schema)
                adapter = ''
                # Preserve the existing reviewed no-args read/UI route. No
                # inferred catalog arguments can add slots to this contract.
                if schema is None and desc['risk'] in (policy.RISK_READ, policy.RISK_UI):
                    if record.get('args'):
                        problem = 'inferred_arguments_only'
                    else:
                        schema = policy.NO_ARGS_SCHEMA
                        problem = ''
                        adapter = 'legacy_no_args/v1'
                if not problem:
                    if schema is policy.NO_ARGS_SCHEMA:
                        adapter = adapter or 'builtin_no_args/v1'
                    elif set(schema) == {'remainder'} and schema['remainder']['type'] == 'str':
                        adapter = 'literal_remainder/v1'
                    else:
                        problem = 'adapter_unavailable'
                reason = reason or problem
                handler = entry.handler if entry.source == 'plugin' else executor.commands.get(entry.phrase)
                if entry.source == 'plugin':
                    live_handler = registry.get(entry.phrase, {}).get('func')
                    if not callable(handler):
                        reason = reason or 'no_handler'
                    elif live_handler is not handler:
                        reason = reason or 'stale_handler'
                else:
                    from .commands import get_handler
                    if not handler or get_handler(handler.get('type')) is None:
                        reason = reason or 'no_handler'
                if len(matches) != 1:
                    reason = reason or 'ambiguous_documentation_mapping'
                requirements = list(entry.preconditions)
                if entry.scope is not None:
                    requirements.append(entry.scope.describe())
                # Recovery is declared metadata only, never the catalog's
                # inferred undoable flag or an LLM-generated description.
                recovery = ('declared_reversible' if entry.metadata.get('reversible') is True
                            else 'not_declared')
                cid = 'cap_' + _digest([entry.source, entry.phrase])[:24]
                records.append(dict(id=cid, canonical_phrase=entry.phrase,
                                    aliases=sorted(set(entry.aliases)),
                                    documentation_id=record.get('canonical_id'),
                                    description=record.get('description') or entry.description or entry.phrase,
                                    category=entry.pack, pack=entry.pack,
                                    argument_schema=dict(schema) if schema is not None else None,
                                    adapter=adapter or None, target_requirements=requirements,
                                    available=not bool(reason), reason=reason, detail=desc.get('detail', ''),
                                    effect_hook=desc.get('effect_hook'), recovery=recovery,
                                    declared_risk=desc['risk'],
                                    approval_possible=desc.get('approval_possible', False),
                                    schema_version=SCHEMA_VERSION, provenance='live_registry',
                                    registry_generation=getattr(executor, '_capability_generation', 0),
                                    documentation_only=False,
                                    foreground_match=entry.scope is not None and not desc['reason']))
                identities.append([entry.phrase, id(entry), id(entry.handler), entry.metadata,
                                   executor.commands.get(entry.phrase), entry.param_schema,
                                   # Detect changes to the loaded plugin declaration,
                                   # even before the matcher is explicitly rebuilt.
                                   {k: registry.get(entry.phrase, {}).get(k) for k in
                                    ('source', 'phrase', 'aliases', 'risk_class', 'param_schema', 'metadata')}])
            # Inventory registrations shadowed by builtins or not yet loaded
            # into this matcher too. A registry row alone is no runtime route.
            seen_registrations = set()
            for registration in registry.values():
                if id(registration) in seen_registrations:
                    continue
                seen_registrations.add(id(registration))
                phrase = registration['phrase']
                live = matcher._entries.get(phrase)
                if live is not None and live.source == 'plugin' and live.handler is registration.get('func'):
                    continue
                source = str(registration.get('source', 'plugin'))
                reason = 'shadowed_registration' if live is not None else 'unindexed_registration'
                doc_id = source.rsplit('.', 1)[-1] + '.' + command_catalog.slug(phrase)
                records.append(dict(id='reg_' + _digest([source, phrase])[:24],
                                    canonical_phrase=phrase, aliases=registration.get('aliases', []),
                                    documentation_id=doc_id, description=phrase,
                                    category=registration.get('pack', 'core'), pack=registration.get('pack', 'core'),
                                    argument_schema=registration.get('param_schema'), available=False,
                                    reason=reason, adapter=None, target_requirements=[], effect_hook=None,
                                    recovery='not_declared', approval_possible=False,
                                    schema_version=SCHEMA_VERSION, provenance='registered_not_executable',
                                    registry_generation=getattr(executor, '_capability_generation', 0),
                                    documentation_only=False, foreground_match=False))
                identities.append([source, phrase, id(registration.get('func')), reason])
            # Static rows may explain absent commands, but never offer them.
            live_docs = {r['documentation_id'] for r in records}
            for record in documentation:
                doc_id = record['canonical_id']
                if doc_id in live_docs:
                    continue
                records.append(dict(id='doc_' + _digest(doc_id)[:24],
                                    canonical_phrase=command_catalog.canonical_phrase(record),
                                    aliases=record.get('aliases', []), documentation_id=doc_id,
                                    description=record.get('description', ''),
                                    category=record.get('pack', 'documentation'),
                                    pack=record.get('pack', 'documentation'),
                                    argument_schema=None, available=False, reason='documentation_only',
                                    adapter=None, target_requirements=[], effect_hook=None,
                                    recovery='not_declared', approval_possible=False,
                                    schema_version=SCHEMA_VERSION, provenance='documentation',
                                    registry_generation=getattr(executor, '_capability_generation', 0),
                                    documentation_only=True, foreground_match=False))
            config = getattr(effective_app, 'config', {}) or {}
            identities.append({'command_packs': config.get('command_packs'),
                               'execution_policy': config.get('execution_policy')})
            return cls(records, generation=getattr(executor, '_capability_generation', 0),
                       identities=identities)
        except Exception:
            # Registry/metadata/schema failures must never advertise static tools.
            return cls(error='registry_unavailable')

    def rank(self, query='', category=None):
        ids = set(self.by_id)
        if category is not None:
            ids = {cid for cid in ids if self.by_id[cid].record()['category'] == category}
        scores = defaultdict(int)
        exact = self.exact.get(_normal(query), ()) if query else ()
        for cid in exact:
            scores[cid] += 100
        for token in _tokens(query):
            for field, weight in (('names', 8), ('description', 3), ('category', 2)):
                for cid in self.postings[field].get(token, ()):
                    scores[cid] += weight
        if query:
            ids &= set(scores)
        for cid in ids:
            if self.by_id[cid].record()['foreground_match']:
                scores[cid] += 5
        return sorted(ids, key=lambda cid: (-scores[cid], cid))


class CapabilityRequest:
    """One turn's pinned offers and host-issued, request-bound cursors.

    The object must travel with the response context; no process/thread-local
    map or later, larger menu can substitute for the actual offered records.
    """
    def __init__(self, executor, request_id, *, app=None, context=None):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('request_id required')
        self.executor, self.request_id = executor, request_id
        self.app = app if app is not None else getattr(executor, '_app', None)
        self.context = context
        self.index = executor.capability_index(app=self.app, context=context)
        self.generation = policy.current_generation(self.app)
        self._offered = {}
        self._cursors = {}
        self._advertised_categories = set()
        self.rounds = 0
        self._started = False

    @property
    def offered(self):
        return MappingProxyType(dict(self._offered))

    def _fresh(self):
        if not policy.is_fresh(self.app, self.generation):
            raise CapabilityError('stale_generation')
        live = self.executor.capability_index(app=self.app, context=None)
        if live.error or live.revision != self.index.revision:
            raise CapabilityError('stale_index')

    def _cursor(self, kind, value):
        token = uuid.uuid4().hex
        self._cursors[token] = (kind, value)
        return token

    def _categories(self, offset=0):
        out = []
        position = offset
        for category in self.index.categories[offset:]:
            # Reserve space for cursors, counts and refusal metadata in the
            # separate 2,000-character index envelope.
            if len(_json(out + [category])) > INDEX_BUDGET - 800:
                break
            out.append(dict(category))
            self._advertised_categories.add(category['id'])
            position += 1
        cursor = (self._cursor('categories', position) if position < len(self.index.categories) else None)
        return dict(categories=out, next_category_cursor=cursor,
                    total_categories=len(self.index.categories))

    def _page(self, query, category, offset=0):
        ids = self.index.rank(query, category)
        records, used, position = [], 2, offset
        unavailable = defaultdict(int)
        for cid in ids:
            reason = self.index.by_id[cid].record()['reason']
            if reason:
                unavailable[reason] += 1
        while position < len(ids) and len(records) < PAGE_SIZE:
            descriptor = self.index.by_id[ids[position]]
            record = descriptor.record()
            cost = len(descriptor.payload) + bool(records)
            if cost + 2 > DESCRIPTOR_BUDGET:
                stub = {'id': descriptor.id, 'reason': 'full_descriptor_required',
                        'descriptor_cursor': self._cursor('descriptor', descriptor.id)}
                cost = len(_json(stub)) + bool(records)
                record = stub
            if used + cost > DESCRIPTOR_BUDGET:
                break
            records.append(record)
            used += cost
            if record.get('available'):
                self._offered[descriptor.id] = descriptor
            position += 1
        next_cursor = (self._cursor('page', (query, category, position)) if position < len(ids) else None)
        return dict(records=records, **self._categories(), next_cursor=next_cursor,
                    total_matches=len(ids), unavailable_matches=dict(sorted(unavailable.items())),
                    search_complete=next_cursor is None and not self.index.error, revision=self.index.revision,
                    reason=self.index.error, lookup_rounds_remaining=LOOKUP_ROUNDS - self.rounds)

    def initial(self, query=''):
        if self._started:
            raise CapabilityError('initial_already_used')
        self._started = True
        if not isinstance(query, str):
            raise CapabilityError('invalid_query')
        if not self.index.error:
            self._fresh()
        return self._page(query, None)

    def lookup(self, *, query='', category=None, cursor=None, remaining_context_chars=FULL_DESCRIPTOR_BUDGET):
        if self.rounds >= LOOKUP_ROUNDS:
            return dict(records=[], reason='lookup_budget_exhausted', search_complete=False,
                        next_cursor=None, lookup_rounds_remaining=0)
        self.rounds += 1
        self._fresh()
        if not isinstance(query, str) or len(query) > 200 or '\x00' in query:
            raise CapabilityError('invalid_query')
        if category is not None and (not isinstance(category, str) or category not in self._advertised_categories):
            raise CapabilityError('unadvertised_category')
        if cursor is not None:
            if not isinstance(cursor, str) or cursor not in self._cursors:
                raise CapabilityError('invalid_cursor')
            if query or category is not None:
                raise CapabilityError('cursor_query_conflict')
            kind, value = self._cursors.pop(cursor)
            if kind == 'categories':
                metadata = self._categories(value)
                return dict(records=[], **metadata, reason='',
                            search_complete=metadata['next_category_cursor'] is None,
                            next_cursor=None, lookup_rounds_remaining=LOOKUP_ROUNDS - self.rounds)
            if kind == 'page':
                return self._page(*value)
            descriptor = self.index.by_id[value]
            if len(descriptor.payload) + 2 > min(FULL_DESCRIPTOR_BUDGET, remaining_context_chars):
                return dict(records=[], reason='descriptor_too_large', search_complete=False,
                            next_cursor=None, lookup_rounds_remaining=LOOKUP_ROUNDS - self.rounds)
            record = descriptor.record()
            if record['available']:
                self._offered[descriptor.id] = descriptor
            return dict(records=[record], reason='', search_complete=True, next_cursor=None,
                        total_matches=1, lookup_rounds_remaining=LOOKUP_ROUNDS - self.rounds)
        if not query and category is None:
            metadata = self._categories()
            return dict(records=[], **metadata, reason=self.index.error,
                        search_complete=metadata['next_category_cursor'] is None, next_cursor=None,
                        lookup_rounds_remaining=LOOKUP_ROUNDS - self.rounds)
        return self._page(query, category)

    def bind(self, offered_id, args=None, *, source_text=''):
        """Validate exactly this turn's offered ID; never run or approve it."""
        descriptor = self._offered.get(offered_id)
        if descriptor is None:
            raise CapabilityError('unoffered')
        self._fresh()
        record = descriptor.record()
        if args is not None and not isinstance(args, dict):
            raise CapabilityError('invalid_args')
        invocation = policy.Invocation(record['canonical_phrase'], dict(args or {}), policy.Route.MODEL,
                                       self.generation, source_text=source_text)
        decision = policy.authorize(invocation, app=self.app, executor=self.executor, quiet=True)
        if isinstance(decision, policy.Denied):
            raise CapabilityError(decision.reason)
        return invocation

    def bind_legacy(self, phrase, *, source_text=''):
        """Legacy ACTION may use only an exact canonical phrase already offered."""
        for cid, descriptor in self._offered.items():
            if descriptor.record()['canonical_phrase'] == phrase:
                return self.bind(cid, source_text=source_text)
        raise CapabilityError('unoffered')
