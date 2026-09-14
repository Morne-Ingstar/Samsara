"""Deterministic intent layer (SAMSARA_MAP Move B tier 2) -- NOT wired.

Understands natural phrasing for the catalogued commands with no model:

    normalize  pure text clean-up: fillers, Whisper confusions, numbers, NATO
    grammar    verb classes + shared slot parsers -> Parse(canonical_id, args,
               confidence, span), "and"/"then" chaining
    resolve    tier order: exact alias -> grammar -> token-overlap similarity
               -> resolved | suggest | dictation

Nothing in the app calls this package yet (no dispatch, no session_modes,
no plugin). docs/INTENT_GRAMMAR.md describes the tiers and how coverage is
measured (tools/gen_intent_eval.py, tests/test_intent_grammar_eval.py).

The entry point is samsara.intent.resolve.resolve(utterance) (not re-exported
here, so the submodule name stays importable).
"""

from samsara.intent.resolve import IntentResolver, Resolution

__all__ = ["IntentResolver", "Resolution"]
