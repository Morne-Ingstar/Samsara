"""Downloadable release components: manifest, state, verified fetch (33).

The runtime half of docs/RELEASE_MANIFEST.md, for the installer and for
Settings later. It does four things and nothing else:

    load_manifest(source)            a local path or an https:// URL, validated
    validate_manifest(doc)           every problem, as messages
    component_state(component, root) 'present' | 'missing' | 'unavailable'
    download_component(...)          one component, SHA-256 verified, atomic

No UI, no automatic download, and no network access at import time. The
transport is a plain callable so tests never touch the network.

Verification reuses tools/release_preflight._sha256 (the streaming file
hash the release gate uses) when that module is importable; a frozen build
does not ship tools/, so a byte-identical fallback is kept here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

SCHEMA_VERSION = 1
KINDS = ("core", "acceleration", "model", "voice")
SOURCES = ("built", "pinned")
STATES = ("present", "missing", "unavailable")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

TOP_LEVEL_FIELDS = ("schema_version", "app_version", "release_tag", "build_date", "git_sha", "components")
COMPONENT_FIELDS = (
    "id", "name", "description", "kind", "available", "filename", "url", "size_bytes",
    "sha256", "min_disk_bytes", "install_dir", "requires", "default", "source",
)

PART_SUFFIX = ".part"
SIDECAR_SUFFIX = ".sha256"
DEFAULT_CHUNK = 1 << 20


class ManifestError(ValueError):
    """The manifest is not a valid schema-version-1 document."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class ComponentUnavailable(RuntimeError):
    """The component is declared but not released (available: false)."""


class HashMismatch(RuntimeError):
    """The downloaded bytes do not hash to the manifest's sha256."""


class DownloadError(RuntimeError):
    """The transport failed or returned something unusable."""


# ---------------------------------------------------------------------------
# Hashing (reuse the release gate's helper when it is importable)
# ---------------------------------------------------------------------------

