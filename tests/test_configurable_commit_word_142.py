"""Queue 142: the commit word is configurable and defaults to 'finish'."""
import pytest

from samsara import session_modes as sm


@pytest.fixture(autouse=True)
def _restore_commit_word():
    original = sm.DICTATE_COMMIT_PHRASE
    yield
    sm.set_commit_phrase(original)


def test_the_product_default_is_not_a_filler_homophone():
    assert sm.DEFAULT_DICTATE_COMMIT_PHRASE == "finish"
    assert sm.DEFAULT_DICTATE_COMMIT_PHRASE not in sm.COMMIT_WORD_HOMOPHONE_RISKS


def test_setting_the_word_changes_what_commits():
    sm.set_commit_phrase("finish")
    assert sm.is_dictate_commit("finish") is True
    assert sm.is_dictate_commit("Finish.") is True
    assert sm.is_dictate_commit("end") is False
    assert sm.is_dictate_commit("And.") is False


def test_end_still_works_when_chosen_explicitly():
    sm.set_commit_phrase("end")
    assert sm.is_dictate_commit("End!") is True
    assert sm.is_dictate_commit("finish") is False


def test_a_lone_and_never_commits_under_any_word():
    for word in ("finish", "end", "done"):
        sm.set_commit_phrase(word)
        assert sm.is_dictate_commit("and") is False
        assert sm.is_dictate_commit("And...") is False


def test_a_multi_word_or_empty_value_is_refused_and_keeps_the_current_word():
    sm.set_commit_phrase("finish")
    assert sm.set_commit_phrase("that is enough") == "finish"
    assert sm.set_commit_phrase("") == "finish"
    assert sm.set_commit_phrase(None) == "finish"
    assert sm.is_dictate_commit("finish") is True


def test_case_and_punctuation_never_matter():
    sm.set_commit_phrase("Finish")
    assert sm.DICTATE_COMMIT_PHRASE == "finish"
    assert sm.is_dictate_commit("FINISH!") is True


def test_the_trailing_commit_split_follows_the_configured_word():
    sm.set_commit_phrase("finish")
    assert sm.split_trailing_dictate_commit("Fixed it. Finish.") == "Fixed it."
    assert sm.split_trailing_dictate_commit("Fixed it. End.") is None


def test_the_homophone_set_tracks_the_configured_word():
    # command_catalog and quick_reference_qt read this set to reserve and
    # display the commit words; a stale set would reserve the wrong phrase.
    sm.set_commit_phrase("finish")
    assert set(sm._DICTATE_COMMIT_HOMOPHONES) == {"finish"}
