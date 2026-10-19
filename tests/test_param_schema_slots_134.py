"""Queue 134: typed plugin schemas must not collapse back to a raw remainder."""
from __future__ import annotations

import pytest


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
# Send needs a destination; 607 expanded snap to an optional app and the
# handler's free-text side spelling. Keep pinning the exact live declarations.
TYPED[("plugins.commands.windows", "send")]["monitor"]["required"] = True
TYPED[("plugins.commands.windows", "snap")] = {
    "side": {"type": "str", "required": False},
    "app_name": {"type": "app_name", "required": False},
}


@pytest.fixture(scope="module")
def schemas():
    # Run before conftest's per-test discovery suppression. Isolate the live
    # registry without launching a child process or importing dictation.
    from samsara import plugin_commands
    from tools.dump_command_metadata import build_executor
    with pytest.MonkeyPatch.context() as clean:
        clean.setattr(plugin_commands, "_REGISTRY", {})
        clean.setattr(plugin_commands, "_shared_matcher", None)
        executor = build_executor()
        rows = {(e["source"], e["phrase"]): e.get("param_schema", {})
                for e in plugin_commands._REGISTRY.values()}
        entry, remainder = executor._matcher.match("open made up application")
        return rows, [entry.phrase if entry else None, remainder]


def test_flattened_schemas_are_typed_or_intentional_free_text(schemas):
    all_schemas, _match = schemas
    schemas = {key: all_schemas[key] for key in FREE_TEXT | NO_ARGUMENTS | set(TYPED)}
    assert len(schemas) == 43
    assert {key for key, schema in schemas.items() if schema == {"remainder": {"type": "str", "required": False}}} == FREE_TEXT
    assert {key for key, schema in schemas.items() if not schema} == NO_ARGUMENTS
    assert {key: schemas[key] for key in TYPED} == TYPED


def test_app_verb_single_token_matcher_keeps_the_raw_dispatch_remainder(schemas):
    _all_schemas, match = schemas
    assert match == ["open", "made up application"]
