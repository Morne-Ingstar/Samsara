# Intent grammar (Move B, tier 2)

`samsara/intent/` understands natural phrasing for the catalogued commands
**without a model**. Most of "every way someone could say move X to Y" is
*verb + object + slots*, not a list of sentences; this package is that
deterministic layer. The model becomes an upgrade, never the floor.

**Status: not wired.** Nothing in `dictation.py`, `samsara/session_modes.py`,
`samsara/commands.py`, the registry or any plugin imports it (a test enforces
this). It is the prerequisite for Move A's intent-gate shadow inside DICTATE.

## Source of truth

The catalog is built **live** through the queue-15 path:
`command_catalog.catalog_from_registry_rows(CommandMatcher.list_commands())`
(`samsara.intent.resolve.load_records`). The rows path skips the
"handler reads `remainder`" argument inference, so `load_records` re-applies
`command_catalog.infer_args` with the handler from
`plugin_commands._MODULE_ENTRIES`; the result matches `commands_catalog.json`
exactly (tested). `commands_catalog.json` is a fixture, not a runtime source:
the packaged build does not ship it.

## Tiers

`samsara.intent.resolve.resolve(utterance)` returns a `Resolution` whose
`kind` is `resolved`, `suggest` (the chip's "did you mean") or `dictation`.
The order is the constant `TIERS = ("exact", "grammar", "similarity")`.

| tier | what | decides |
|---|---|---|
| 1 exact | the utterance **is** an alias, as heard or with fillers stripped | resolved, confidence 1.0 |
| 2 grammar | `Grammar.parse_chain`: verb classes, object synonyms, look-alikes, slots, "and"/"then" chaining | resolved at `GRAMMAR_RESOLVE` (0.75); a weaker parse becomes the first suggestion |
| 3 similarity | order-free token overlap (Dice) against every alias | resolved at `SIMILARITY_RESOLVE` (0.86) with no alias word missing and at most `SIMILARITY_MAX_EXTRA` (1) extra word; `suggest` at `SIMILARITY_SUGGEST` (0.60); below is dictation |

Filler handling: `normalize.filler_variants` yields the least-stripped
reading first ("snap right now" -> "snap right", then "snap"). Tier 1 is tried
on every reading before tier 2; tier 2 keeps the most confident parse over
all readings.

Safety rules (all tested):

* Whole-utterance control words (`command_catalog.reserved_whole_utterances`,
  e.g. "scratch that") match **only** tier 1, as heard. They are not grammar
  templates.
* A destructive command never resolves through a look-alike, a synonym or
  tier 3. Those readings are suggestions.
* Tier 3 never resolves a command with a required argument.
* A missing required slot in tier 2 is a suggestion.
* An app name that the supplied list does not know costs `P_UNKNOWN_APP`,
  which pushes the parse below `GRAMMAR_RESOLVE`.

Latency budget: tiers 1+2 decide in under `LATENCY_BUDGET_MS` (30 ms) on the
full catalog. `Resolution.t12_ms` records the time, and the eval test asserts
the p99 and the maximum over the corpus.

## Confidence

A grammar parse starts at `GRAMMAR_BASE` (0.95) and pays for every
approximation:

| cost | when |
|---|---|
| `P_STEM` 0.02 | singular/plural |
| `P_SYNONYM` 0.03 | verb class or object synonym |
| `P_CONFUSION` 0.05 | Whisper look-alike |
| `P_TEXT_SLOT` 0.05 | free-text slot |
| `P_RAW_APP` 0.10 | app name with no list to check against |
| `P_UNKNOWN_APP` 0.30 | app name the list does not know |
| `P_MISSING_REQUIRED` 0.30 | required slot not said |

`Parse.word_penalty` is the cost of the command words alone; the destructive
rule uses it, so a plainly said destructive command with a text slot still
resolves. Ties go to the longer alias, then catalog order. Rules
(`RULE_CONFIDENCE` 0.92) win ties because they are specific.

## Normalisation (`normalize.py`, pure)

* `tokens`: the registry's own matching view (`command_catalog.normalize_phrase`).
* `strip_fillers` / `filler_variants`: `LEADING_FILLERS` ("can you", "could you
  just", "would you mind", "hey", "um", ...), `TRAILING_FILLERS` ("please",
  "for me", "right now", ...), `HESITATIONS` anywhere. An utterance is never
  stripped to nothing.
* `confusion_key` / `collapse_confusions`: `command_catalog.WHISPER_CONFUSIONS`
  merged with `EXTRA_CONFUSIONS` (tab/tap, close/clothes, write/right, won/one,
  two/to/too, for/four, ...). Digits read as number words first. Extend the
  table from the correction dictionary later.
* Numbers: `parse_number`, `number_to_words`, `numerals_to_words`,
  `words_to_numerals`. Ordinals: `parse_ordinal`.
* Letters: `nato_letter`, `parse_letters`.

## Grammar (`grammar.py`)

Every alias becomes a **template**: its words, then the command's declared
argument slots. An alias word matches when it is said as:

* itself;
* a stem (singular/plural);
* a look-alike;
* a synonym: `VERB_CLASSES` for the first word, `OBJECT_SYNONYMS` for later
  words.

`OPTIONAL_WORDS` (the, a, my, this, on, over, for, ...) may appear in between.
The rest of the utterance must parse as the slots:

| slot | parser |
|---|---|
| `text`, `playlist` | the remainder ("saying"/"that"/"about" dropped) |
| `int` | `normalize.parse_number` / `parse_ordinal` |
| `nato_letter` | `normalize.parse_letters`; a plural argument name takes a list |
| `app_name` | `app_index.rank_candidates` + `MATCH_FLOOR` against the caller's `app_names` (app index plus running windows); never a destination |
| `monitor` | `windows._is_destination_text`; "the other one" -> `Unresolved` |
| `side` | left/right/top/bottom and the corners |
| `app_name` + `monitor` | `windows._parse_send_remainder`, plus destination-first order ("move to the left screen chrome") |

`RULES` cover families that are not alias-shaped:

* `rule_volume`: "make it louder", "crank the sound down";
* `rule_scroll`: direction, amount and page, "scroll to the top";
* `rule_numbered_tab`: "go to tab three", "the second tab";
* `rule_dictate_to_claude`: "send a message to claude saying ...".

Chaining: `parse_chain` parses the whole utterance as one command first.
Otherwise it splits at "and then" / "then" / "and" / "after that", but only
when **every** part parses, so "tile windows bravo and charlie" stays one
command.

### Adding a verb class

1. Add a `VerbClass(name, synonyms, verbs)` to `VERB_CLASSES`. `verbs` are
   catalog first words (the verb of real aliases); `synonyms` are how people
   say them and must include the verbs. A multi-word synonym ("bring up") is
   fine.
2. If the class needs an object word said differently, add it to
   `OBJECT_SYNONYMS` (alias word -> spoken forms).
3. If the family is not alias-shaped (it needs direction words, numbers or
   free order), write a rule `rule_x(grammar, tokens) -> Parse | None` and
   append it to `RULES`.
4. Add unit cases to `tests/test_intent_grammar.py`. The test
   `test_every_verb_class_covers_real_catalog_verbs` fails if the class names
   verbs the catalog does not have.
5. Regenerate and re-measure (below). A class that raises coverage but
   resolves a dictated sentence is wrong: the eval test fails on the first
   one.

## Reuse

The slot parsers reuse existing code rather than adding a fourth parser:

| need | reused from |
|---|---|
| number words 0-99, compounds | `plugins/commands/show_numbers._WORD_TO_NUM`, `_parse_spoken_number` |
| ordinals | `plugins/commands/windows._ORDINALS` |
| NATO + spoken letter names | `plugins/commands/window_switcher.PHONETIC`, `_parse_letters` |
| screens, sides, send remainder | `plugins/commands/windows._is_destination_text`, `_parse_send_remainder` |
| app-name scoring | `samsara/app_index.score_name_match`, `rank_candidates`, `MATCH_FLOOR` |
| Whisper confusions | `samsara/command_catalog.WHISPER_CONFUSIONS` |
| token view | `samsara/command_catalog.normalize_phrase` (`command_registry.view_tokens`) |

These duplicates remain as candidates to fold into the ones above later:

* `window_cube._NUMBER_WORDS` / `_number_from` / `_numbers_in` (1-9);
* `window_switcher._parse_monitor_index`;
* `samsara/letter_spelling._LETTER_HOMOPHONES` / `parse_letters` (spelling truth, different contract);
* `samsara/command_parser.DEFAULT_FILLERS` / `strip_fillers`;
* the phrase/word tables in `samsara/phonetic_wash`.

## Measuring coverage

`tools/gen_intent_eval.py` writes `tests/fixtures/intent_eval.jsonl`: 40 lines
for every canonical id.

* **37 positives**: aliases, slot fills, fillers and politeness, verb synonyms,
  determiners, reordered slots, Whisper-style mishearings, numerals, casing
  and punctuation.
* **3 negatives**:
  * `sentence`: a dictated sentence containing the phrase;
  * `other_command`: the nearest different command;
  * `nonsense`.

The generator is seeded and deterministic (`--check` exits 1 if the file is
stale). It keeps its own synonym and mishearing tables. `GENERATOR_ONLY_*`
entries are deliberately unknown to the grammar, so the number includes
phrasings nobody taught it. The file is a **test set**; nothing reads it as
a lookup table.

`tests/test_intent_grammar_eval.py` resolves every line with the eval app
list (`APP_NAMES`). It prints:

* overall coverage, and coverage by phrasing kind;
* coverage excluding the trivial alias/filler/punct kinds;
* the 20 worst commands, each with an example failure;
* negative hits;
* tier 1+2 latency.

It asserts:

* overall resolved-correctly >= 0.85;
* **zero** dictated sentences resolved to any command;
* no near-miss or nonsense line resolved to its target;
* the fixture is current;
* tier 1+2 p99 and max are under budget.

```
F:\envs\sami\python.exe tools\gen_intent_eval.py
F:\envs\sami\python.exe -m pytest tests/test_intent_grammar.py tests/test_intent_grammar_eval.py -q -p no:cacheprovider
```
