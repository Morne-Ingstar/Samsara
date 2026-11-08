"""Shipped code identities and log templates, with no runtime/private plugins."""
import ast
from functools import lru_cache
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "samsara" / "data" / "report_code.json"


def create_index(root):
    root = Path(root)
    frames, templates = {}, set()
    paths = [root / "dictation.py"]
    for package in ("samsara", "plugins"):
        paths.extend((root / package).rglob("*.py"))
    for path in paths:
        if path.name.startswith(("local_", "demo_")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError):
            continue
        frames[path.relative_to(root).as_posix()] = sorted({"<module>"} | {
            node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))})
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning", "error", "critical", "exception")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("log", "logger", "logging") and node.args):
                continue
            message = node.args[0]
            if isinstance(message, ast.JoinedStr):
                parts = [part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                         else "\x00" for part in message.values]
                template = "".join(parts)
            elif isinstance(message, ast.Constant) and isinstance(message.value, str):
                template = re.sub(r"%(?:\([^)]+\))?[-+#0 .\d]*[sdrifge]", "\x00", message.value)
            else:
                continue
            if "\n" not in template and template.count("\x00") <= 6 and len(template) < 1000 and len(template.replace("\x00", "").strip()) >= 8:
                templates.add(template)
    return {"frames": frames, "templates": sorted(templates, key=lambda item: (-len(item.replace("\x00", "")), item))}


@lru_cache(maxsize=1)
def index():
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"frames": {}, "templates": []} if getattr(sys, "frozen", False) else create_index(ROOT)


@lru_cache(maxsize=1)
def templates():
    return tuple((re.compile("^" + "(.*?)".join(re.escape(part) for part in text.split("\x00")) + "$"),
                  text.split("\x00")) for text in index().get("templates", ()))


def structured_message(message):
    for pattern, parts in templates():
        if not message.startswith(parts[0]):
            continue
        match = pattern.fullmatch(message)
        if match is None:
            continue
        output = parts[0]
        for value, literal in zip(match.groups(), parts[1:]):
            measurement = re.match(r"(?:ms|s\b|Hz|dB| chars| words| samples| frames)", literal) or re.search(
                r"(?:elapsed_ms|duration_ms|latency_ms|rms|peak|samples|channels|frames|segments|count|pid|total|thread)\s*[=:]\s*$", output)
            safe = value if measurement and re.fullmatch(r"-?\d{1,9}(?:\.\d{1,6})?(?:ms|s|Hz|dB)?", value) else "<value redacted>"
            output += safe + literal
        return output
    return None
