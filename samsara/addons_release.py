"""Resolve add-ons manifests and bind their hashes to offered update assets."""
import re
import urllib.request

from samsara import components


def _manifest_release_info(tag, archive, filename, opener, timeout):
    # Import at call time: updater exposes the shared types and safe transport.
    from samsara import updater

    try:
        asset_size = int(archive["size"])
        asset_url = str(archive["browser_download_url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise updater.ReleaseMetadataError("The release assets have invalid metadata.") from exc
    updater._validate_release_asset_url(asset_url, tag, filename)
    if not 0 < asset_size <= updater.MAX_DOWNLOAD_BYTES:
        raise updater.ReleaseMetadataError("The update archive has an unsafe declared size.")
    expected = _manifest_checksum(tag, filename, asset_url, asset_size, opener, timeout)
    return updater.ReleaseInfo(tag.removeprefix("v"), tag, asset_size, asset_url, sha256=expected)


def _manifest_checksum(tag, filename, asset_url, asset_size, opener, timeout) -> str:
    from samsara import updater

    def fetch(url):
        # Both versioned and fallback catalogs are fixed assets under addons.
        name = url.rsplit("/", 1)[-1]
        updater._validate_release_asset_url(url, components.ADDONS_TAG, name)
        request = urllib.request.Request(url, headers={"User-Agent": "Samsara updater"})
        with updater._open_response(opener, request, timeout) as response:
            updater._validate_download_redirect(getattr(response, "geturl", lambda: url)(), url)
            return updater._read_bounded(response, updater.MAX_RELEASE_JSON_BYTES)

    try:
        doc = components.load_release_manifest(tag, fetch=fetch)
    except updater.UpdateError:
        raise
    except Exception:
        # PLACEHOLDER COPY (1003): transport/manifest errors can contain private paths or values.
        raise updater.ReleaseMetadataError("Could not read the update manifest. Try again later.") from None
    if doc["app_version"] != tag.removeprefix("v") or doc["release_tag"] != tag:
        raise updater.ReleaseMetadataError("The update manifest belongs to a different version.")
    records = list(doc["components"]) + list(doc.get("release_assets", []))
    matches = [r for r in records if isinstance(r, dict) and r.get("filename") == filename]
    if len(matches) != 1:
        raise updater.ReleaseMetadataError("The update manifest must identify exactly one update asset.")
    record = matches[0]
    if (record.get("url") != asset_url or type(record.get("size_bytes")) is not int
            or record["size_bytes"] != asset_size
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
        raise updater.ReleaseMetadataError("The update manifest asset metadata does not match the release.")
    return record["sha256"]


def _release_checksum(release, filename, opener, timeout) -> str:
    from samsara import updater

    if updater._parse_version(release.tag) >= updater._parse_version("0.40.4-0"):
        if not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
            raise updater.UpdateIntegrityError("The update is missing its manifest SHA-256.")
        return release.sha256
    raw = updater._download_bytes(release.checksum_url, tag=release.tag, filename=filename + ".sha256",
                                  opener=opener, timeout=timeout,
                                  limit=min(updater.MAX_CHECKSUM_BYTES, max(release.checksum_size, 1)))
    return updater._expected_checksum(raw, filename)
