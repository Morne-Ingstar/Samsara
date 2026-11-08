"""Build identity and non-personal configuration keys; never collect values."""
from pathlib import Path
import json
import re
import sys


ROOT = Path(__file__).resolve().parents[2]


def build_commit(root=None, executable=None, frozen=None):
    root = Path(root) if root is not None else ROOT
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = Path(executable or sys.executable)
    # Daily's real metadata lives beside the executable, outside _internal.
    for path in (executable.parent / "BUILD_INFO.txt" if frozen else root / "BUILD_INFO.txt",
                 root / "release" / "build_info.json"):
        try:
            raw = path.read_text(encoding="utf-8-sig")[:8192]
            value = json.loads(raw).get("commit", "") if path.suffix == ".json" else (
                re.search(r"(?im)^commit[ :=]+([0-9a-f]{7,40})\s*$", raw).group(1))
            if re.fullmatch(r"[0-9a-f]{7,40}", value):
                return value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if not frozen:
        # Source builds only: read this application's HEAD, without running git.
        try:
            gitdir = root / ".git"
            if gitdir.is_file():
                gitdir = (root / gitdir.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")).resolve()
            head = (gitdir / "HEAD").read_text(encoding="ascii").strip()
            if head.startswith("ref: refs/"):
                ref = head[5:]
                common = gitdir
                if (gitdir / "commondir").is_file():
                    common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
                try:
                    head = (common / ref).read_text(encoding="ascii").strip()
                except OSError:
                    head = next(line.split()[0] for line in (common / "packed-refs").read_text().splitlines()
                                if line.endswith(" " + ref))
            if re.fullmatch(r"[0-9a-f]{40}", head):
                return head
        except (OSError, ValueError, StopIteration):
            pass
    return "not recorded in this build"


def changed_keys(config):
    from samsara.config_defaults import DEFAULTS
    # ui_scale is a legacy default used by support_feedback outside the schema.
    defaults = {**DEFAULTS, "ui_scale": 1.0}
    result = []
    for key, default in defaults.items():
        value = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            if value != default:
                result.append(key)
    return sorted(result)


def recent_commands(app):
    from .capture import canonical_id
    from samsara.outcome_ring import as_record
    records = (as_record(item) for item in list(getattr(app, "_outcome_ring", ()) or ())[-20:])
    return [name for item in records if item is not None
            and (name := canonical_id(item.canonical_id)) != "unavailable"]