def _sha256_fallback(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(DEFAULT_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path) -> str:
    """Streaming SHA-256 of a file: tools.release_preflight._sha256 when the
    checkout is available, else the identical fallback above."""
    try:
        from tools.release_preflight import _sha256  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - frozen build, or tools/ not on sys.path
        return _sha256_fallback(path)
    return _sha256(Path(path))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_component(component, index: int = 0) -> list:
    """Problems with one component entry (empty list when valid)."""
    where = f"components[{index}]"
    if not isinstance(component, dict):
        return [f"{where}: not an object"]
    problems = []
    for field in COMPONENT_FIELDS:
        if field not in component:
            problems.append(f"{where}: missing field {field!r}")
    if problems:
        return problems
    cid = component["id"]
    where = f"component {cid!r}" if isinstance(cid, str) else where
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        problems.append(f"{where}: id must match [a-z0-9-]+")
    for field in ("name", "description", "install_dir"):
        if not isinstance(component[field], str) or not component[field]:
            problems.append(f"{where}: {field} must be a non-empty string")
    if component["kind"] not in KINDS:
        problems.append(f"{where}: unknown kind {component['kind']!r} (expected one of {', '.join(KINDS)})")
    if component["source"] not in SOURCES:
        problems.append(f"{where}: unknown source {component['source']!r}")
    for field in ("available", "default"):
        if not isinstance(component[field], bool):
            problems.append(f"{where}: {field} must be a boolean")
    if not isinstance(component["filename"], str) or not _FILENAME_RE.match(component["filename"]):
        problems.append(f"{where}: filename must be a plain file name")
    requires = component["requires"]
    if not isinstance(requires, list) or not all(isinstance(r, str) and r for r in requires):
        problems.append(f"{where}: requires must be a list of non-empty strings")

    available = component["available"] is True
    url, size, sha, min_disk = (component["url"], component["size_bytes"],
                                component["sha256"], component["min_disk_bytes"])
    if available:
        if not isinstance(url, str) or urlparse(url).scheme != "https" or not urlparse(url).netloc:
            problems.append(f"{where}: url must be an https:// URL")
        if not _is_int(size) or size <= 0:
            problems.append(f"{where}: size_bytes must be a positive integer")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            problems.append(f"{where}: sha256 must be 64 lowercase hex characters")
        if not _is_int(min_disk) or min_disk < 0:
            problems.append(f"{where}: min_disk_bytes must be a non-negative integer")
        elif _is_int(size) and min_disk < size:
            problems.append(f"{where}: min_disk_bytes ({min_disk}) is smaller than size_bytes ({size})")
    else:
        for field, value in (("url", url), ("size_bytes", size), ("sha256", sha), ("min_disk_bytes", min_disk)):
            if value is not None:
                problems.append(f"{where}: {field} must be null while available is false")
    return problems


def validate_manifest(doc) -> list:
    """Every problem with a manifest document; an empty list means valid."""
    if not isinstance(doc, dict):
        return ["manifest: not an object"]
    problems = []
    for field in TOP_LEVEL_FIELDS:
        if field not in doc:
            problems.append(f"manifest: missing field {field!r}")
    if problems:
        return problems
    if doc["schema_version"] != SCHEMA_VERSION:
        problems.append(f"manifest: schema_version {doc['schema_version']!r} is not {SCHEMA_VERSION}")
    for field in ("app_version", "release_tag", "build_date"):
        if not isinstance(doc[field], str) or not doc[field]:
            problems.append(f"manifest: {field} must be a non-empty string")
    if not isinstance(doc["git_sha"], str) or not _GIT_SHA_RE.match(doc["git_sha"]):
        problems.append("manifest: git_sha must be a 40-hex commit")
    components = doc["components"]
    if not isinstance(components, list) or not components:
        return problems + ["manifest: components must be a non-empty array"]
    seen = set()
    for index, component in enumerate(components):
        problems.extend(validate_component(component, index))
        cid = component.get("id") if isinstance(component, dict) else None
        if isinstance(cid, str):
            if cid in seen:
                problems.append(f"component {cid!r}: duplicate id")
            seen.add(cid)
    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _default_fetch(url: str, timeout: float = 60.0) -> bytes:
    import urllib.request  # noqa: PLC0415 - only when a URL is actually loaded

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (https enforced by caller)
        return resp.read()


def load_manifest(source, *, fetch: Optional[Callable[[str], bytes]] = None) -> dict:
    """Read and validate a manifest from a local path or an https:// URL.
    `fetch(url) -> bytes` is the transport for URLs (tests inject one).
    Raises ManifestError for an invalid document."""
    text = str(source)
    if urlparse(text).scheme in ("http", "https"):
        if urlparse(text).scheme != "https":
            raise ManifestError([f"manifest URL must be https://, got {text}"])
        raw = (fetch or _default_fetch)(text)
    else:
        raw = Path(text).read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError([f"manifest is not valid JSON: {exc}"]) from exc
    problems = validate_manifest(doc)
    if problems:
        raise ManifestError(problems)
    return doc


def get_component(manifest: dict, component_id: str) -> dict:
    for component in manifest["components"]:
        if component["id"] == component_id:
            return component
    raise KeyError(component_id)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def component_path(component: dict, downloads_dir) -> Path:
    return Path(downloads_dir) / component["filename"]


def _read_sidecar(path: Path) -> Optional[str]:
    sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
    try:
        return sidecar.read_text(encoding="ascii").split()[0].lower()
    except (OSError, IndexError, UnicodeDecodeError):
        return None


def component_state(component: dict, downloads_dir) -> str:
    """'unavailable' when the manifest says so; 'present' when the file is on
    disk with the manifest's size and a verified-hash sidecar equal to the
    manifest's sha256 (written only after a verified download); else
    'missing'. Never re-hashes a file on a listing call."""
    if component.get("available") is not True:
        return "unavailable"
    path = component_path(component, downloads_dir)
    try:
        size = path.stat().st_size
    except OSError:
        return "missing"
    if size != component["size_bytes"]:
        return "missing"
    if _read_sidecar(path) != component["sha256"]:
        return "missing"
    return "present"


def list_components(manifest: dict, downloads_dir) -> list:
    """[(component, state)] in manifest order."""
    return [(c, component_state(c, downloads_dir)) for c in manifest["components"]]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class _UrllibResponse:
    """Adapter so the default transport matches the injectable one:
    .status, .headers (case-insensitive get), .read(n), close()."""

    def __init__(self, resp):
        self._resp = resp
        self.status = getattr(resp, "status", 200)
        self.headers = resp.headers

    def read(self, n: int) -> bytes:
        return self._resp.read(n)

    def close(self):
        self._resp.close()


def _default_open(url: str, headers: dict):
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url, headers=headers)
    try:
        return _UrllibResponse(urllib.request.urlopen(req, timeout=60))  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "code", None)
        if status == 416:
            # Requested range not satisfiable: the part file is complete or
            # over-long; the caller restarts from zero.
            return _UrllibResponse(exc)
        raise DownloadError(f"{url}: {exc}") from exc


