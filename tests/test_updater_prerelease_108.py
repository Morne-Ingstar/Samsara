"""Queue 108/F10: a prerelease build can see an update.

samsara/__init__.py declares 0.23.0-beta.1. Before 108 check_for_update fed
that straight to the stable-only parser, so the beta raised
ReleaseMetadataError before any network request and could see NO update at
all -- including the stable release that supersedes it. Every tester on the
beta was silently cut off from updating.

Two decisions live in check_for_update and 108 separates them:

  * what this build IS      -- prereleases accepted (_parse_version)
  * what may be OFFERED     -- stable only, unless allow_prerelease=True

The second stays False by default. That is the intent, not an oversight: a
beta tester should be moved onto the stable release that supersedes their
build, not sideways onto another beta.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import samsara
from samsara import updater


# ---------------------------------------------------------------------------
# Helpers -- a GitHub payload and an opener, no network
# ---------------------------------------------------------------------------

#: Must be the real repo: updater._validate_release_asset_url pins the host
#: and path, which is a separate protection 108 does not touch.
_DOWNLOADS = "https://github.com/Morne-Ingstar/Samsara/releases/download"


def _release_payload(tag: str, *, prerelease: bool = False) -> dict:
    zip_name = f"Samsara-Windows-{tag}.zip"
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {"name": zip_name, "size": 123,
             "browser_download_url": f"{_DOWNLOADS}/{tag}/{zip_name}"},
            {"name": f"{zip_name}.sha256", "size": 96,
             "browser_download_url": f"{_DOWNLOADS}/{tag}/{zip_name}.sha256"},
        ],
    }


class _Response:
    def __init__(self, body: bytes, url: str):
        self._body, self._url = body, url

    def read(self, n=-1):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload: dict, calls: list):
    def open_it(request, timeout=None):
        url = getattr(request, "full_url", request)
        calls.append(url)
        return _Response(json.dumps(payload).encode("utf-8"), url)
    return open_it


@pytest.fixture(autouse=True)
def eligible(monkeypatch):
    """These tests run from source, where updates are disabled outright.
    The gate itself is covered in tests/test_updater.py; here it is the
    version comparison that is under test."""
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

def test_the_shipped_version_is_a_prerelease():
    """If this ever stops being true the bug stops being reachable, and this
    file should be read again rather than trusted."""
    assert "-" in samsara.__version__, samsara.__version__
    assert updater._parse_version(samsara.__version__)


@pytest.mark.parametrize("lower, higher", [
    ("0.23.0-beta.1", "0.23.0"),          # the whole point: stable supersedes the beta
    ("0.23.0-beta.1", "0.23.0-beta.2"),
    ("0.23.0-alpha.9", "0.23.0-beta.1"),  # alphanumeric identifiers compare as text
    ("0.23.0-beta.2", "0.23.0-beta.10"),  # numeric identifiers compare as numbers
    ("0.22.1", "0.23.0-beta.1"),          # a prerelease of a later version is still later
    ("0.23.0", "0.23.1"),
    ("v0.23.0-rc.1", "v0.23.0"),          # a leading v is accepted on either side
])
def test_semver_precedence(lower, higher):
    assert updater._parse_version(lower) < updater._parse_version(higher)


def test_build_metadata_is_ignored_for_precedence():
    assert updater._parse_version("0.23.0-beta.1") == updater._parse_version("0.23.0-beta.1+g1234abc")


@pytest.mark.parametrize("bad", ["", "0.23", "not-a-version", "0.23.0.1", "v0.23.0-"])
def test_a_version_that_is_not_a_version_is_still_rejected(bad):
    with pytest.raises(updater.ReleaseMetadataError):
        updater._parse_version(bad)


def test_the_stable_parser_still_refuses_a_prerelease():
    """It is still the right parser for the OFFERED tag; it just is not the
    right one for the installed version."""
    with pytest.raises(updater.ReleaseMetadataError):
        updater._parse_stable_version("0.23.0-beta.1")
    assert updater._parse_stable_version("v0.22.1") == (0, 22, 1)


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------

def test_a_prerelease_installation_reaches_the_network():
    """The brief's gate. Before 108 this raised with zero calls made."""
    calls: list = []
    updater.check_for_update("0.23.0-beta.1", opener=_opener(_release_payload("v0.23.0"), calls))
    assert calls == [updater.GITHUB_RELEASES_API]


