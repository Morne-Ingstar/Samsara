"""978: supplemental help examples match real registered commands, without effects."""
import pytest

from samsara import command_catalog as catalog
from samsara.command_help_recent import HELP
from samsara.command_scope import MatchContext


@pytest.fixture(scope='module')
def router():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    executor._matcher.set_enabled_packs({entry.pack for entry in executor._matcher._sorted})
    return executor, {catalog.canonical_phrase(s.to_dict()): s for s in catalog.build_catalog(executor)}


@pytest.mark.parametrize('phrase', HELP)
def test_recent_examples_match_registered_identity_and_metadata(router, phrase):
    executor, specs = router
    examples, precondition = HELP[phrase]
    assert specs[phrase].examples == examples
    assert specs[phrase].precondition == precondition
    for example in examples:
        entry, _remainder = executor._matcher.match(example, context=MatchContext.for_app('chrome.exe'))
        assert entry is not None, example
        assert catalog.normalize_phrase(entry.phrase) == phrase, example