def _header(headers, name: str) -> Optional[str]:
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    if value is None and hasattr(headers, "items"):
        for key, val in headers.items():
            if key.lower() == name.lower():
                return val
    return value


def download_component(component: dict, downloads_dir, *,
                       progress: Optional[Callable[[int, int], None]] = None,
                       opener: Optional[Callable[[str, dict], object]] = None,
                       chunk_size: int = DEFAULT_CHUNK) -> Path:
    """Download one available component into downloads_dir/<filename>.

    Streams into <filename>.part; a part file left by an earlier attempt is
    resumed with an HTTP Range request (restarted from zero when the server
    answers 200 instead of 206, or 416). progress(done_bytes, total_bytes)
    is called after every chunk. The finished part is hashed; on a mismatch
    it is deleted and HashMismatch is raised; on success it is renamed into
    place and a <filename>.sha256 sidecar records the verified hash. A
    half-written component is never left under the final name.

    opener(url, headers) -> response with .status, .headers, .read(n),
    .close(); the default uses urllib. Tests inject an in-memory one.
    """
    if component.get("available") is not True:
        raise ComponentUnavailable(component.get("id", "?"))
    if urlparse(component["url"]).scheme != "https":
        raise DownloadError(f"refusing non-https url {component['url']!r}")
    open_fn = opener or _default_open
    downloads_dir = Path(downloads_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    final = component_path(component, downloads_dir)
    part = final.with_name(final.name + PART_SUFFIX)
    total = int(component["size_bytes"])
    expected = component["sha256"]

    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}

    if have < total:
        resp = open_fn(component["url"], headers)
        try:
            status = int(getattr(resp, "status", 200))
            if have and status == 206:
                mode = "ab"
            elif status in (200, 416):
                mode = "wb"          # server ignored the range: start over
                have = 0
                if status == 416:
                    resp.close()
                    resp = open_fn(component["url"], {})
                    status = int(getattr(resp, "status", 200))
                    if status != 200:
                        raise DownloadError(f"{component['url']}: HTTP {status}")
            else:
                raise DownloadError(f"{component['url']}: HTTP {status}")
            length = _header(getattr(resp, "headers", {}), "Content-Length")
            if length is not None and have + int(length) != total:
                raise DownloadError(
                    f"{component['id']}: server offers {have + int(length)} bytes, manifest says {total}")
            with open(part, mode) as fh:
                if progress is not None:
                    progress(have, total)
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    have += len(chunk)
                    if have > total:
                        raise DownloadError(f"{component['id']}: server sent more than {total} bytes")
                    if progress is not None:
                        progress(have, total)
        finally:
            resp.close()
        if have != total:
            raise DownloadError(f"{component['id']}: connection ended at {have} of {total} bytes (part kept for resume)")

    actual = sha256_file(part)
    if actual != expected:
        try:
            part.unlink()
        finally:
            pass
        raise HashMismatch(f"{component['id']}: sha256 {actual} != {expected} (partial file deleted)")
    os.replace(part, final)
    final.with_name(final.name + SIDECAR_SUFFIX).write_text(
        f"{expected}  {final.name}\n", encoding="ascii")
    return final


__all__ = [
    "SCHEMA_VERSION", "KINDS", "SOURCES", "STATES", "COMPONENT_FIELDS", "TOP_LEVEL_FIELDS",
    "ManifestError", "ComponentUnavailable", "HashMismatch", "DownloadError",
    "sha256_file", "validate_component", "validate_manifest", "load_manifest", "get_component",
    "component_path", "component_state", "list_components", "download_component",
]
