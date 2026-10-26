"""Offline newcomer-document checks; usable in a plain source export."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (ROOT / "README.md", ROOT / "PRIVACY.md", ROOT / "RELEASING.md",
             *sorted((ROOT / "Docs").rglob("*.md")))
RELEASE_ASSETS = frozenset({
    "SamsaraSetup-0.40.0.exe",
    "SamsaraSetup-0.40.0.exe.sha256",
    "Samsara-Windows-v0.40.0.zip",
    "Samsara-Windows-v0.40.0.zip.sha256",
    "Samsara-GestureControl-v0.40.0.zip",
    "Samsara-GestureControl-v0.40.0.zip.sha256",
    "Samsara-WakeWord-Models-v0.40.0.zip",
    "Samsara-WakeWord-Models-v0.40.0.zip.sha256",
    "manifest.json",
})


def links(text):
    # Code examples are not rendered links. Include reference definitions,
    # autolinks and HTML attributes as well as ordinary Markdown links/images.
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    text = re.sub(r"`[^`\n]+`", "", text)
    patterns = (
        r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)",
        r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)",
        r'(?:href|src)=[\"\x27]([^\"\x27]+)',
        r"<(https?://[^>]+)>",
        r"https?://[^\s<>\[\]()`]+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            yield match.group(1).strip("<>") if match.lastindex else match.group().rstrip(".,;")


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_internal_links_resolve(document):
    missing = []
    for target in links(document.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = unquote(parsed.path)
        resolved = ROOT / path.lstrip("/") if path.startswith("/") else document.parent / path
        if not resolved.exists():
            missing.append(target)
    assert not missing, f"{document.relative_to(ROOT)}: missing targets: {missing}"


def asset_error(target):
    parsed = urlsplit(target)
    prefix = "/Morne-Ingstar/Samsara/releases/download/"
    if parsed.netloc.lower() != "github.com" or not parsed.path.startswith(prefix):
        return None
    tag, _, asset = unquote(parsed.path[len(prefix):]).partition("/")
    # Current download links must target this release's exact assets. Historical
    # filenames in prose (e.g. the separate CUDA pack) are not current links.
    if tag == "v0.40.0" and asset in RELEASE_ASSETS:
        return None
    return f"unexpected release target: {target}"


def test_release_asset_names():
    errors = []
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        errors.extend(error for target in links(text) if (error := asset_error(target)))
        # Check concrete current-release names even when shown as inline code.
        for asset in re.findall(r"Samsara[\w.-]*0\.40\.0[\w.-]*\.(?:exe|zip)(?:\.sha256)?", text):
            if asset not in RELEASE_ASSETS:
                errors.append(f"{document.relative_to(ROOT)}: unknown asset {asset}")
    assert not errors, "\n".join(errors)


def test_readme_does_not_offer_an_old_beta_as_current():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"releases/(?:tag|download)/[^\s)]*beta", text)


@pytest.mark.parametrize("target", [
    "https://github.com/Morne-Ingstar/Samsara/releases/download/v0.40.0/SamsaraSetup-v0.40.0.exe",
    "https://github.com/Morne-Ingstar/Samsara/releases/download/v0.40.0/Samsara-Windows-0.40.0.zip",
    "https://github.com/Morne-Ingstar/Samsara/releases/download/latest/Samsara-Windows-v0.40.0.zip",
])
def test_wrong_download_names_are_rejected(target):
    assert asset_error(target)


@pytest.mark.parametrize("asset", sorted(RELEASE_ASSETS))
def test_exact_release_download_names_are_accepted(asset):
    assert asset_error(f"https://github.com/Morne-Ingstar/Samsara/releases/download/v0.40.0/{asset}") is None
