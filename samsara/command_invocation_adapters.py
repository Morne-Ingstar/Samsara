"""Reviewed argument delivery, with request offers rechecked at the effect boundary.

No schema inference, JSON-to-command concatenation or fuzzy redispatch belongs
here. The only generic argument transport is a declared literal remainder.
"""
from dataclasses import dataclass
import json
import sys

from samsara import execution_policy as policy
from samsara.ava_capabilities import CapabilityError


@dataclass(frozen=True, repr=False)
class Binding:
    request: object
    offered_id: str
    arguments: str
    source_text: str

    def invocation(self):
        args = json.loads(self.arguments)
        descriptor = self.request.offered.get(self.offered_id)
        if descriptor is None:
            raise CapabilityError('unoffered')
        record = descriptor.record()
        if record['adapter'] not in {'literal_remainder/v1', 'legacy_no_args/v1', 'builtin_no_args/v1'}:
            raise CapabilityError('adapter_unavailable')
        # Generic string arguments may name recipients, paths or amounts. A
        # model-generated plausible target is never a local entity binding.
        if args:
            if (record['adapter'] != 'literal_remainder/v1' or set(args) != {'remainder'}
                    or not isinstance(args['remainder'], str) or '\0' in args['remainder']
                    or not args['remainder'] or args['remainder'] not in self.source_text):
                raise CapabilityError('ungrounded_args')
        return self.request.bind(self.offered_id, args, source_text=self.source_text)

    def check(self, executor, app, command_id, args, generation, source_text):
        if self.request.executor is not executor or self.request.app is not app:
            raise CapabilityError('wrong_request')
        invocation = self.invocation()
        if (invocation.command_id != command_id or invocation.args != args
                or invocation.generation != generation or source_text != self.source_text):
            raise CapabilityError('wrong_request')


def bind(request, offered_id, args, source_text):
    if not isinstance(args, dict):
        raise CapabilityError('invalid_args')
    binding = Binding(request, offered_id, json.dumps(args, ensure_ascii=False, allow_nan=False), source_text)
    binding.invocation()
    return binding


def delivery_problem(executor, entry, args, route):
    """Fail before calling a handler which would silently ignore typed slots."""
    if route is not policy.Route.MODEL or not args:
        return ''
    # 1031b's reviewed click handler consumes label through label_remainder,
    # after authorize remembers the schema-checked invocation. Match the actual
    # loaded handler, never a command name or metadata claiming this adapter.
    numbers = sys.modules.get('plugins.commands.show_numbers')
    if (entry is not None and entry.source == 'plugin'
            and numbers is not None and entry.phrase == 'click'
            and entry.handler is getattr(numbers, 'handle_click', None)):
        return ''
    if entry is None or entry.source != 'plugin' or set(args) != {'remainder'}:
        return 'adapter_unavailable'
    _, _, schema = policy.classify(entry.phrase, executor=executor, declared_only=True)
    if not isinstance(schema, dict) or set(schema) != {'remainder'} or schema['remainder'].get('type') != 'str':
        return 'adapter_unavailable'
    return ''
