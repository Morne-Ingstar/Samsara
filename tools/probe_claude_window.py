"""Dump the UI Automation tree of the live Claude desktop window (read-only).

Diagnostic for plugins/commands/message_claude.py: before any code presses a
key in the Claude app we need the REAL control names -- the composer (an
editable element), the Send control next to it, and the conversation view
that new message nodes appear in. This walks the tree with the same
`uiautomation` package the plugin uses, prints every node with control type,
name, automation id, class name and the patterns it supports, and then lists
the candidates for those three roles. It never invokes, focuses, types or
clicks anything.

Usage:
    F:\\envs\\sami\\python.exe tools\\probe_claude_window.py [--depth 14] [--max-nodes 4000]
                                                     [--title Claude] [--out perf_artifacts/claude_uia.txt]
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import uiautomation as uia
except ImportError as exc:  # pragma: no cover - diagnostic only
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


def find_claude_windows(title_hint: str, process_only: bool = True):
    """Top-level windows of the Claude desktop app (process claude*.exe). With
    process_only=False any window whose title contains the hint is included
    too (browsers, editors) -- useful to compare trees, noisy otherwise."""
    hits = []
    for w in uia.GetRootControl().GetChildren():
        try:
            name = w.Name or ""
            cls = w.ClassName or ""
            pid = w.ProcessId
        except Exception:
            continue
        proc = ""
        try:
            import psutil  # noqa: PLC0415
            proc = psutil.Process(pid).name()
        except Exception:
            pass
        if proc.lower().startswith("claude") or (not process_only and title_hint.lower() in name.lower()):
            hits.append((w, name, cls, proc, pid))
    return hits


def patterns_of(ctrl) -> list:
    found = []
    for label, pid in PATTERN_IDS.items():
        try:
            if ctrl.GetPattern(pid) is not None:
                found.append(label)
        except Exception:
            pass
    return found


def prime_accessibility(root, settle_s: float = 1.5) -> None:
    """Chromium/Electron builds its accessibility tree lazily, only after a UIA
    client asks for it. Touch the deepest nodes once, wait, and the second
    walk sees the real web content (composer, buttons, messages)."""
    stack = [root]
    seen = 0
    while stack and seen < 200:
        ctrl = stack.pop()
        seen += 1
        try:
            stack.extend(ctrl.GetChildren())
        except Exception:
            pass
    time.sleep(settle_s)


def walk(root, depth_limit: int, max_nodes: int, budget_s: float, out):
    """Depth-first dump; returns the flat node list [(depth, ctrl, info)]."""
    nodes = []
    t0 = time.monotonic()
    stack = [(root, 0)]
    while stack:
        ctrl, depth = stack.pop()
        if len(nodes) >= max_nodes or time.monotonic() - t0 > budget_s:
            out.write(f"... stopped: {len(nodes)} nodes, {time.monotonic() - t0:.1f}s\n")
            break
        try:
            info = {
                "type": ctrl.ControlTypeName, "name": (ctrl.Name or "")[:80],
                "aid": ctrl.AutomationId or "", "cls": ctrl.ClassName or "",
                "patterns": patterns_of(ctrl),
            }
            try:
                r = ctrl.BoundingRectangle
                info["rect"] = (r.left, r.top, r.right, r.bottom)
            except Exception:
                info["rect"] = None
        except Exception as exc:
            info = {"type": "?", "name": f"<error {exc}>", "aid": "", "cls": "", "patterns": [], "rect": None}
        nodes.append((depth, ctrl, info))
        out.write("  " * depth + f"{info['type']} name={info['name']!r} aid={info['aid']!r} "
                  f"cls={info['cls']!r} patterns={','.join(info['patterns'])} rect={info['rect']}\n")
        if depth < depth_limit:
            try:
                children = ctrl.GetChildren()
            except Exception:
                children = []
            for child in reversed(children):
                stack.append((child, depth + 1))
    return nodes


def candidates(nodes, out):
    out.write("\n== candidates ==\n")
    editables = [n for n in nodes if n[2]["type"] in ("EditControl", "DocumentControl")
                 or "Value" in n[2]["patterns"] and n[2]["type"] != "WindowControl"]
    buttons = [n for n in nodes if n[2]["type"] == "ButtonControl"]
    send_like = [n for n in buttons if "send" in n[2]["name"].lower() or "submit" in n[2]["name"].lower()]
    lists = [n for n in nodes if n[2]["type"] in ("ListControl", "GroupControl", "PaneControl")
             and any(k in n[2]["name"].lower() for k in ("conversation", "message", "chat", "main"))]
    out.write("composer candidates (editable / value pattern):\n")
    for d, _c, i in editables[:20]:
        out.write(f"  depth={d} {i['type']} name={i['name']!r} aid={i['aid']!r} cls={i['cls']!r} patterns={i['patterns']}\n")
    out.write("send candidates (buttons named send/submit):\n")
    for d, _c, i in send_like[:20]:
        out.write(f"  depth={d} name={i['name']!r} aid={i['aid']!r} patterns={i['patterns']} rect={i['rect']}\n")
    out.write(f"all buttons ({len(buttons)}):\n")
    for d, _c, i in buttons[:60]:
        out.write(f"  depth={d} name={i['name']!r} aid={i['aid']!r}\n")
    out.write("conversation/message containers:\n")
    for d, _c, i in lists[:20]:
        out.write(f"  depth={d} {i['type']} name={i['name']!r} aid={i['aid']!r} cls={i['cls']!r}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", default="Claude")
    parser.add_argument("--depth", type=int, default=40)
    parser.add_argument("--all-windows", action="store_true",
                        help="include non-claude.exe windows whose title contains --title")
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument("--budget", type=float, default=60.0, help="seconds")
    parser.add_argument("--out", default=None, help="also write the dump to this file")
    args = parser.parse_args(argv)

    out = sys.stdout
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    windows = find_claude_windows(args.title, process_only=not args.all_windows)
    if not windows:
        out.write("no top-level window with 'Claude' in its title or a claude* process\n")
        return 1
    dump = []
    for w, name, cls, proc, pid in windows:
        out.write(f"== window: name={name!r} class={cls!r} process={proc!r} pid={pid}\n")
        import io  # noqa: PLC0415
        buf = io.StringIO()
        prime_accessibility(w)
        nodes = walk(w, args.depth, args.max_nodes, args.budget, buf)
        candidates(nodes, buf)
        text = buf.getvalue()
        dump.append(text)
        out.write(text)
        out.write(f"== {len(nodes)} nodes\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(dump))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
