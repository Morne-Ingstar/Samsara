"""Privacy-conscious, verified updates for frozen Windows builds.

Nothing in this module performs network or process activity at import time.
Callers must explicitly call :func:`check_for_update`, then
:func:`prepare_update`, and finally :func:`launch_prepared_update`.
"""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable
from urllib.parse import urlparse

from samsara import __version__
from samsara.paths import samsara_home_dir
from samsara.update_customizations import migrate_update_customizations


GITHUB_RELEASES_API = (
    "https://api.github.com/repos/Morne-Ingstar/Samsara/releases/latest"
)
MAX_RELEASE_JSON_BYTES = 1 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
NETWORK_TIMEOUT_S = 30.0
PRE_DOWNLOAD_MARGIN_BYTES = 512 * 1024 * 1024
PRE_EXTRACT_MARGIN_BYTES = 256 * 1024 * 1024
TRANSIENT_STATUS_MAX_AGE_S = 15 * 60

CUDA_DLL_ALLOWLIST = frozenset(
    {
        "cublas64_12.dll",
        "cublasLt64_12.dll",
        "cudart64_12.dll",
        "cudnn_adv64_9.dll",
        "cudnn_cnn64_9.dll",
        "cudnn_engines_precompiled64_9.dll",
        "cudnn_engines_runtime_compiled64_9.dll",
        "cudnn_graph64_9.dll",
        "cudnn_heuristic64_9.dll",
        "cudnn_ops64_9.dll",
    }
)

