"""Read-only ChatGPT desktop UIA spike; never launches an app or sends a prompt.

Run with --output <report artifact.json>. Conversation text and window titles
are deliberately omitted: only known UI labels and text lengths are recorded.
Desktop enumeration failures are reported separately from app discovery.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


_LABELS = {
    "ChatGPT", "You said:", "ChatGPT said:", "You", "Assistant",
    "Stop generating", "Stop streaming", "Stop response", "Send prompt",
    "Send message", "Send", "Message ChatGPT", "New chat", "Copy",
    "Copy response", "Edit message", "Read aloud", "Good response",
    "Bad response", "Retry", "Regenerate", "Conversation",
}


def probe() -> dict:
    import psutil
    import win32gui
    import win32process

    process_ids = []
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info["name"] or "").lower() == "chatgpt.exe":
            process_ids.append(process.info["pid"])
    windows = []
    desktop = {"enumerated_windows": 0, "visible_windows": 0}

    def found(hwnd, _):
        desktop["enumerated_windows"] += 1
        visible = win32gui.IsWindowVisible(hwnd)
        desktop["visible_windows"] += int(bool(visible))
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in process_ids and visible:
            windows.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(found, None)
    except Exception as exc:
        desktop["error_type"] = type(exc).__name__
        desktop["winerror"] = getattr(exc, "winerror", None)
    result = {
        "process_ids": process_ids,
        "window_count": len(windows),
        "prompts_sent": 0,
        "privacy": "No conversation text, arbitrary labels or titles recorded.",
        "windows": [],
        "desktop": desktop,
    }
    if "error_type" in desktop or not desktop["enumerated_windows"]:
        result["status"] = "BLOCKED"
        result["reason"] = (
            "Desktop enumeration failed; app installation/visibility unassessed"
            if "error_type" in desktop else
            "No desktop windows enumerated; app installation/visibility unassessed"
        )
        return result
    if not windows:
        result["status"] = "BLOCKED"
        result["reason"] = (
            "No visible ChatGPT.exe window" if process_ids
            else "No ChatGPT.exe process or visible window discoverable in this execution context"
        )
        return result

    import uiautomation as uia

    for hwnd in windows:
        started = time.monotonic()
        stack = [(uia.ControlFromHandle(hwnd), -1, 0)]
        nodes = []
        while stack and len(nodes) < 5000 and time.monotonic() - started < 20:
            control, parent, depth = stack.pop()
            index = len(nodes)
            name = control.Name or ""
            nodes.append({
                "parent": parent,
                "type": control.ControlTypeName,
                # IDs/classes can embed page content; keep only lengths until
                # a controlled test conversation can be inspected in person.
                "automation_id_chars": len(control.AutomationId or ""),
                "class_chars": len(control.ClassName or ""),
                "label": name if name in _LABELS else None,
                "name_chars": len(name),
                "name_words": len(name.split()),
            })
            if depth < 60:
                stack.extend((child, index, depth + 1)
                             for child in reversed(control.GetChildren()))
        result["windows"].append({
            "hwnd": hwnd,
            "elapsed_s": round(time.monotonic() - started, 4),
            "truncated": bool(stack),
            "nodes": nodes,
        })
    result["status"] = "OBSERVED; manual spike assessment still required"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--watch", action="store_true",
                        help="Read-only: wait for a manually sent reply; save counts/timing only.")
    parser.add_argument("--settle-s", type=float, default=1.5)
    args = parser.parse_args()
    try:
        result = probe()
        if args.watch and result.get("window_count") == 1:
            result = watch_probe(result["windows"][0]["hwnd"], args.settle_s)
    except Exception as exc:
        result = {"status": "BLOCKED", "error_type": type(exc).__name__,
                  "prompts_sent": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "windows"}))
    return 2 if result["status"] == "BLOCKED" else 0


def watch_probe(hwnd, settle_s=1.5):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from samsara.agent_channel import chatgpt_reply as reply

    after = reply.message_index(hwnd)
    print(json.dumps({"status": "WATCH_READY", "after_index": after}), flush=True)
    polls = []
    result = {"prompts_sent": 0, "privacy": "Known labels, counts and timing only.",
              "after_index": after, "polls": polls}
    started = time.monotonic()
    try:
        text = reply.watch_for_reply(hwnd, after, 120, settle_s=settle_s, observe=polls.append)
        result.update(status="COMPLETED", elapsed_s=round(time.monotonic() - started, 4),
                      reply_chars=len(text), reply_words=len(text.split()))
    except (reply.NoNewNode, reply.ReplyTimeout, reply.WindowGone) as exc:
        result.update(status="BLOCKED", reason=type(exc).__name__,
                      elapsed_s=round(time.monotonic() - started, 4))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
