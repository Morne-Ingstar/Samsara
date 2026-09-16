"""Queue 134: typed plugin schemas must not collapse back to a raw remainder."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


FREE_TEXT = {
    ("plugins.commands.health_tracker", "pain level"),
    ("plugins.commands.health_tracker", "took"),
    ("plugins.commands.health_tracker", "symptom"),
    ("plugins.commands.smart_actions", "note"),
    ("plugins.commands.smart_actions", "brain dump"),
    ("plugins.commands.tasks", "add to list"),
}

NO_ARGUMENTS = {
    ("plugins.commands.alarm_commands", "complete alarm"),
    ("plugins.commands.core_utils", "restart samsara"),
    ("plugins.commands.health_tracker", "export health log"),
    ("plugins.commands.health_tracker", "undo health log"),
    ("plugins.commands.health_tracker", "clear health log"),
    ("plugins.commands.tasks", "clear completed"),
    ("plugins.commands.text_marker", "mark here"),
    ("plugins.commands.text_marker", "select to here"),
    ("plugins.commands.text_marker", "select paragraph"),
}

TYPED = {
    ("plugins.commands.alarm_commands", "enable alarm"): {"alarm_name": {"type": "str", "required": True}},
    ("plugins.commands.alarm_commands", "disable alarm"): {"alarm_name": {"type": "str", "required": True}},
    **{("plugins.commands.app_verbs", phrase): {"app_name": {"type": "app_name", "required": False}}
       for phrase in ("focus", "open", "close")},
    ("plugins.commands.show_numbers", "click"): {"label": {"type": "int", "required": False}},
    ("plugins.commands.tab_finder", "find tab"): {"query": {"type": "text", "required": True}},
    **{("plugins.commands.tasks", phrase): {"task_number": {"type": "int", "required": True}}
       for phrase in ("complete task", "remove task")},
    ("plugins.commands.window_cube", "cube page"): {"page": {"type": "int", "required": True}},
    **{("plugins.commands.window_cube", phrase): {"numbers": {"type": "int", "required": True}}
       for phrase in ("cube copy", "cube tile")},
    **{("plugins.commands.window_switcher", phrase): {"label": {"type": "nato_letter", "required": True}}
       for phrase in ("window switch", "window bring", "window mute", "window unmute", "window close")},
    ("plugins.commands.window_switcher", "window move"): {
        "label": {"type": "nato_letter", "required": True},
        "monitor": {"type": "monitor", "required": False},
    },
    **{("plugins.commands.window_switcher", phrase): {"labels": {"type": "nato_letter", "required": True}}
       for phrase in ("window copy", "window tile")},
    ("plugins.commands.windows", "bring"): {"app_name": {"type": "app_name", "required": False}},
    ("plugins.commands.windows", "send"): {
        "app_name": {"type": "app_name", "required": False},
        "monitor": {"type": "monitor", "required": True},
    },
    **{("plugins.commands.windows", phrase): {"name": {"type": "text", "required": True}}
       for phrase in ("save layout", "restore layout", "delete layout")},
    ("plugins.commands.windows", "find window"): {"app_name": {"type": "app_name", "required": True}},
    ("plugins.commands.windows", "cursor to"): {"monitor": {"type": "monitor", "required": True}},
    ("plugins.commands.windows", "snap"): {"side": {"type": "side", "required": True}},
}

# Matcher dispatch always supplies the handler's positional raw remainder;
# missing spoken detail is handled by the command's own prompt. Typed metadata
# must therefore remain optional on this direct route.
TYPED = {
    key: {name: {**spec, "required": False} for name, spec in schema.items()}
    for key, schema in TYPED.items()
}


def _schemas():
    code = """
import json
from samsara.commands import CommandExecutor
from samsara import plugin_commands
executor = CommandExecutor()
seen, rows = set(), []
for entry in plugin_commands._REGISTRY.values():
    if id(entry) not in seen:
        seen.add(id(entry))
        rows.append([entry['source'], entry['phrase'], entry.get('param_schema', {})])
entry, remainder = executor._matcher.match('open made up application')
print('QUEUE134=' + json.dumps({'rows': rows, 'match': [entry.phrase if entry else None, remainder]}))
"""
    output = subprocess.check_output([sys.executable, "-c", code], cwd=REPO, text=True)
    payload = json.loads(next(line[9:] for line in output.splitlines() if line.startswith("QUEUE134=")))
    schemas = {(source, phrase): schema for source, phrase, schema in payload["rows"]}
    return schemas, payload["match"]


def test_flattened_schemas_are_typed_or_intentional_free_text():
    all_schemas, _match = _schemas()
    schemas = {key: all_schemas[key] for key in FREE_TEXT | NO_ARGUMENTS | set(TYPED)}
    assert len(schemas) == 43
    assert {key for key, schema in schemas.items() if schema == {"remainder": {"type": "str", "required": False}}} == FREE_TEXT
    assert {key for key, schema in schemas.items() if not schema} == NO_ARGUMENTS
    assert {key: schemas[key] for key in TYPED} == TYPED


def test_app_verb_single_token_matcher_keeps_the_raw_dispatch_remainder():
    _all_schemas, match = _schemas()
    assert match == ["open", "made up application"]
