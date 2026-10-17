"""Read Warp's UI Automation structure without storing conversation text.

The probe never invokes, focuses, clicks, or types into controls. It records
control types, class names, patterns, depth, and redacted name lengths only.
During --watch-seconds, the owner can send one short prompt to Warp's agent;
the probe then checks whether reply-like TextControl/DocumentControl nodes,
an editable composer, or a Stop/streaming marker appear.

Usage:
    python tools\\probe_warp_window.py --watch-seconds 150
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys
import time

try:
    import uiautomation as uia
except ImportError as exc:  # pragma: no cover - probe environment only
    raise SystemExit(f"uiautomation is not importable: {exc}")


PATTERN_IDS = {
    "Invoke": uia.PatternId.InvokePattern,
    "Value": uia.PatternId.ValuePattern,
    "Text": uia.PatternId.TextPattern,
    "Selection": uia.PatternId.SelectionPattern,
    "ScrollItem": uia.PatternId.ScrollItemPattern,
    "Scroll": uia.PatternId.ScrollPattern,
    "LegacyIAccessible": uia.PatternId.LegacyIAccessiblePattern,
}
TEXT_TYPES = {"TextControl", "DocumentControl"}
INPUT_TYPES = {"EditControl", "DocumentControl", "ComboBoxControl"}
ROLE_WORDS = ("message", "conversation", "assistant", "agent", "response", "chat")
STOP_WORDS = ("stop", "cancel response", "stop generating", "stop response")
STREAM_WORDS = ("generating", "thinking", "responding", "working", "in progress")


def _process_name(pid: int) -> str:
    try:
        import psutil  # noqa: PLC0415
        return psutil.Process(pid).name()
    except Exception:
        return ""


def find_warp_windows():
    hits = []
    for window in uia.GetRootControl().GetChildren():
        try:
            pid = window.ProcessId
            process = _process_name(pid)
            if process.lower() == "warp.exe":
                # Keep the title in memory only to avoid recording conversation names.
                title_len = len(window.Name or "")
                hits.append((window, pid, process, window.ClassName or "", title_len))
        except Exception:
            continue
    return hits


def patterns_of(control) -> list[str]:
    found = []
    for label, pattern_id in PATTERN_IDS.items():
        try:
            if control.GetPattern(pattern_id) is not None:
                found.append(label)
        except Exception:
            pass
    return found


def prime_accessibility(root, limit: int = 500) -> None:
    """Request child structure to prime lazy Chromium/Electron accessibility."""
    stack = [root]
    seen = 0
    while stack and seen < limit:
        control = stack.pop()
        seen += 1
        try:
            stack.extend(control.GetChildren())
        except Exception:
            pass
    time.sleep(1.0)


def _record(control, depth: int, role_context: bool, max_children: int):
    try:
        kind = control.ControlTypeName or "?"
        name = control.Name or ""
        class_name = control.ClassName or ""
        automation_id = control.AutomationId or ""
        patterns = patterns_of(control)
        try:
            rect = control.BoundingRectangle
            visible = rect.right > rect.left and rect.bottom > rect.top
        except Exception:
            visible = False
        low_name = name.casefold()
        own_role = any(word in low_name for word in ROLE_WORDS)
        stop_marker = kind == "ButtonControl" and any(word in low_name for word in STOP_WORDS)
        stream_marker = kind in {"ButtonControl", "TextControl", "StatusBarControl", "ProgressBarControl"} \
            and any(word in low_name for word in STREAM_WORDS)
        children = control.GetChildren()[:max_children]
        return {
            "depth": depth,
            "type": kind,
            "class": class_name,
            "automation_id_chars": len(automation_id),
            "name_present": bool(name),
            "name_chars": len(name),
            "patterns": patterns,
            "visible_rect": visible,
            "child_count": len(children),
            "reply_context": role_context or own_role,
            "stop_marker": stop_marker,
            "stream_marker": stream_marker,
            "children": children,
        }
    except Exception as exc:
        return {"depth": depth, "type": "?", "error_type": type(exc).__name__, "children": []}


def snapshot(root, depth_limit: int, max_nodes: int):
    flat = []
    stack = [(root, 0, False)]
    while stack and len(flat) < max_nodes:
        control, depth, role_context = stack.pop()
        row = _record(control, depth, role_context, max_children=max_nodes)
        children = row.pop("children", [])
        flat.append(row)
        if depth < depth_limit:
            next_role = row.get("reply_context", role_context)
            stack.extend((child, depth + 1, next_role) for child in reversed(children))
    return flat


def summarize(nodes) -> dict:
    type_counts = Counter(node.get("type", "?") for node in nodes)
    text_nodes = [node for node in nodes if node.get("type") in TEXT_TYPES and node.get("name_present")]
    reply_nodes = [node for node in text_nodes if node.get("reply_context")]
    editable = [node for node in nodes if node.get("type") in INPUT_TYPES
                or ("Value" in node.get("patterns", [])
                    and node.get("type") not in {"TitleBarControl", "WindowControl", "PaneControl"})]
    return {
        "node_count": len(nodes),
        "types": dict(sorted(type_counts.items())),
        "named_text_or_document_nodes": len(text_nodes),
        "named_text_or_document_in_reply_context": len(reply_nodes),
        "reply_node_structure": [
            {key: node.get(key) for key in ("depth", "type", "class", "patterns", "name_chars", "visible_rect")}
            for node in reply_nodes[:30]
        ],
        "editable_candidates": [
            {key: node.get(key) for key in ("depth", "type", "class", "patterns", "name_chars", "visible_rect")}
            for node in editable[:30]
        ],
        "stop_marker_count": sum(bool(node.get("stop_marker")) for node in nodes),
        "stream_marker_count": sum(bool(node.get("stream_marker")) for node in nodes),
        "markers": [
            {"depth": node.get("depth"), "type": node.get("type"),
             "stop": node.get("stop_marker"), "stream": node.get("stream_marker"),
             "name_chars": node.get("name_chars"), "patterns": node.get("patterns")}
            for node in nodes if node.get("stop_marker") or node.get("stream_marker")
        ],
    }


def write_structure(nodes, out) -> None:
    out.write("== sanitized control structure (names and automation IDs redacted) ==\n")
    for node in nodes:
        if node.get("type") == "?":
            out.write(f"depth={node['depth']} type=? error={node.get('error_type', 'unknown')}\n")
            continue
        out.write(
            f"depth={node['depth']} type={node['type']} class={node['class']!r} "
            f"aid_chars={node['automation_id_chars']} name=<redacted:{node['name_chars']}> "
            f"patterns={','.join(node['patterns'])} rect={node['visible_rect']} "
            f"reply_context={node['reply_context']} stop={node['stop_marker']} stream={node['stream_marker']}\n"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch-seconds", type=float, default=0)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--depth", type=int, default=35)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--out", default="warp_uia_structure.txt")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as out:
        windows = find_warp_windows()
        if not windows:
            out.write("No top-level window belonging to warp.exe was found.\n")
            return 1
        for window, pid, process, class_name, title_len in windows:
            out.write(f"== Warp window pid={pid} process={process!r} class={class_name!r} title=<redacted:{title_len}> ==\n")
            prime_accessibility(window)
            nodes = snapshot(window, args.depth, args.max_nodes)
            write_structure(nodes, out)
            summary = summarize(nodes)
            out.write(f"== initial summary: {summary!r}\n")
            out.flush()

        if args.watch_seconds > 0:
            deadline = time.monotonic() + args.watch_seconds
            started = time.monotonic()
            prior = None
            while time.monotonic() < deadline:
                for window, pid, process, _class_name, _title_len in find_warp_windows():
                    nodes = snapshot(window, args.depth, args.max_nodes)
                    current = summarize(nodes)
                    signature = repr((pid, current))
                    if signature != prior:
                        out.write(f"t={time.monotonic() - started:.1f}s pid={pid} summary={current!r}\n")
                        out.flush()
                        prior = signature
                time.sleep(max(0.1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
