"""Import a UI workflow without modifying it or the owner's config.

Usage: python tools/comfy_import_workflow.py <ui.json> [api.json]
Requires the local ComfyUI server for its node definitions. Unsupported UI-only
nodes and ambiguous fields fail explicitly; they are never guessed or dropped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara.agent_channel.comfy import ComfyClient, ComfyError
from samsara.paths import samsara_home_dir


def convert_workflow(workflow, object_info):
    if not isinstance(workflow.get("nodes"), list):
        raise ValueError("Expected a UI workflow with nodes and links.")
    links = {link[0]: link for link in workflow.get("links", [])}
    graph = {}
    for node in workflow["nodes"]:
        kind = node["type"]
        if (node.get("mode", 0) != 0 or kind not in object_info
                or object_info[kind].get("api_node")):
            raise ValueError(f"Node {node['id']} ({kind}) is disabled, bypassed, or unsupported.")
        definitions = object_info[kind].get("input", {})
        fields = {**definitions.get("required", {}), **definitions.get("optional", {})}
        sockets = {item["name"]: item for item in node.get("inputs", [])}
        named = node.get("widgets_values_named")
        widgets = iter(node.get("widgets_values", []))
        inputs = {}
        for name, definition in fields.items():
            socket = sockets.get(name, {})
            typename = definition[0]
            options = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
            widget = (isinstance(typename, list) or typename in ("INT", "FLOAT", "STRING", "BOOLEAN"))
            widget = widget and not options.get("forceInput", False)
            value_present = False
            if named is not None:
                if name in named:
                    value, value_present = named[name], True
            elif widget:
                # UI seed controls have a second, non-API widget after the number.
                try:
                    value, value_present = next(widgets), True
                    if name in ("seed", "noise_seed") or options.get("control_after_generate"):
                        control = next(widgets)
                        if control not in ("fixed", "increment", "decrement", "randomize"):
                            raise ValueError(f"Unexpected seed control in node {node['id']}.")
                except StopIteration as exc:
                    raise ValueError(f"Missing widget {node['id']}.{name}; export API format instead.") from exc
            if socket.get("link") is not None:
                link = links[socket["link"]]
                if link[3] != node["id"]:
                    raise ValueError("Invalid workflow link target.")
                inputs[name] = [str(link[1]), link[2]]
            elif value_present:
                inputs[name] = value
            elif name in definitions.get("required", {}):
                raise ValueError(f"Missing required input {node['id']}.{name}.")
        graph[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    for node in graph.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and str(value[0]) not in graph:
                raise ValueError("A link references an unsupported node.")
    return graph, detect_fields(graph)


def detect_fields(graph):
    def root(node_id, field, seen=None):
        seen = set() if seen is None else seen
        key = (node_id, field)
        if key in seen:
            raise ValueError("Cyclic workflow input.")
        seen.add(key)
        value = graph[node_id]["inputs"][field]
        if not isinstance(value, list):
            return [node_id, "inputs", field]
        upstream = str(value[0])
        if graph[upstream]["class_type"] == "PrimitiveInt":
            return root(upstream, "value", seen)
        raise ValueError(f"Cannot find the editable source for {node_id}.{field}.")

    encoders = {node_id for node_id, node in graph.items()
                if node["class_type"] == "CLIPTextEncode" and "text" in node["inputs"]}
    positive = {str(node["inputs"]["positive"][0]) for node in graph.values()
                if isinstance(node["inputs"].get("positive"), list)} & encoders
    candidates = positive or encoders
    if len(candidates) != 1:
        raise ValueError("The positive prompt node is ambiguous; export and configure it explicitly.")
    fields = {"prompt": [next(iter(candidates)), "inputs", "text"]}
    for field in ("seed", "width", "height"):
        paths = {tuple(root(node_id, name)) for node_id, node in graph.items()
                 for name in node["inputs"]
                 if name in (("seed", "noise_seed") if field == "seed" else (field,))}
        if len(paths) != 1:
            raise ValueError(f"The {field} input is missing or ambiguous.")
        fields[field] = list(next(iter(paths)))
    return fields


async def main_async(args):
    source = Path(args.source).resolve()
    target = (Path(args.destination) if args.destination else
              samsara_home_dir() / "comfy_workflows" / "klein_simple.api.json").resolve()
    if target == source or target.exists():
        raise ValueError("The destination already exists or is the source; nothing overwritten.")
    async with ComfyClient(args.base_url) as client:
        info = await client.object_info()
    graph, fields = convert_workflow(json.loads(source.read_text(encoding="utf-8-sig")), info)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(graph, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Positive prompt node: {fields['prompt'][0]}.{'.'.join(fields['prompt'][1:])}")
    print(json.dumps({"path": str(target), **fields}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except (ComfyError, ValueError, OSError, KeyError, RuntimeError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")


if __name__ == "__main__":
    main()
