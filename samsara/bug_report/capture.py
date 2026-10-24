"""Immutable local diagnostic snapshots with strictly enumerated metadata."""
from dataclasses import dataclass
from pathlib import Path
import json
import platform
import re
from .redact import error_first_line, redact_lines, redact_fault

from .vocabulary import OUTCOMES


def tail(path, count=50):
    """Bound I/O even for a huge diagnostic file. Never discover profile paths."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - 65536)
            stream.seek(start)
            lines = stream.read(65536).decode("utf-8", "replace").splitlines()
            return lines[1 if start else 0:][-count:]
    except (OSError, ValueError):
        return []


def canonical_id(value):
    """Only shipped catalogue identities; never echo a caller-supplied string."""
    try:
        catalog = json.loads((Path(__file__).resolve().parents[2] / "commands_catalog.json").read_text(encoding="utf-8"))
        entries = catalog if isinstance(catalog, list) else catalog.get("commands", [])
        ids = {item.get("canonical_id") for item in entries if isinstance(item, dict)}
        if value in ids and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", str(value)):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return "unavailable"


@dataclass(frozen=True)
class Failure:
    outcome: str = "report"
    command: str = "unavailable"
    error: str = "Details omitted for privacy (unrecognized text)."


def failure(label="", kind="error", source="", detail=None):
    detail = detail if isinstance(detail, dict) else {}
    command = detail.get("canonical_id") or getattr(label, "canonical_id", "")
    if not command and detail.get("phrase"):
        from samsara.outcome_ring import canonical_id_for_phrase
        command = canonical_id_for_phrase(detail["phrase"])
    error = error_first_line(detail.get("error") or detail.get("reason") or label)
    if detail.get("reason") == "pack_disabled":
        from samsara.command_packs import PACKS
        pack = detail.get("pack")
        if pack in PACKS:
            error = "Command pack is disabled: " + pack
    return Failure(source if source in OUTCOMES else kind if kind in OUTCOMES else "report",
                   canonical_id(command), error)


def snapshot(app, failed=None, *, fault=()):
    """Called only on Report: current safe configuration and last log window."""
    from samsara import __version__
    from samsara.command_packs import PACKS
    failed = failed or Failure()
    cfg = getattr(app, "config", {}) or {}
    from samsara.listening_display import run_display
    display = run_display(app).value
    packs_cfg = cfg.get("command_packs", {}) or {}
    packs = sorted(name for name, meta in PACKS.items()
                   if meta["always_on"] or packs_cfg.get(name, meta["default_enabled"]) is True)
    integrations = []
    for name, section in (("Agent server", "agent_server"),
                          ("Vision", "vision"), ("Smart actions", "smart_actions")):
        if (cfg.get(section, {}) or {}).get("enabled") is True:
            integrations.append(name)
    if (cfg.get("ava", {}) or {}).get("provider_policy") in ("local", "cloud"):
        integrations.append("Ava")
    if packs_cfg.get("hermes") is True and (cfg.get("hermes", {}) or {}).get("notice_accepted") is True:
        integrations.append("Hermes")
    if ((cfg.get("tasks", {}) or {}).get("todoist", {}) or {}).get("enabled") is True:
        integrations.append("Todoist")
    commit = "unavailable"
    # Optional build metadata, never invoke git or inspect a user's repository.
    build = Path(__file__).resolve().parents[2] / "release" / "build_info.json"
    try:
        candidate = json.loads(build.read_text(encoding="utf-8")).get("commit", "")
        if re.fullmatch(r"[0-9a-f]{7,40}", candidate):
            commit = candidate
    except (OSError, ValueError, TypeError):
        pass
    log_dir = getattr(app, "_bug_report_log_dir", None)
    logs = redact_lines(tail(Path(log_dir) / "samsara.log")) if log_dir else ()
    version = __version__ if re.fullmatch(r"[0-9A-Za-z.+-]{1,60}", __version__) else "unavailable"
    windows = ".".join(str(n) for n in __import__('sys').getwindowsversion()[:3]) if platform.system() == "Windows" else "unavailable"
    lines = ["Samsara problem report", f"Outcome: {failed.outcome}", f"Command: {failed.command}",
             f"Error: {failed.error}", f"Samsara: {version}", f"Commit: {commit}",
             f"Windows: {windows}", f"Display: {display.title()}",
             "Enabled packs: " + (", ".join(packs) or "none"),
             "Integrations on: " + (", ".join(integrations) or "none"),
             "", "Diagnostics (last 50 lines; unrecognized lines omitted):", *(logs or ("No allow-listed lines.",))]
    if fault:
        lines.extend(("", "Previous-session fault (unrecognized frames omitted):", *redact_fault(fault)))
    return "\n".join(lines)
