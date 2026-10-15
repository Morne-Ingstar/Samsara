"""Offline guard contract; runs in a plain source export without Git or apps."""

import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
RULES = {rule["id"]: rule for rule in CONFIG["rules"]}


def _allowlisted(allowlist, path, match, secret):
    checks = []
    if "paths" in allowlist:
        checks.append(any(re.search(pattern, path) for pattern in allowlist["paths"]))
    if "regexes" in allowlist:
        target = {"secret": secret, "match": match}[allowlist.get("regexTarget", "secret")]
        checks.append(any(re.search(pattern, target) for pattern in allowlist["regexes"]))
    return bool(checks) and (all(checks) if allowlist.get("condition") == "AND" else any(checks))


def _findings(rule, path, content):
    if "path" in rule and not re.search(rule["path"], path):
        return []
    findings = []
    for match in re.finditer(rule["regex"], content):
        secret = match.group(rule.get("secretGroup", 0))
        allowlists = CONFIG.get("allowlists", []) + rule.get("allowlists", [])
        if not any(_allowlisted(item, path, match.group(), secret) for item in allowlists):
            findings.append(secret)
    return findings


POSITIVES = [
    ("printer-device-credential", "plugins/commands/printer.py", '"flashforge_check_code": "A1B2C3D4"'),
    ("printer-device-credential", "plugins/commands/printer.py", 'serial_number = "SYNTHETIC12345"'),
    ("printer-device-credential", "plugins/commands/printer.py", '"flashforge_serial": "SYNTHETIC12345"'),
    ("private-chat-export", "messages.html", "synthetic export"),
    ("private-chat-export", "nested/messages.json", "{}"),
    ("private-chat-export", "nested/telegram-export-01.json", "{}"),
    ("private-chat-export", "nested/chat_export.txt", "synthetic export"),
    ("private-chat-export", "nested/chat.txt", "synthetic export"),
    ("private-chat-export", "nested/result.json", "{}"),
    ("telegram-export-content", "renamed.html", '<div class="message default clearfix">'),
    ("telegram-export-content", "renamed.json", '{"type": "personal_chat"}'),
    ("private-history", "nested/HISTORY.json", "[]"),
    ("private-training-data", "nested/training_data.json", "{}"),
    ("private-vocabulary", "nested/custom-vocabulary-export.json", "{}"),
    ("private-config", "nested/config.json", "{}"),
    ("private-config", "nested/config.json.backup", "{}"),
    ("private-log", "nested/app.log", "synthetic diagnostic"),
    ("personal-windows-path", "source.py", "C:" + "\\Users\\Synthetic\\file.txt"),
    ("personal-windows-path", "source.json", "C:" + "\\\\Users\\\\Synthetic\\\\file.txt"),
    ("personal-windows-path", "source.md", "C:" + "/Users/Synthetic/file.txt"),
]


@pytest.mark.parametrize("rule_id,path,content", POSITIVES)
def test_custom_rule_detects_synthetic_positive(rule_id, path, content):
    assert _findings(RULES[rule_id], path, content)


def test_every_custom_rule_has_positive_coverage_and_defaults_remain_enabled():
    assert set(RULES) == {rule_id for rule_id, _, _ in POSITIVES}
    assert CONFIG["extend"] == {"useDefault": True}
    # Python's regex engine is only an offline proxy. Keep patterns in Go RE2's
    # supported subset: no lookaround or backreferences.
    for rule in RULES.values():
        for field in ("regex", "path"):
            pattern = rule.get(field, "")
            assert not re.search(r"\(\?[=!<]|\\[1-9]", pattern)


FIXTURES = [
    ("tests/test_cloud_gating.py", '"api_key": "sk-real-key"', "sk-real-key"),
    ("tests/test_cloud_gating.py", '"api_key": "sk-test"', "sk-test"),
    ("tests/test_settings.py", "setText('sk-test-key')", "sk-test-key"),
    ("browser_extension/bridge-auth-core.test.js", 'const SECRET = "test-pairing-secret-0123456789-abcdefghijklmnopqrstuvwxyz";',
     "test-pairing-secret-0123456789-abcdefghijklmnopqrstuvwxyz"),
    ("plugins/commands/flashforge_printer.py", '"flashforge_check_code": "EXAMPLE_CODE"', "EXAMPLE_CODE"),
    ("plugins/commands/flashforge_printer.py", '"flashforge_serial": "EXAMPLE_SERIAL"', "EXAMPLE_SERIAL"),
]


@pytest.mark.parametrize("path,content,secret", FIXTURES)
def test_578_fixtures_remain_narrowly_allowlisted(path, content, secret):
    assert secret in (ROOT / path).read_text(encoding="utf-8")
    allowlists = CONFIG["allowlists"] + RULES["printer-device-credential"]["allowlists"]
    assert any(_allowlisted(item, path, content, secret) for item in allowlists)
    assert not any(_findings(rule, path, content) for rule in RULES.values())
    assert not any(_allowlisted(item, "other.py", content, secret) for item in allowlists)
    assert not any(_allowlisted(item, path, content, "SYNTHETIC_NEW_VALUE") for item in allowlists)


@pytest.mark.parametrize("path,content", [
    ("samsara/config_schema.py", 'config = {"enabled": True}'),
    ("tests/fixtures/settings_apply_and_close_snapshot.json", '{}'),
    ("plugins/commands/flashforge_printer.py", "serial = app.config.get('flashforge_serial', '')"),
    ("plugins/commands/flashforge_printer.py", '"flashforge_check_code": "A1B2C3D4"'),
])
def test_nearby_source_and_new_printer_value(path, content):
    hits = [rule["id"] for rule in RULES.values() if _findings(rule, path, content)]
    assert hits == (["printer-device-credential"] if "A1B2C3D4" in content else [])


def test_workflow_scans_push_and_pull_request_without_exposing_reports():
    workflow = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    for required in ("on: [push, pull_request]", "contents: read", "fetch-depth: 0",
                     "docker://ghcr.io/gitleaks/gitleaks:v8.24.2",
                     'git --config=.gitleaks.toml --redact'):
        assert required in workflow
    assert 'upload-artifact' not in workflow
    assert 'GITHUB_TOKEN' not in workflow


def test_workflow_bounds_each_event_and_passes_range_to_scanner():
    workflow = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    for required in (
        'BEFORE: ${{ github.event.before }}',
        'PUSH_HEAD: ${{ github.sha }}',
        'PR_BASE: ${{ github.event.pull_request.base.sha }}',
        'PR_HEAD: ${{ github.event.pull_request.head.sha }}',
        'DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}',
        'if [[ "$EVENT_NAME" == "pull_request" ]]; then\n'
        '            log_opts="$PR_BASE..$PR_HEAD"',
        'elif [[ "$BEFORE" == "0000000000000000000000000000000000000000" ]]; then',
        'git rev-parse --verify "refs/remotes/origin/$DEFAULT_BRANCH"',
        'log_opts="$PUSH_HEAD --not refs/remotes/origin/$DEFAULT_BRANCH"',
        'else\n            log_opts="$BEFORE..$PUSH_HEAD"',
        'echo "log_opts=$log_opts" >> "$GITHUB_OUTPUT"',
        '"--log-opts=${{ steps.range.outputs.log_opts }}"',
        'set -euo pipefail',
    ):
        assert required in workflow
    assert 'baseline' not in workflow.lower()