def test_the_shipped_version_reaches_the_network():
    calls: list = []
    updater.check_for_update(samsara.__version__,
                             opener=_opener(_release_payload("v9.9.9"), calls))
    assert calls == [updater.GITHUB_RELEASES_API]


def test_a_stable_release_is_offered_to_a_prerelease_installation():
    calls: list = []
    release = updater.check_for_update(
        "0.23.0-beta.1", opener=_opener(_release_payload("v0.23.0"), calls))
    assert release is not None
    assert release.tag == "v0.23.0" and release.version == "0.23.0"


@pytest.mark.parametrize("tag", ["v0.24.0", "v0.24.0-beta.3"])
def test_the_offered_version_string_comes_from_the_tag(tag):
    """It is shown to the user ("Samsara v{version} is available") and
    written into the update status file, so it must be the real version and
    not a rendering of the comparison key -- which since 108 carries a fourth
    element and would read "0.24.0.(1,)"."""
    release = updater.check_for_update(
        "0.23.0-beta.1", opener=_opener(_release_payload(tag, prerelease="-" in tag), []),
        allow_prerelease=True)
    assert release is not None
    assert release.tag == tag
    assert release.version == tag.removeprefix("v")


def test_an_older_stable_is_not_offered_to_a_prerelease_installation():
    """0.22.1 is behind 0.23.0-beta.1, so there is nothing to install."""
    calls: list = []
    assert updater.check_for_update(
        "0.23.0-beta.1", opener=_opener(_release_payload("v0.22.1"), calls)) is None
    assert calls == [updater.GITHUB_RELEASES_API]


def test_the_same_stable_is_not_offered_twice():
    assert updater.check_for_update(
        "0.23.0", opener=_opener(_release_payload("v0.23.0"), [])) is None


# ---------------------------------------------------------------------------
# The offered-update policy is a SEPARATE decision, and it defaults to stable
# ---------------------------------------------------------------------------

def test_a_prerelease_release_is_not_offered_by_default():
    """Unchanged from before 108, and deliberately so. Note this now fails
    on the policy rather than on the installed version's format."""
    payload = _release_payload("v0.24.0-beta.1", prerelease=True)
    with pytest.raises(updater.ReleaseMetadataError, match="not a stable"):
        updater.check_for_update("0.23.0-beta.1", opener=_opener(payload, []))


def test_a_prerelease_tag_is_not_offered_even_when_github_omits_the_flag():
    """Belt and braces: the tag is parsed by the stable-only parser too, so a
    mislabelled release cannot slip a beta onto a stable installation."""
    payload = _release_payload("v0.24.0-beta.1", prerelease=False)
    with pytest.raises(updater.ReleaseMetadataError, match="stable version"):
        updater.check_for_update("0.23.0", opener=_opener(payload, []))


def test_allow_prerelease_opens_the_policy_and_only_the_policy():
    """The flag is live, not decoration -- both guards defer to it. In
    production /releases/latest never serves a prerelease, so flipping it
    there also needs the /releases endpoint; that is out of 108's scope."""
    payload = _release_payload("v0.24.0-beta.1", prerelease=True)
    release = updater.check_for_update("0.23.0-beta.1", opener=_opener(payload, []),
                                       allow_prerelease=True)
    assert release is not None and release.tag == "v0.24.0-beta.1"


def test_allow_prerelease_still_refuses_an_older_prerelease():
    payload = _release_payload("v0.23.0-beta.1", prerelease=True)
    assert updater.check_for_update("0.23.0-beta.2", opener=_opener(payload, []),
                                    allow_prerelease=True) is None


def test_the_install_path_still_pins_the_tag_to_a_stable_version():
    """updater.py's download/install path parses release.tag with the
    stable-only parser. 108 did not touch it, and this says so."""
    source = (Path(updater.__file__)).read_text(encoding="utf-8", errors="replace")
    assert "_parse_stable_version(release.tag)" in source
