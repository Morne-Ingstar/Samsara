"""A fake ducking host: speaks samsara/ducking_host.py's wire protocol but
touches no COM, so transport behavior can be tested deterministically.

Run as a subprocess by tests/test_ducking_transport.py. Leading underscore
keeps pytest from collecting it as a test module.

    python tests/_fake_ducking_child.py <mode>

Modes:
  normal        answer every command promptly
  hang          wedge forever on get_volume -- models a stuck COM call, the
                exact failure the child process exists to make killable
  die           exit abruptly mid-command on get_volume
  hang_startup  never answer anything, not even the first command
"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

_SESSIONS = [{"sid": "fake-1", "pid": 4242, "process_name": "fake.exe"}]
_LEVELS = {"fake-1": 0.5}


def _reply_for(op, cmd):
    if op == "ping":
        return {"ok": True}
    if op == "list_sessions":
        return {"ok": True, "sessions": list(_SESSIONS)}
    if op == "get_volume":
        sid = cmd.get("sid")
        if sid not in _LEVELS:
            return {"ok": False, "err": "unknown sid"}
        return {"ok": True, "level": _LEVELS[sid]}
    if op == "set_volume":
        sid = cmd.get("sid")
        if sid not in _LEVELS:
            return {"ok": False, "err": "unknown sid"}
        previous = _LEVELS[sid]
        _LEVELS[sid] = float(cmd.get("level", 1.0))
        return {"ok": True, "prev_level": previous}
    if op == "set_many":
        results = []
        for item in cmd.get("items") or []:
            inner = _reply_for("set_volume", item)
            inner["sid"] = item.get("sid")
            results.append(inner)
        return {"ok": True, "results": results}
    if op == "shutdown":
        return {"ok": True}
    return {"ok": False, "err": f"unknown op {op!r}"}


def main() -> int:
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        cmd = json.loads(line)
        op = cmd.get("op")

        if MODE == "hang_startup":
            time.sleep(3600)
        if MODE == "hang" and op == "get_volume":
            time.sleep(3600)
        if MODE == "die" and op == "get_volume":
            sys.stdout.close()
            sys.exit(9)

        reply = _reply_for(op, cmd)
        reply["id"] = cmd.get("id")
        reply["elapsed_ms"] = 0
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        if op == "shutdown":
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
