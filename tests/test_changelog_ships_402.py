"""The packaged changelog lives where Home's release-note reader expects it."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_recipe_ships_changelog_at_data_root():
    spec = (ROOT / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    assert "datas.append((str(app_dir / 'CHANGELOG.md'), '.'))" in spec


def test_home_and_catalog_agree_on_changelog_root():
    from samsara import command_catalog
    from samsara.ui import home_qt

    assert home_qt.CHANGELOG_PATH == command_catalog.ROOT / "CHANGELOG.md"


def test_repository_changelog_has_a_release_summary():
    from samsara.ui import home_qt

    assert (ROOT / "CHANGELOG.md").is_file()
    version, sentence = home_qt.changelog_summary()
    assert version
    assert sentence and sentence.strip()
