"""Read owned Spotify playlists from a local account data export."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import zipfile


class MusicImportError(ValueError):
    """The selected source is not a readable Spotify playlist export."""


@dataclass(frozen=True)
class ImportedPlaylist:
    name: str
    uri: str
    track_count: int


@dataclass(frozen=True)
class ImportResult:
    playlists: tuple[ImportedPlaylist, ...]
    skipped_not_owned: int = 0
    skipped_without_uri: int = 0
    skipped_invalid: int = 0


@dataclass(frozen=True)
class PlaylistMergeResult:
    library: dict[str, dict[str, str]]
    added_count: int
    already_there: int

    @property
    def summary(self) -> str:
        return f"{self.added_count} playlists added, {self.already_there} already there"


_PLAYLIST_URI = re.compile(r"^spotify:playlist:([A-Za-z0-9]+)$", re.IGNORECASE)
_OWNERSHIP_FIELDS = (
    "isOwned", "is_owned", "owned", "isOwner", "is_owner",
    "ownerIsCurrentUser", "owner_is_current_user",
)
_NOT_OWNED_VALUES = {"false", "0", "no", "followed", "not_owned", "not-owned", "other"}
_FOLLOWED_FIELDS = ("isFollowed", "is_followed", "followed")


def _is_false(value) -> bool:
    return value is False or (isinstance(value, (int, float)) and value == 0) or (
        isinstance(value, str) and value.strip().casefold() in _NOT_OWNED_VALUES
    )


def _is_not_owned(playlist: dict) -> bool:
    if any(field in playlist and _is_false(playlist[field]) for field in _OWNERSHIP_FIELDS):
        return True
    if any(playlist.get(field) is True for field in _FOLLOWED_FIELDS):
        return True
    ownership = playlist.get("ownershipStatus", playlist.get("ownership_status"))
    if isinstance(ownership, str) and ownership.strip().casefold() in _NOT_OWNED_VALUES:
        return True
    owner = playlist.get("owner")
    if isinstance(owner, dict):
        return any(
            field in owner and _is_false(owner[field])
            for field in ("isCurrentUser", "is_current_user", "isOwner", "is_owner")
        )
    return False


def _playlist_documents(data, label: str) -> list[dict]:
    if isinstance(data, list):
        documents = data
    elif isinstance(data, dict) and isinstance(data.get("playlists"), list):
        documents = data["playlists"]
    elif isinstance(data, dict):
        documents = [data]
    else:
        raise MusicImportError(f"Playlist data in {label} has an unsupported shape.")
    return [item for item in documents if isinstance(item, dict)]


def _is_playlist_json(name: str) -> bool:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return base.startswith("playlist") and base.endswith(".json")


def _decode_playlists(raw: bytes, label: str) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MusicImportError(f"Could not read playlist data in {label}.") from exc
    return _playlist_documents(data, label)


def _track_count(value) -> int:
    if isinstance(value, dict):
        value = value.get("items", [])
    return len(value) if isinstance(value, list) else 0


def _read_playlist_documents(source: Path) -> list[dict]:
    if not source.exists():
        raise MusicImportError("The selected Spotify export path does not exist.")

    documents = []
    has_playlist_files = False
    if source.is_dir():
        files = sorted(
            (path for path in source.rglob("*") if path.is_file() and _is_playlist_json(path.name)),
            key=lambda path: str(path).casefold(),
        )
        has_playlist_files = bool(files)
        for path in files:
            try:
                documents.extend(_decode_playlists(path.read_bytes(), path.name))
            except OSError as exc:
                raise MusicImportError(f"Could not read playlist data in {path.name}.") from exc
    elif source.is_file() and zipfile.is_zipfile(source):
        try:
            with zipfile.ZipFile(source) as archive:
                files = sorted(
                    (name for name in archive.namelist() if _is_playlist_json(name)),
                    key=str.casefold,
                )
                has_playlist_files = bool(files)
                for name in files:
                    documents.extend(_decode_playlists(archive.read(name), name))
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise MusicImportError("That Spotify data ZIP could not be read.") from exc
    else:
        raise MusicImportError("Choose a Spotify data ZIP or its extracted folder.")

    if not has_playlist_files:
        raise MusicImportError("No Playlist JSON file was found in that export.")
    return documents


def import_spotify_export(zip_or_folder) -> ImportResult:
    """Read only Playlist*.json files and return unique, owned playlists.

    The export is read in place. No source file is copied or retained.
    """
    source = Path(zip_or_folder)
    documents = _read_playlist_documents(source)
    playlists: dict[str, ImportedPlaylist] = {}
    skipped_not_owned = 0
    skipped_without_uri = 0
    skipped_invalid = 0

    for document in documents:
        if _is_not_owned(document):
            skipped_not_owned += 1
            continue
        raw_uri = document.get("uri")
        if not isinstance(raw_uri, str) or not raw_uri.strip():
            skipped_without_uri += 1
            continue
        match = _PLAYLIST_URI.fullmatch(raw_uri.strip())
        name = document.get("name")
        if match is None or not isinstance(name, str) or not name.strip():
            skipped_invalid += 1
            continue

        uri = f"spotify:playlist:{match.group(1)}"
        item = ImportedPlaylist(name.strip(), uri, _track_count(document.get("items", [])))
        current = playlists.get(uri)
        if current is None:
            playlists[uri] = item
        else:
            playlists[uri] = ImportedPlaylist(
                current.name, uri, max(current.track_count, item.track_count)
            )

    return ImportResult(
        tuple(playlists.values()), skipped_not_owned, skipped_without_uri, skipped_invalid
    )


def _spoken_name_key(name: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", name.casefold(), flags=re.UNICODE))


def _entry_uri(value):
    return value.get("uri") if isinstance(value, dict) else value


def merge_imported_playlists(existing_library, imported: ImportResult) -> PlaylistMergeResult:
    """Merge by Spotify URI, retaining existing spoken names and avoiding collisions."""
    library = {
        name: dict(value) if isinstance(value, dict) else value
        for name, value in (existing_library or {}).items()
    }
    existing_by_uri = {}
    used_names = set()
    for name, value in library.items():
        if isinstance(name, str):
            used_names.add(_spoken_name_key(name))
        uri = _entry_uri(value)
        if isinstance(uri, str):
            existing_by_uri.setdefault(uri, name)

    added = 0
    already_there = 0
    for playlist in imported.playlists:
        if playlist.uri in existing_by_uri:
            already_there += 1
            continue

        candidate = playlist.name.strip() or "Spotify playlist"
        base = candidate
        suffix = 2
        while _spoken_name_key(candidate) in used_names:
            candidate = f"{base} ({suffix})"
            suffix += 1
        library[candidate] = {"uri": playlist.uri, "type": "playlist"}
        used_names.add(_spoken_name_key(candidate))
        existing_by_uri[playlist.uri] = candidate
        added += 1

    return PlaylistMergeResult(library, added, already_there)