_STABLE_VERSION_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})(?:\s+[*]?(.+))?$")
_GITHUB_REDIRECT_HOSTS = frozenset(
    {
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

ProgressCallback = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """Base class for updater failures that should be shown to the user."""


class UpdateNotSupported(UpdateError):
    """The current process must not check for or install updates."""


class ReleaseMetadataError(UpdateError):
    """GitHub returned release metadata that is missing or unsafe."""


class UpdateIntegrityError(UpdateError):
    """A downloaded update could not be cryptographically verified."""


class UnsafeArchiveError(UpdateError):
    """An archive contains a path or member type that is unsafe to extract."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    asset_size: int
    asset_url: str
    checksum_url: str
    checksum_size: int


@dataclass(frozen=True)
class PreparedUpdate:
    version: str
    tag: str
    install_dir: Path
    staged_dir: Path
    rollback_dir: Path
    workspace_dir: Path
    status_path: Path
    helper_path: Path
    executable_name: str = "Samsara.exe"


@dataclass(frozen=True)
class UpdateStatus:
    state: str
    message: str
    tag: str


def is_frozen_build(*, frozen: bool | None = None) -> bool:
    """Return whether this process is a PyInstaller-frozen build."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return bool(frozen)


def update_unavailable_reason(
    *,
    frozen: bool | None = None,
    platform_name: str | None = None,
    home_override: str | None = None,
    disable_override: str | None = None,
) -> str | None:
    """Return why updates are disabled, or ``None`` when they are eligible.

    An explicit ``SAMSARA_HOME_DIR`` identifies smoke tests, first-run
    previews, or other isolated instances. Those processes must never mutate
    the shared frozen installation.
    """
    if disable_override is None:
        disable_override = os.environ.get("SAMSARA_DISABLE_UPDATE_CHECK", "")
    if disable_override.strip().casefold() in {"1", "true", "yes"}:
        return "Updates are disabled by SAMSARA_DISABLE_UPDATE_CHECK."
    if platform_name is None:
        platform_name = os.name
    if platform_name != "nt":
        return "Updates are available only on Windows."
    if not is_frozen_build(frozen=frozen):
        return "Updates are disabled when Samsara runs from source."
    if home_override is None:
        home_override = os.environ.get("SAMSARA_HOME_DIR", "")
    if home_override:
        return "Updates are disabled for isolated Samsara profiles."
    return None


def _require_update_eligible() -> None:
    reason = update_unavailable_reason()
    if reason:
        raise UpdateNotSupported(reason)


def _parse_stable_version(value: str) -> tuple[int, int, int]:
    match = _STABLE_VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise ReleaseMetadataError(
            f"Expected a stable version such as v0.22.1, received {value!r}."
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _clean_parsed_url(url: str):
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReleaseMetadataError("Update metadata contained an invalid URL.") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ReleaseMetadataError("Update metadata contained a non-GitHub URL.")
    return parsed


def _validate_github_api_url(url: str) -> None:
    parsed = _clean_parsed_url(url)
    expected = urlparse(GITHUB_RELEASES_API)
    if (
        parsed.hostname != "api.github.com"
        or parsed.path != expected.path
        or parsed.query
        or parsed.params
    ):
        raise ReleaseMetadataError("The updater accepts only Samsara's GitHub Releases API.")


def _expected_asset_url(tag: str, filename: str) -> str:
    return (
        "https://github.com/Morne-Ingstar/Samsara/releases/download/"
        f"{tag}/{filename}"
    )


def _validate_release_asset_url(url: str, tag: str, filename: str) -> None:
    parsed = _clean_parsed_url(url)
    if parsed.query or parsed.params or url != _expected_asset_url(tag, filename):
        raise ReleaseMetadataError(
            "Update metadata contained an unexpected release asset URL."
        )


def _validate_download_redirect(url: str, original_url: str) -> None:
    if url == original_url:
        return
    parsed = _clean_parsed_url(url)
    if parsed.hostname not in _GITHUB_REDIRECT_HOSTS:
        raise ReleaseMetadataError("The update download redirected outside GitHub.")


def _open_response(opener, request: urllib.request.Request, timeout: float):
    try:
        return opener(request, timeout=timeout)
    except TypeError:
        # Small injected test openers commonly accept only the request.
        return opener(request)


def _read_bounded(response: BinaryIO, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ReleaseMetadataError("The update server response was unexpectedly large.")
    return data


def check_for_update(
    current_version: str = __version__,
    *,
    opener=urllib.request.urlopen,
    api_url: str = GITHUB_RELEASES_API,
    timeout: float = NETWORK_TIMEOUT_S,
) -> ReleaseInfo | None:
    """Query GitHub's latest stable release after an explicit caller action.

    Returns ``None`` when the current build is already current. An unverified
    release (including v0.22.0, which has no checksum asset) is reported as a
    visible :class:`ReleaseMetadataError`, never offered for installation.
    """
    _require_update_eligible()
    current = _parse_stable_version(current_version)
    _validate_github_api_url(api_url)
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Samsara/{current_version} updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _open_response(opener, request, timeout) as response:
            final_url = getattr(response, "geturl", lambda: api_url)()
            _validate_github_api_url(final_url)
            payload = json.loads(_read_bounded(response, MAX_RELEASE_JSON_BYTES))
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not check GitHub for updates: {exc}") from exc

    if not isinstance(payload, dict):
        raise ReleaseMetadataError("GitHub returned invalid release metadata.")
    if payload.get("draft") or payload.get("prerelease"):
        raise ReleaseMetadataError("GitHub's latest release is not a stable release.")
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise ReleaseMetadataError("The release is missing its version tag.")
    latest = _parse_stable_version(tag)
    if latest <= current:
        return None

    zip_name = f"Samsara-Windows-{tag}.zip"
    checksum_name = f"{zip_name}.sha256"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ReleaseMetadataError("The release does not contain downloadable assets.")
    by_name: dict[str, dict] = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            name = asset["name"]
            if name in by_name:
                raise ReleaseMetadataError(f"The release contains duplicate asset {name!r}.")
            by_name[name] = asset
    if zip_name not in by_name or checksum_name not in by_name:
        raise ReleaseMetadataError(
            f"Release {tag} is missing {zip_name} or its required .sha256 file. "
            "Samsara will not install an unverified update."
        )

    archive = by_name[zip_name]
    checksum = by_name[checksum_name]
    try:
        asset_size = int(archive["size"])
        checksum_size = int(checksum["size"])
        asset_url = str(archive["browser_download_url"])
        checksum_url = str(checksum["browser_download_url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseMetadataError("The release assets have invalid metadata.") from exc
    if not 0 < asset_size <= MAX_DOWNLOAD_BYTES:
        raise ReleaseMetadataError("The update archive has an unsafe declared size.")
    if not 0 < checksum_size <= MAX_CHECKSUM_BYTES:
        raise ReleaseMetadataError("The checksum file has an unsafe declared size.")
    _validate_release_asset_url(asset_url, tag, zip_name)
    _validate_release_asset_url(checksum_url, tag, checksum_name)
    return ReleaseInfo(
        version=".".join(str(part) for part in latest),
        tag=tag,
        asset_size=asset_size,
        asset_url=asset_url,
        checksum_url=checksum_url,
        checksum_size=checksum_size,
    )


def _download_bytes(
    url: str,
    *,
    tag: str,
    filename: str,
    opener,
    timeout: float,
    limit: int,
) -> bytes:
    _validate_release_asset_url(url, tag, filename)
    request = urllib.request.Request(url, headers={"User-Agent": "Samsara updater"})
    try:
        with _open_response(opener, request, timeout) as response:
            final_url = getattr(response, "geturl", lambda: url)()
            _validate_download_redirect(final_url, url)
            return _read_bounded(response, limit)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not download an update file: {exc}") from exc


def _expected_checksum(text: bytes, filename: str) -> str:
    try:
        line = text.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise UpdateIntegrityError("The release checksum is not valid ASCII.") from exc
    match = _SHA256_RE.fullmatch(line)
    if not match:
        raise UpdateIntegrityError("The release checksum file is malformed.")
    named_file = match.group(2)
    if named_file is not None and named_file.strip() != filename:
        raise UpdateIntegrityError("The release checksum names a different file.")
    return match.group(1).lower()


def _download_archive(
    release: ReleaseInfo,
    destination: Path,
    expected_sha256: str,
    *,
    opener,
    timeout: float,
    progress_callback: ProgressCallback | None,
) -> None:
    filename = f"Samsara-Windows-{release.tag}.zip"
    _validate_release_asset_url(release.asset_url, release.tag, filename)
    request = urllib.request.Request(
        release.asset_url, headers={"User-Agent": "Samsara updater"}
    )
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with _open_response(opener, request, timeout) as response, destination.open("xb") as out:
            final_url = getattr(response, "geturl", lambda: release.asset_url)()
            _validate_download_redirect(final_url, release.asset_url)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES or downloaded > release.asset_size:
                    raise UpdateIntegrityError(
                        "The update download exceeded its declared size."
                    )
                digest.update(chunk)
                out.write(chunk)
                if progress_callback:
                    progress_callback(downloaded, release.asset_size)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not download the update archive: {exc}") from exc
    if downloaded != release.asset_size:
        raise UpdateIntegrityError(
            f"The update download was incomplete ({downloaded} of {release.asset_size} bytes)."
        )
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise UpdateIntegrityError("The update failed SHA-256 verification.")


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    seen: set[str] = set()
    extracted = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchiveError("The update archive contains too many files.")
            declared = sum(member.file_size for member in members)
            if declared > MAX_EXTRACTED_BYTES:
                raise UnsafeArchiveError("The expanded update would be unexpectedly large.")
            for member in members:
                raw_name = member.filename
                if not raw_name or "\x00" in raw_name:
                    raise UnsafeArchiveError("The update archive contains an invalid path.")
                normalized = raw_name.replace("\\", "/")
                pure = PurePosixPath(normalized)
                if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
                    raise UnsafeArchiveError(
                        f"The update archive contains an unsafe path: {raw_name!r}."
                    )
                if re.match(r"^[A-Za-z]:", normalized):
                    raise UnsafeArchiveError(
                        f"The update archive contains a drive-qualified path: {raw_name!r}."
                    )
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    raise UnsafeArchiveError("The update archive contains a symbolic link.")
                key = normalized.rstrip("/").casefold()
                if key in seen:
                    raise UnsafeArchiveError("The update archive contains duplicate paths.")
                seen.add(key)
                target = destination.joinpath(*pure.parts)
                resolved = target.resolve()
                try:
                    resolved.relative_to(destination_root)
                except ValueError as exc:
                    raise UnsafeArchiveError(
                        f"The update archive escapes its destination: {raw_name!r}."
                    ) from exc
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                if member.flag_bits & 0x1:
                    raise UnsafeArchiveError("Encrypted update archives are not supported.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        extracted += len(chunk)
                        if extracted > MAX_EXTRACTED_BYTES:
                            raise UnsafeArchiveError(
                                "The expanded update exceeded the safe size limit."
                            )
                        output.write(chunk)
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UnsafeArchiveError(f"The update archive could not be safely extracted: {exc}") from exc


def _preserve_cuda_dlls(install_dir: Path, staged_dir: Path) -> None:
    old_dir = install_dir / "_internal" / "ctranslate2"
    new_dir = staged_dir / "_internal" / "ctranslate2"
    if not old_dir.is_dir():
        return
    for filename in CUDA_DLL_ALLOWLIST:
        source = old_dir / filename
        destination = new_dir / filename
        if (
            source.is_file()
            and not source.is_symlink()
            and not destination.exists()
        ):
            new_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _preserved_cuda_size(install_dir: Path) -> int:
    source_dir = install_dir / "_internal" / "ctranslate2"
    total = 0
    for filename in CUDA_DLL_ALLOWLIST:
        source = source_dir / filename
        if source.is_file() and not source.is_symlink():
            total += source.stat().st_size
    return total


def _archive_uncompressed_size(archive: Path) -> int:
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchiveError("The update archive contains too many files.")
            total = sum(member.file_size for member in members)
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UnsafeArchiveError(f"The update archive is invalid: {exc}") from exc
    if total > MAX_EXTRACTED_BYTES:
        raise UnsafeArchiveError("The expanded update would be unexpectedly large.")
    return total


def _require_disk_space(location: Path, required: int, *, disk_usage, phase: str) -> None:
    available = int(disk_usage(location).free)
    if available < required:
        required_mib = (required + 1024 * 1024 - 1) // (1024 * 1024)
        available_mib = available // (1024 * 1024)
        raise UpdateError(
            f"Not enough disk space to {phase} the update: "
            f"{required_mib:,} MiB required, {available_mib:,} MiB available."
        )


def _frozen_install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _system_powershell_path() -> Path:
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError) as exc:
        raise UpdateNotSupported("Could not resolve the Windows System32 directory.") from exc
    if length <= 0 or length >= len(buffer):
        raise UpdateNotSupported("Could not resolve the Windows System32 directory.")
    powershell = (
        Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ).resolve()
    if not powershell.is_absolute() or not powershell.is_file():
        raise UpdateNotSupported("Windows PowerShell was not found in System32.")
    return powershell


def _validate_prepared_update(prepared: PreparedUpdate) -> None:
    install = prepared.install_dir.resolve()
    frozen_install = _frozen_install_dir()
    workspace = prepared.workspace_dir.resolve()
    staged = prepared.staged_dir.resolve()
    rollback = prepared.rollback_dir.resolve()
    updates = (samsara_home_dir() / "updates").resolve()
    if install != frozen_install:
        raise UpdateNotSupported("The prepared update targets a different installation.")
    if prepared.executable_name != "Samsara.exe":
        raise UpdateError("The prepared update has an invalid executable name.")
    if not install.is_dir() or not (install / "Samsara.exe").is_file():
        raise UpdateError("The Samsara installation is no longer valid.")
    if install.is_symlink() or workspace.is_symlink() or staged.is_symlink():
        raise UpdateError("The update paths may not be symbolic links.")
    if (
        workspace.parent != install.parent
        or not workspace.name.startswith(f".{install.name}-update-")
        or staged != workspace / "payload"
        or not staged.is_dir()
        or not (staged / "Samsara.exe").is_file()
    ):
        raise UpdateError("The prepared update staging paths are invalid.")
    if (
        rollback.parent != install.parent
        or not rollback.name.startswith(f".{install.name}-rollback-")
        or rollback.exists()
    ):
        raise UpdateError("The prepared update rollback path is invalid.")
    if prepared.status_path.resolve() != updates / "last_update.json":
        raise UpdateError("The prepared update status path is invalid.")
    helper = prepared.helper_path.resolve()
    if (
        helper.parent != updates
        or not helper.name.startswith(f"install-{prepared.tag}-")
        or helper.suffix.casefold() != ".ps1"
    ):
        raise UpdateError("The prepared update helper path is invalid.")


def _prepared_workspace_is_safe_to_remove(prepared: PreparedUpdate) -> bool:
    """Return whether cleanup is confined to this frozen install's sibling."""
    try:
        install = prepared.install_dir.resolve()
        workspace = prepared.workspace_dir.resolve()
        updates = (samsara_home_dir() / "updates").resolve()
        return (
            install == _frozen_install_dir()
            and workspace.parent == install.parent
            and workspace.name.startswith(f".{install.name}-update-")
            and not workspace.is_symlink()
            and prepared.status_path.resolve() == updates / "last_update.json"
            and prepared.helper_path.resolve().parent == updates
            and prepared.helper_path.name.startswith(f"install-{prepared.tag}-")
            and prepared.helper_path.suffix.casefold() == ".ps1"
        )
    except (OSError, RuntimeError):
        return False


def _cleanup_pre_swap_failure(prepared: PreparedUpdate, message: str) -> None:
    cleanup_error = None
    try:
        if prepared.workspace_dir.exists():
            shutil.rmtree(prepared.workspace_dir)
    except OSError as exc:
        cleanup_error = exc
    try:
        prepared.helper_path.unlink(missing_ok=True)
    except OSError as exc:
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        _write_status(
            prepared.status_path,
            "cleanup_pending",
            f"{message} Cleanup will be retried: {cleanup_error}",
            prepared.tag,
            prepared,
        )
    else:
        _write_status(prepared.status_path, "failed", message, prepared.tag)


def prepare_update(
    release: ReleaseInfo,
    install_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    *,
    opener=urllib.request.urlopen,
    timeout: float = NETWORK_TIMEOUT_S,
    disk_usage=shutil.disk_usage,
) -> PreparedUpdate:
    """Download, verify, and safely stage an update beside the install."""
    _require_update_eligible()
    _parse_stable_version(release.tag)
    if release.version != release.tag.lstrip("v"):
        raise ReleaseMetadataError("The update version and tag do not agree.")
    if install_dir is None:
        install = _frozen_install_dir()
    else:
        install = Path(install_dir).resolve()
    if not install.is_dir() or not (install / "Samsara.exe").is_file():
        raise UpdateNotSupported("The Samsara installation directory is invalid.")

    cuda_size = _preserved_cuda_size(install)
    pre_download_required = (
        release.asset_size
        + max(release.asset_size * 2, PRE_DOWNLOAD_MARGIN_BYTES)
        + cuda_size
    )
    _require_disk_space(
        install.parent,
        pre_download_required,
        disk_usage=disk_usage,
        phase="download and stage",
    )

    suffix = f"{release.tag}-{uuid.uuid4().hex[:10]}"
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{install.name}-update-{suffix}-", dir=install.parent)
    ).resolve()
    staged = workspace / "payload"
    rollback = install.parent / f".{install.name}-rollback-{suffix}"
    zip_name = f"Samsara-Windows-{release.tag}.zip"
    archive = workspace / zip_name
    updates_dir = samsara_home_dir() / "updates"
    prepared = PreparedUpdate(
        version=release.version,
        tag=release.tag,
        install_dir=install,
        staged_dir=staged,
        rollback_dir=rollback,
        workspace_dir=workspace,
        status_path=updates_dir / "last_update.json",
        helper_path=updates_dir / f"install-{release.tag}-{uuid.uuid4().hex[:10]}.ps1",
    )
    try:
        checksum_bytes = _download_bytes(
            release.checksum_url,
            tag=release.tag,
            filename=f"{zip_name}.sha256",
            opener=opener,
            timeout=timeout,
            limit=min(MAX_CHECKSUM_BYTES, max(release.checksum_size, 1)),
        )
        expected = _expected_checksum(checksum_bytes, zip_name)
        _download_archive(
            release,
            archive,
            expected,
            opener=opener,
            timeout=timeout,
            progress_callback=progress_callback,
        )
        uncompressed_size = _archive_uncompressed_size(archive)
        _require_disk_space(
            workspace,
            uncompressed_size + cuda_size + PRE_EXTRACT_MARGIN_BYTES,
            disk_usage=disk_usage,
            phase="extract",
        )
        _safe_extract_zip(archive, staged)
        archive.unlink()
        if not (staged / "Samsara.exe").is_file():
            raise UnsafeArchiveError("The verified update does not contain Samsara.exe.")
        _preserve_cuda_dlls(install, staged)
        migrate_update_customizations(
            install,
            staged,
            updates_dir / "backups",
        )
        updates_dir.mkdir(parents=True, exist_ok=True)
        return prepared
    except Exception as exc:
        try:
            if workspace.exists():
                shutil.rmtree(workspace)
        except OSError as cleanup_exc:
            try:
                _write_status(
                    prepared.status_path,
                    "cleanup_pending",
                    f"Update preparation failed; cleanup will be retried: {cleanup_exc}",
                    prepared.tag,
                    prepared,
                )
            except OSError:
                pass
            exc.add_note(f"Staged update cleanup also failed: {cleanup_exc}")
        raise


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# Shared by both generated PowerShell templates (_helper_script and
# _cleanup_retry_script). Inserted verbatim into an f-string as a plain
# string value (not re-parsed as an f-string), so it uses single braces.
#
# Checks every existing path component from the expected installation
# parent down to -- and including -- the target itself, not just the leaf.
# A one-shot up-front validation (Assert-SafeUpdatePaths) is not enough:
# the waits between it and the eventual Remove-Item/Move-Item can run
# minutes, long enough for a reparse point to be planted at any level
# under the parent, not only at the final path.
_ASSERT_NOT_REPARSE_POINT_PS = """function Assert-NotReparsePoint([string]$path, [string]$expectedParent) {
    $parentFull = [IO.Path]::GetFullPath($expectedParent)
    $targetFull = [IO.Path]::GetFullPath($path)
    $components = @($parentFull)
    $cursor = $targetFull
    $chain = @()
    while ($cursor -and -not [string]::Equals($cursor, $parentFull, [StringComparison]::OrdinalIgnoreCase)) {
        $chain = ,$cursor + $chain
        $next = [IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrEmpty($next) -or [string]::Equals($next, $cursor, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $cursor = $next
    }
    $components += $chain
    foreach ($component in $components) {
        if (Test-Path -LiteralPath $component) {
            $item = Get-Item -LiteralPath $component -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to modify a reparse point: $component"
            }
        }
    }
}"""


def _helper_script(prepared: PreparedUpdate, current_pid: int) -> str:
    install = _ps_literal(prepared.install_dir)
    staged = _ps_literal(prepared.staged_dir)
    rollback = _ps_literal(prepared.rollback_dir)
    workspace = _ps_literal(prepared.workspace_dir)
    status = _ps_literal(prepared.status_path)
    executable = _ps_literal(prepared.executable_name)
    tag = _ps_literal(prepared.tag)
    workspace_prefix = _ps_literal(f".{prepared.install_dir.name}-update-")
    rollback_prefix = _ps_literal(f".{prepared.install_dir.name}-rollback-")
    return f"""$ErrorActionPreference = 'Stop'
$install = {install}
$staged = {staged}
$rollback = {rollback}
$workspace = {workspace}
$status = {status}
$executable = {executable}
$tag = {tag}
$workspacePrefix = {workspace_prefix}
$rollbackPrefix = {rollback_prefix}
$installParent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($install))
$pathsValidated = $false

function Write-UpdateStatus([string]$state, [string]$message) {{
    $data = @{{
        state=$state; message=$message; tag=$tag;
        install_dir=$install; staged_dir=$staged; rollback_dir=$rollback;
        workspace_dir=$workspace; helper_path=$PSCommandPath;
        updated_at=(Get-Date).ToUniversalTime().ToString('o')
    }}
    $temporary = "$status.tmp"
    $data | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $status -Force
}}

function Show-UpdateFailure([string]$message) {{
    if ($env:SAMSARA_UPDATE_TEST_NO_DIALOG -eq '1') {{ return }}
    try {{
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $message,
            'Samsara Update Failed',
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }} catch {{}}
}}

{_ASSERT_NOT_REPARSE_POINT_PS}

function Assert-SafeUpdatePaths {{
    $installFull = [IO.Path]::GetFullPath($install)
    $installParent = [IO.Path]::GetDirectoryName($installFull)
    $workspaceFull = [IO.Path]::GetFullPath($workspace)
    $stagedFull = [IO.Path]::GetFullPath($staged)
    $rollbackFull = [IO.Path]::GetFullPath($rollback)
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($workspaceFull), $installParent, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The staging directory is no longer beside the installation.'
    }}
    if (-not [IO.Path]::GetFileName($workspaceFull).StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The staging directory name is invalid.'
    }}
    if (-not [string]::Equals($stagedFull, [IO.Path]::Combine($workspaceFull, 'payload'), [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The staged payload path is invalid.'
    }}
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($rollbackFull), $installParent, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The rollback directory is no longer beside the installation.'
    }}
    if (-not [IO.Path]::GetFileName($rollbackFull).StartsWith($rollbackPrefix, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The rollback directory name is invalid.'
    }}
    foreach ($required in @($installFull, $workspaceFull, $stagedFull)) {{
        $item = Get-Item -LiteralPath $required -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{
            throw 'An update directory became a reparse point.'
        }}
    }}
    $script:pathsValidated = $true
    if (-not (Test-Path -LiteralPath (Join-Path $installFull 'Samsara.exe') -PathType Leaf)) {{
        throw 'The installed Samsara.exe is missing.'
    }}
    if (-not (Test-Path -LiteralPath (Join-Path $stagedFull 'Samsara.exe') -PathType Leaf)) {{
        throw 'The staged Samsara.exe is missing.'
    }}
    if (Test-Path -LiteralPath $rollbackFull) {{ throw 'The rollback directory already exists.' }}
}}

function Complete-PreSwapFailure([string]$message) {{
    if ($pathsValidated) {{
        try {{
            if (Test-Path -LiteralPath $workspace) {{
                Assert-NotReparsePoint $workspace $installParent
                Remove-Item -LiteralPath $workspace -Recurse -Force -ErrorAction Stop
            }}
            Write-UpdateStatus 'failed' $message
            return
        }} catch {{
            Write-UpdateStatus 'cleanup_pending' "$message Cleanup will be retried: $($_.Exception.Message)"
            return
        }}
    }}
    Write-UpdateStatus 'failed' $message
}}

try {{
    Assert-SafeUpdatePaths
}} catch {{
    Complete-PreSwapFailure "Update path validation failed: $($_.Exception.Message)"
    Show-UpdateFailure "The update was cancelled because its files or paths changed unexpectedly.`n`n$($_.Exception.Message)"
    exit 5
}}

Write-UpdateStatus 'waiting' 'Waiting for Samsara to close.'
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Process -Id {int(current_pid)} -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {{
    Start-Sleep -Milliseconds 250
}}
if (Get-Process -Id {int(current_pid)} -ErrorAction SilentlyContinue) {{
    Complete-PreSwapFailure 'Samsara did not close before the update timeout.'
    Show-UpdateFailure 'Samsara did not close in time. The update was not installed.'
    exit 1
}}

$oldMoved = $false
$newProcess = $null
try {{
    Assert-NotReparsePoint $install $installParent
    Move-Item -LiteralPath $install -Destination $rollback
    $oldMoved = $true
    Move-Item -LiteralPath $staged -Destination $install
    Write-UpdateStatus 'awaiting_confirmation' 'The update was installed; waiting for Samsara to finish starting.'
    $newProcess = Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -PassThru
    $confirmDeadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $confirmDeadline) {{
        try {{
            $currentStatus = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
            if (($currentStatus.state -eq 'installed') -or ($currentStatus.state -eq 'reported')) {{
                $cleanupErrors = @()
                try {{ Assert-NotReparsePoint $rollback $installParent; Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction Stop }} catch {{ $cleanupErrors += $_.Exception.Message }}
                try {{ Assert-NotReparsePoint $workspace $installParent; Remove-Item -LiteralPath $workspace -Recurse -Force -ErrorAction Stop }} catch {{ $cleanupErrors += $_.Exception.Message }}
                if ($cleanupErrors.Count -gt 0) {{
                    Write-UpdateStatus 'cleanup_pending' "The update succeeded, but old update files still need cleanup: $($cleanupErrors -join '; ')"
                }}
                exit 0
            }}
        }} catch {{}}
        if ($newProcess.HasExited) {{
            throw 'The updated Samsara closed before startup completed.'
        }}
        Start-Sleep -Milliseconds 500
        $newProcess.Refresh()
    }}
    throw 'The updated Samsara did not confirm a healthy startup within 180 seconds.'
}} catch {{
    $problem = $_.Exception.Message
    if ($oldMoved) {{
        try {{
            if (($null -ne $newProcess) -and (-not $newProcess.HasExited)) {{
                Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
                $stopDeadline = (Get-Date).AddSeconds(10)
                while ((Get-Process -Id $newProcess.Id -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $stopDeadline)) {{
                    Start-Sleep -Milliseconds 100
                }}
            }}
            if (Test-Path -LiteralPath $install) {{
                Assert-NotReparsePoint $install $installParent
                Remove-Item -LiteralPath $install -Recurse -Force
            }}
            Assert-NotReparsePoint $rollback $installParent
            Move-Item -LiteralPath $rollback -Destination $install
            Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install
            try {{
                if (Test-Path -LiteralPath $workspace) {{
                    Assert-NotReparsePoint $workspace $installParent
                    Remove-Item -LiteralPath $workspace -Recurse -Force -ErrorAction Stop
                }}
                Write-UpdateStatus 'rolled_back' "The update failed and the previous version was restored: $problem"
            }} catch {{
                Write-UpdateStatus 'cleanup_pending' "The update failed and the previous version was restored. Leftover update files still need cleanup: $($_.Exception.Message)"
            }}
            Show-UpdateFailure "The update failed, so Samsara restored and relaunched the previous version.`n`n$problem"
            exit 2
        }} catch {{
            Write-UpdateStatus 'failed' "Update and rollback both failed: $problem / $($_.Exception.Message)"
            Show-UpdateFailure "The update and automatic rollback both failed. Samsara may need to be downloaded again.`n`n$problem"
            exit 3
        }}
    }}
    try {{
        Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install
    }} catch {{}}
    Complete-PreSwapFailure "The update failed before replacement: $problem"
    Show-UpdateFailure "The update could not replace Samsara. The existing installation was left in place.`n`n$problem"
    exit 4
}}
"""


def _write_status(
    path: Path,
    state: str,
    message: str,
    tag: str,
    prepared: PreparedUpdate | None = None,
    extra_fields: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "state": state,
        "message": message,
        "tag": tag,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if prepared is not None:
        payload.update(
            {
                "install_dir": str(prepared.install_dir),
                "staged_dir": str(prepared.staged_dir),
                "rollback_dir": str(prepared.rollback_dir),
                "workspace_dir": str(prepared.workspace_dir),
                "helper_path": str(prepared.helper_path),
            }
        )
    if extra_fields is not None:
        payload.update(extra_fields)
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def launch_prepared_update(
    prepared: PreparedUpdate,
    current_pid: int = os.getpid(),
    *,
    process_runner=subprocess.Popen,
) -> None:
    """Write and detach the swap/rollback helper for a prepared update."""
    _require_update_eligible()
    try:
        _validate_prepared_update(prepared)
    except Exception as exc:
        if _prepared_workspace_is_safe_to_remove(prepared):
            try:
                _cleanup_pre_swap_failure(
                    prepared, f"The prepared update failed validation: {exc}"
                )
            except OSError:
                pass
        raise
    try:
        powershell = _system_powershell_path()
        prepared.helper_path.parent.mkdir(parents=True, exist_ok=True)
        prepared.helper_path.write_text(
            _helper_script(prepared, current_pid), encoding="utf-8-sig"
        )
        _write_status(
            prepared.status_path,
            "ready",
            "The verified update is ready; Samsara will close and relaunch.",
            prepared.tag,
            prepared,
        )
        creationflags = 0
        for flag_name in (
            "DETACHED_PROCESS",
            "CREATE_NEW_PROCESS_GROUP",
            "CREATE_NO_WINDOW",
        ):
            creationflags |= int(getattr(subprocess, flag_name, 0))
        process_runner(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(prepared.helper_path),
            ],
            cwd=str(prepared.helper_path.parent),
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as exc:
        message = f"Could not launch the update installer: {exc}"
        try:
            _cleanup_pre_swap_failure(prepared, message)
        except OSError as cleanup_exc:
            exc.add_note(f"Could not record update cleanup status: {cleanup_exc}")
        raise UpdateError(f"Could not launch the update installer: {exc}") from exc


def _validated_recorded_path(
    value: object, parent: Path, required_prefix: str
) -> Path:
    if not isinstance(value, str) or not value:
        raise UpdateError("The previous update status is missing a cleanup path.")
    candidate = Path(value).resolve()
    if candidate.parent != parent or not candidate.name.startswith(required_prefix):
        raise UpdateError("The previous update status contains an unsafe cleanup path.")
    return candidate


_LEFTOVER_PATH_KEYS = ("install_dir", "rollback_dir", "workspace_dir")


def _leftover_update_dirs(payload: dict) -> tuple[Path, Path, Path] | None:
    """Extract a validated (workspace, rollback, install) triple from an
    ``installed``-state status payload, if the payload carries one.

    Returns ``None`` only when the payload predates path persistence --
    i.e. *none* of install_dir/rollback_dir/workspace_dir are present.
    Callers must treat that, and only that, as "nothing to check, this is a
    legacy status" rather than an error.

    If *any* of those fields are present, all three are required and must
    resolve to safe, validated paths beside this installation; this raises
    :class:`UpdateError` on a malformed, incomplete, tampered, or unsafe
    subset instead of silently swallowing it -- callers must not treat that
    case the same as "nothing to check", or a corrupted status could hide
    orphaned update directories forever.
    """
    present = [key for key in _LEFTOVER_PATH_KEYS if payload.get(key)]
    if not present:
        return None
    missing = [key for key in _LEFTOVER_PATH_KEYS if key not in present]
    if missing:
        raise UpdateError(
            "The update status has some but not all required cleanup path "
            f"fields (missing: {', '.join(missing)})."
        )
    install = _frozen_install_dir()
    recorded_install = Path(str(payload.get("install_dir", ""))).resolve()
    if recorded_install != install:
        raise UpdateError("The update status belongs to a different installation.")
    workspace = _validated_recorded_path(
        payload.get("workspace_dir"), install.parent, f".{install.name}-update-"
    )
    rollback = _validated_recorded_path(
        payload.get("rollback_dir"), install.parent, f".{install.name}-rollback-"
    )
    if workspace.is_symlink() or rollback.is_symlink():
        raise UpdateError("An update cleanup path became a symbolic link.")
    return workspace, rollback, install


def _quarantine_status(path: Path, reason: str) -> UpdateStatus:
    quarantine = path.with_name(
        f"{path.stem}.invalid-{int(time.time())}-{uuid.uuid4().hex[:8]}{path.suffix}"
    )
    try:
        os.replace(path, quarantine)
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            reason = (
                f"{reason} The invalid status could not be quarantined or consumed: "
                f"{exc}; {unlink_exc}"
            )
    return UpdateStatus("failed", reason, "unknown")


def _status_is_stale(payload: dict, now: float) -> bool:
    value = payload.get("updated_at")
    if not isinstance(value, str):
        return True
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = now - stamp.timestamp()
    except (ValueError, OverflowError):
        return True
    return age < -300 or age > TRANSIENT_STATUS_MAX_AGE_S


def _cleanup_retry_script(
    workspace: Path,
    rollback: Path,
    install: Path,
    status: Path,
    tag: str,
) -> str:
    return f"""$ErrorActionPreference = 'Stop'
$workspace = {_ps_literal(workspace)}
$rollback = {_ps_literal(rollback)}
$install = {_ps_literal(install)}
$expectedParent = {_ps_literal(install.parent)}
$workspaceName = {_ps_literal(workspace.name)}
$rollbackName = {_ps_literal(rollback.name)}
$status = {_ps_literal(status)}
$tag = {_ps_literal(tag)}
function Write-CleanupStatus([string]$state, [string]$message) {{
    $data = @{{state=$state; message=$message; tag=$tag; updated_at=(Get-Date).ToUniversalTime().ToString('o')}}
    $temporary = "$status.tmp"
    $data | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $status -Force
}}
{_ASSERT_NOT_REPARSE_POINT_PS}
try {{
    $workspaceFull = [IO.Path]::GetFullPath($workspace)
    $rollbackFull = [IO.Path]::GetFullPath($rollback)
    $installFull = [IO.Path]::GetFullPath($install)
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($workspaceFull), [IO.Path]::GetFullPath($expectedParent), [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The cleanup directory is no longer beside the installation.'
    }}
    if (-not [string]::Equals([IO.Path]::GetDirectoryName($rollbackFull), [IO.Path]::GetFullPath($expectedParent), [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The rollback cleanup directory is no longer beside the installation.'
    }}
    if (-not [string]::Equals([IO.Path]::GetFileName($workspaceFull), $workspaceName, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The cleanup directory name changed.'
    }}
    if (-not [string]::Equals([IO.Path]::GetFileName($rollbackFull), $rollbackName, [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'The rollback cleanup directory name changed.'
    }}
    if (-not (Test-Path -LiteralPath $installFull -PathType Container)) {{
        throw 'The active Samsara installation is missing; cleanup was cancelled.'
    }}
    foreach ($candidate in @($rollbackFull, $workspaceFull)) {{
        if (Test-Path -LiteralPath $candidate) {{
            Assert-NotReparsePoint $candidate $expectedParent
            Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction Stop
        }}
    }}
    Write-CleanupStatus 'cleanup_complete' 'Leftover update files were removed.'
}} catch {{
    Write-CleanupStatus 'cleanup_pending' "Leftover update cleanup will be retried: $($_.Exception.Message)"
}}
"""


def _launch_cleanup_retry(
    workspace: Path,
    rollback: Path,
    install: Path,
    status_path: Path,
    tag: str,
    *,
    process_runner,
) -> None:
    updates = status_path.parent
    helper = updates / f"cleanup-{uuid.uuid4().hex[:10]}.ps1"
    helper.write_text(
        _cleanup_retry_script(workspace, rollback, install, status_path, tag),
        encoding="utf-8-sig",
    )
    creationflags = 0
    for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        creationflags |= int(getattr(subprocess, flag_name, 0))
    process_runner(
        [
            str(_system_powershell_path()),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(helper),
        ],
        cwd=str(updates),
        close_fds=True,
        creationflags=creationflags,
    )


def reconcile_update_on_startup(
    *, process_runner=subprocess.Popen, now: float | None = None
) -> UpdateStatus | None:
    """Confirm a newly started update and surface prior helper failures.

    This is intentionally network-free. Call it only once the frozen app has
    reached a healthy, user-visible startup point (normally tray creation).
    The first healthy startup writes the helper's ``installed`` handshake.
    Results are consumed once on a later call so the tray does not repeat the
    same notification forever. Large cleanup remains in the detached helper.
    """
    if update_unavailable_reason() is not None:
        return None
    status_path = samsara_home_dir() / "updates" / "last_update.json"
    if not status_path.is_file():
        return None
    try:
        raw = status_path.read_bytes()
        if len(raw) > 64 * 1024:
            raise UpdateError("The previous update status is unexpectedly large.")
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("status is not an object")
        state = str(payload.get("state", "failed"))
        message = str(payload.get("message", "The previous update did not report a result."))
        tag = str(payload.get("tag", "unknown"))
    except Exception as exc:
        return _quarantine_status(
            status_path, f"Could not read the previous update result: {exc}"
        )

    status = UpdateStatus(state, message, tag)
    known_states = {
        "ready",
        "waiting",
        "awaiting_confirmation",
        "installed",
        "failed",
        "rolled_back",
        "reported",
        "cleanup_pending",
        "cleanup_complete",
    }
    if state not in known_states:
        return _quarantine_status(status_path, f"Unknown update status {state!r}.")
    if now is None:
        now = time.time()
    if state in {"ready", "waiting", "awaiting_confirmation"} and _status_is_stale(
        payload, now
    ):
        return _quarantine_status(
            status_path, f"A stale {state!r} update status was quarantined."
        )
    if state == "reported":
        try:
            status_path.unlink()
        except OSError:
            pass
        return None
    if state == "cleanup_complete":
        _write_status(status_path, "reported", message, tag)
        return UpdateStatus(state, message, tag)
    if state == "cleanup_pending":
        install = _frozen_install_dir()
        try:
            recorded_install = Path(str(payload.get("install_dir", ""))).resolve()
            if recorded_install != install:
                raise UpdateError("The cleanup status belongs to another installation.")
            workspace = _validated_recorded_path(
                payload.get("workspace_dir"),
                install.parent,
                f".{install.name}-update-",
            )
            rollback = _validated_recorded_path(
                payload.get("rollback_dir"),
                install.parent,
                f".{install.name}-rollback-",
            )
            if workspace.is_symlink() or rollback.is_symlink():
                raise UpdateError("An update cleanup path became a symbolic link.")
        except Exception as exc:
            return _quarantine_status(
                status_path, f"Unsafe pending cleanup status: {exc}"
            )
        if not workspace.exists() and not rollback.exists():
            completed = UpdateStatus(
                "cleanup_complete", "Leftover update files were removed.", tag
            )
            _write_status(status_path, "reported", completed.message, tag)
            return completed
        try:
            _launch_cleanup_retry(
                workspace,
                rollback,
                install,
                status_path,
                tag,
                process_runner=process_runner,
            )
        except Exception as exc:
            return UpdateStatus(
                "cleanup_pending",
                f"Leftover update cleanup is still pending: {exc}",
                tag,
            )
        return status
    if state == "installed":
        # The status string alone cannot tell "the detached helper already
        # cleaned up" apart from "the helper died before it got the chance"
        # -- both leave state == "installed" forever, since the helper only
        # writes a further status on cleanup *failure*. Check the recorded
        # directories' actual existence (cheap stat calls; never a
        # synchronous multi-gigabyte delete on this Qt-thread call) before
        # trusting that nothing was orphaned. A malformed/tampered *subset*
        # of the path fields (as opposed to none of them, which is just a
        # legacy status predating this check) must never be silently
        # treated as "nothing to check" -- that would hide a corrupted
        # status and let its update directories leak unnoticed.
        try:
            leftover = _leftover_update_dirs(payload)
        except Exception as exc:
            return _quarantine_status(
                status_path,
                f"The previous update's status had unsafe or malformed "
                f"cleanup path fields: {exc}",
            )
        if leftover is not None:
            workspace, rollback, install = leftover
            if workspace.exists() or rollback.exists():
                pending_message = (
                    f"{message} Leftover update files from a previous "
                    "session still need cleanup."
                )
                extra = {
                    "install_dir": str(install),
                    "rollback_dir": str(rollback),
                    "workspace_dir": str(workspace),
                }
                try:
                    _launch_cleanup_retry(
                        workspace, rollback, install, status_path, tag,
                        process_runner=process_runner,
                    )
                except Exception as exc:
                    pending_message = f"Leftover update cleanup is still pending: {exc}"
                _write_status(
                    status_path, "cleanup_pending", pending_message, tag,
                    extra_fields=extra,
                )
                return UpdateStatus("cleanup_pending", pending_message, tag)
        _write_status(status_path, "reported", message, tag)
        return None
    if state in {"failed", "rolled_back"}:
        _write_status(status_path, "reported", message, tag)
        return status
    if state != "awaiting_confirmation":
        return status

    install = _frozen_install_dir()
    try:
        recorded_install = Path(str(payload.get("install_dir", ""))).resolve()
        if recorded_install != install:
            raise UpdateError("The update status belongs to a different installation.")
        rollback = _validated_recorded_path(
            payload.get("rollback_dir"), install.parent, f".{install.name}-rollback-"
        )
        workspace = _validated_recorded_path(
            payload.get("workspace_dir"), install.parent, f".{install.name}-update-"
        )
        installed = UpdateStatus(
            "installed", "The update was installed successfully.", tag
        )
        # This is the health handshake consumed by the waiting helper. Never
        # remove the potentially multi-gigabyte rollback on the Qt startup
        # thread; the detached helper performs that cleanup after observing
        # this atomic status transition. The path fields are carried forward
        # (not dropped) so that if the helper dies before finishing that
        # cleanup, a later reconcile call (see the "installed" branch above)
        # can still find and retry it instead of leaking the directories.
        _write_status(
            status_path, installed.state, installed.message, installed.tag,
            extra_fields={
                "install_dir": str(install),
                "rollback_dir": str(rollback),
                "workspace_dir": str(workspace),
            },
        )
        return installed
    except Exception as exc:
        return _quarantine_status(
            status_path,
            f"Samsara started with the update, but status validation failed: {exc}",
        )


__all__ = [
    "CUDA_DLL_ALLOWLIST",
    "PreparedUpdate",
    "ReleaseInfo",
    "ReleaseMetadataError",
    "UnsafeArchiveError",
    "UpdateError",
    "UpdateIntegrityError",
    "UpdateNotSupported",
    "UpdateStatus",
    "check_for_update",
    "is_frozen_build",
    "launch_prepared_update",
    "prepare_update",
    "reconcile_update_on_startup",
    "update_unavailable_reason",
]
