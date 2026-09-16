"""
Unified command registry and matcher.

Loads all commands (built-in JSON + plugins), tokenizes trigger phrases,
sorts by token count descending, and provides a single match() function
that both the runtime CommandExecutor and test CommandExecutor share.

Longest-match semantics: "find tab github" matches the 2-token "find tab"
plugin, not the 1-token "find" built-in, even though "find" appears first
in commands.json.

Priority rules:
- On exact same-phrase collision, built-ins win (plugins skipped and logged).
- On prefix overlap (short phrase is a token-prefix of longer phrase), the
  longer phrase wins regardless of source. Collision report logs both.
- Disabled-pack commands are skipped in match(). An utterance that is EXACTLY
  a disabled pack's phrase is a miss (disabled_pack_for() names the pack): it
  never falls through to a shorter enabled command with the rest as its
  argument ("show windows" with window-management off must not run "show").
  A disabled phrase that is only a token-prefix of a longer utterance still
  lets a shorter enabled-pack command match.
- Scoped commands (queue 68, samsara.command_scope) are candidates only when
  their scope is live for the utterance's context (foreground app, window
  title, active tags). Unscoped commands are unaffected. An utterance that is
  EXACTLY an out-of-scope phrase is a miss, like a disabled pack's
  (out_of_scope_for() says why).
"""

import logging
import re
import threading
import time
from enum import Enum

from samsara import command_scope as _scope

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatch result contract
# ---------------------------------------------------------------------------

class DispatchState(str, Enum):
    """What happened to one utterance offered to the command executor.

    MISS is the only state in which the utterance was NOT claimed as a
    command; every other state means "a command owns this utterance" and the
    caller must never re-offer it as dictation or as a new model request --
    including FAILED, REJECTED and CANCELLED.
    """

    MISS = "miss"            # no command recognised (or a handler declined)
    MATCHED = "matched"      # recognised; execution not attempted by this call
    QUEUED = "queued"        # accepted; work scheduled, outcome arrives later
    COMPLETED = "completed"  # handler reported success synchronously
    FAILED = "failed"        # recognised, attempted, and failed (or raised)
    REJECTED = "rejected"    # recognised but refused before running (debounce)
    CANCELLED = "cancelled"  # recognised and cancelled before completion


class DispatchResult(tuple):
    """Result of CommandExecutor.process_text.

    Still unpacks as the legacy ``(result, was_command)`` pair, where
    ``was_command`` now means CLAIMED (state is not MISS) -- a matched command
    that failed is still a command. New callers read ``.state``.
    """

    def __new__(cls, state, result=None, phrase=None, detail=None):
        state = DispatchState(state)
        self = tuple.__new__(cls, (result, state is not DispatchState.MISS))
        self.state = state
        self.phrase = phrase
        self.detail = dict(detail or {})
        return self

    def __getnewargs__(self):
        return (self.state, self[0], self.phrase, self.detail)

    @property
    def result(self):
        return self[0]

    @property
    def claimed(self) -> bool:
        return self[1]

    @property
    def succeeded(self) -> bool:
        """True ONLY for COMPLETED. MATCHED (recognised, not attempted) and
        QUEUED (accepted, outcome unknown) are not success: "queued" is not
        completion, and nothing may report a receipt it does not have."""
        return self.state is DispatchState.COMPLETED

    @classmethod
    def miss(cls, text, detail=None):
        return cls(DispatchState.MISS, result=text, detail=detail)

    def __repr__(self):
        return (f"DispatchResult({self.state.value!r}, result={self[0]!r}, "
                f"phrase={self.phrase!r})")


def adapt_handler_return(value) -> DispatchState:
    """Map a plugin handler's return value onto a DispatchState.

    The plugin API predates async work, so this is the one boundary adapter:
      * DispatchResult / DispatchState -> honoured as given
      * True (or any other truthy value) -> COMPLETED
      * None -> QUEUED. Handlers such as ask_ollama.handle_ask_ava schedule a
        worker and return None; that is accepted work, never a miss.
      * False (or another falsy non-None value) -> MISS. The documented plugin
        contract is "return False to fall through" -- a decline, e.g.
        app_lifecycle refusing a non-whole-utterance match. A handler that
        tried and failed must raise or return DispatchState.FAILED.
    """
    if isinstance(value, DispatchResult):
        return value.state
    if isinstance(value, DispatchState):
        return value
    if value is None:
        return DispatchState.QUEUED
    return DispatchState.COMPLETED if value else DispatchState.MISS


# ---------------------------------------------------------------------------
# Command metadata -- one owner, verbatim, never defaulted to safe
# ---------------------------------------------------------------------------

#: Sentinel for a metadata field the command's author never declared.
UNKNOWN = "unknown"

#: Every safety/AI metadata field a command can declare (see
#: plugin_commands.command). The registry keeps each verbatim; an undeclared
#: field is UNKNOWN in CommandEntry.metadata. Nothing here is enforced yet.
METADATA_FIELDS = (
    "ai_visible",
    "risk_class",
    "ai_composable",
    "side_effects",
    "preconditions",
    "voice_triggerable",
    "param_schema",
    "reversible",
    "preview_template",
)


def _declared_metadata(source: dict) -> dict:
    """Pick METADATA_FIELDS out of a dict verbatim; missing -> UNKNOWN."""
    return {name: source[name] if name in source else UNKNOWN
            for name in METADATA_FIELDS}


#: Tag no code ever publishes: a command with a malformed scope in
#: commands.json is loaded but never live, and says so in the log.
INVALID_SCOPE_TAG = "invalid_scope"


def _resolve_scope(declared, pack: str, phrase: str):
    """The command's own scope, else its pack's (command_packs.PACKS[pack]
    ['scope']), else None (global). A malformed declaration never makes a
    command global by accident: it is logged and the command is never live."""
    raw = declared
    origin = "command"
    if raw is None:
        try:
            from samsara.command_packs import PACKS  # noqa: PLC0415
            raw = (PACKS.get(pack) or {}).get("scope")
            origin = f"pack {pack!r}"
        except Exception:
            raw = None
    try:
        return _scope.parse_scope(raw)
    except ValueError as exc:
        _log.error("[SCOPE] %r: invalid %s scope %r (%s) -- the command is loaded but never live",
                   phrase, origin, raw, exc)
        return _scope.Scope(tags=frozenset({INVALID_SCOPE_TAG}))


# ---------------------------------------------------------------------------
# Normalised matching view with offsets into the original utterance
# ---------------------------------------------------------------------------

_RAW_TOKEN_RE = re.compile(r'\S+')
_NON_WORD_RE = re.compile(r'[^\w]')
# Separator-only tokens between the phrase and its argument ("ask ava - x").
_SEPARATOR_TOKEN_RE = re.compile('^[-\u2013\u2014:;,.]+$')
# Sentence terminators Whisper appends to an utterance. Only these, and only
# at the very end of the argument, are dropped; everything else is verbatim.
_TRAILING_TERMINATOR_RE = re.compile(r'[.,;:]+$')


def view_tokens(text):
    """Tokenise for matching: [(normalised, start, end), ...].

    The normalised form is the old matching view -- lowercased with non-word
    characters removed -- but each token keeps the span of the ORIGINAL text
    it came from, so an argument can be sliced out verbatim.
    """
    tokens = []
    for m in _RAW_TOKEN_RE.finditer(text or ''):
        norm = _NON_WORD_RE.sub('', m.group().lower())
        if norm:
            tokens.append((norm, m.start(), m.end()))
    return tokens


def argument_text(text, start):
    """Original-text argument beginning at offset `start`.

    Verbatim except: surrounding whitespace, separator-only tokens directly
    after the phrase, and one trailing run of sentence terminators.
    """
    rest = (text or '')[start:]
    while True:
        stripped = rest.lstrip()
        m = _RAW_TOKEN_RE.match(stripped)
        if m and _SEPARATOR_TOKEN_RE.match(m.group()):
            rest = stripped[m.end():]
            continue
        rest = stripped
        break
    rest = rest.rstrip()
    return _TRAILING_TERMINATOR_RE.sub('', rest).rstrip()


class CommandEntry:
    """A single registered command (built-in or plugin)."""

    def __init__(self, phrase, source, cmd_type, data=None, handler=None,
                 aliases=None, pack='core', debounce=0.0, app_overrides=None,
                 description='',
                 ai_visible=True, risk_class=UNKNOWN, ai_composable=False,
                 side_effects=None, preconditions=None, voice_triggerable=True,
                 param_schema=None, reversible=False, preview_template='',
                 metadata=None, scope=None):
        """
        Args:
            phrase: canonical trigger phrase (lowercase, stripped)
            source: 'builtin' or 'plugin'
            cmd_type: 'hotkey', 'launch', 'text', 'plugin', etc.
            data: dict from commands.json (for built-ins)
            handler: callable (for plugins)
            aliases: list of alternative trigger phrases
            pack: pack name this command belongs to (default 'core')
            debounce: seconds to suppress re-execution in command mode
            app_overrides: per-app key binding overrides
            description: human-readable summary of what the command does

            -- AI Config Assistant safety metadata --
            ai_visible: if False, excluded from Ava's injected command list
            risk_class: 'safe' | 'reversible' | 'destructive'
            ai_composable: if True, may be included in AI-generated macros
            side_effects: list of side-effect category strings
            preconditions: list of machine-checkable condition id strings
            voice_triggerable: if False, must not fire from voice transcription
            param_schema: dict of param_name -> constraint spec
            reversible: True if effects can be undone
            preview_template: human-readable template describing what will happen
            metadata: {field: value} for every METADATA_FIELDS name, verbatim
                as the author declared it, UNKNOWN where undeclared. The typed
                attributes above keep their legacy gate-closed defaults for
                existing readers; `metadata` is the authoritative export.
        """
        self.phrase = phrase.lower().strip()
        self.tokens = self.phrase.split()
        self.token_count = len(self.tokens)
        self.source = source
        self.cmd_type = cmd_type
        self.data = data or {}
        self.handler = handler
        self.aliases = [a.lower().strip() for a in (aliases or [])]
        self.pack = pack or 'core'
        self.debounce = float(debounce) if debounce else 0.0
        self.app_overrides = dict(app_overrides) if app_overrides else {}
        self.description = description or ''
        self.ai_visible = bool(ai_visible)
        self.risk_class = risk_class or UNKNOWN
        self.ai_composable = bool(ai_composable)
        self.side_effects = list(side_effects or [])
        self.preconditions = list(preconditions or [])
        self.voice_triggerable = bool(voice_triggerable)
        self.param_schema = dict(param_schema or {})
        self.reversible = bool(reversible)
        self.preview_template = str(preview_template)
        # Queue 68: when this command is a candidate (samsara.command_scope).
        # None = global, exactly as before scoping existed.
        self.scope = scope if (scope is None or isinstance(scope, _scope.Scope)) else _scope.parse_scope(scope)
        declared = metadata if metadata is not None else {}
        self.metadata = {name: declared.get(name, UNKNOWN) for name in METADATA_FIELDS}
        if metadata is not None:
            # With explicit metadata the risk class is exactly what the author
            # declared, UNKNOWN when they declared nothing -- never 'safe' by
            # omission (the plugin decorator's legacy dict still says 'safe').
            self.risk_class = self.metadata['risk_class'] or UNKNOWN


class CommandMatch:
    """One match with offsets into the text that was matched.

    remainder is the ORIGINAL-text argument (see argument_text);
    normalized_remainder is the old lowercase/punctuation-free view, for
    schema-declared slots that want it (app aliases, spoken numbers).
    """

    __slots__ = ('entry', 'phrase_tokens', 'argument_start', 'remainder',
                 'normalized_remainder')

    def __init__(self, entry, phrase_tokens, argument_start, remainder,
                 normalized_remainder):
        self.entry = entry
        self.phrase_tokens = tuple(phrase_tokens)
        self.argument_start = argument_start
        self.remainder = remainder
        self.normalized_remainder = normalized_remainder


class CommandMatcher:
    """Token-based longest-match command matcher.

    Usage:
        matcher = CommandMatcher()
        matcher.set_enabled_packs({'core', 'browsers', 'media'})
        matcher.load_builtins(commands_dict)   # from commands.json
        matcher.load_plugins(plugin_registry)  # from plugin_commands
        matcher.freeze()  # sort, detect collisions, lock

        entry, remainder = matcher.match("find tab github")
        # entry.phrase = "find tab", remainder = "github"
    """

    def __init__(self):
        self._entries = {}      # phrase -> CommandEntry
        self._sorted = []       # canonical entries sorted by token_count desc
        self._match_table = []  # [(phrase_tokens, entry), ...] for match()
        self._frozen = False
        self._enabled_packs = None   # None = all packs enabled (no filtering)
        # Debounce tracking: phrase -> monotonic timestamp of last execution
        self._last_executions = {}
        self._exec_lock = threading.Lock()
        # Queue 68: per-utterance scope context. Without a provider (tools,
        # tests) the foreground is "unresolved" and only tags are read.
        self._context_provider = None
        self._has_scoped = False
        self._needs_foreground = False

    # -- scope (queue 68) -----------------------------------------------------

    def set_context_provider(self, provider):
        """provider() -> command_scope.MatchContext, called once per match
        when any registered command is scoped. CommandExecutor installs
        command_scope.capture_context."""
        self._context_provider = provider

    def current_context(self):
        """The context a match uses now, or None when nothing is scoped (the
        common case costs nothing). Never raises."""
        if not self._has_scoped:
            return None
        if self._needs_foreground and self._context_provider is not None:
            try:
                return self._context_provider()
            except Exception as exc:
                _log.warning("[SCOPE] context provider failed (%s): app-scoped commands are not "
                             "candidates for this utterance", exc)
        reason = (_scope.UNRESOLVED_NO_PROVIDER if self._needs_foreground else "")
        return _scope.MatchContext.unresolved(reason, _scope.active_tags())

    def is_live(self, entry, context=None):
        """True when `entry` is a candidate in `context` (scope only; packs
        are checked separately)."""
        if entry.scope is None:
            return True
        return _scope.scope_live(entry.scope, context)[0]

    def out_of_scope_for(self, text, context=None):
        """(entry, why) when ``text`` is EXACTLY the phrase of an enabled but
        out-of-scope command (so match() treated it as a miss), else None."""
        if not text or not self._frozen or not self._has_scoped:
            return None
        clean_lower = ' '.join(norm for norm, _s, _e in view_tokens(text))
        entry = self._entries.get(clean_lower)
        if entry is None or entry.scope is None or not self._pack_enabled(entry.pack):
            return None
        if context is None:
            context = self.current_context()
        live, why = _scope.scope_live(entry.scope, context)
        return None if live else (entry, why)

    def live_phrase_count(self, context=None):
        """(live, total) phrase rows among enabled packs for `context` --
        the size of the candidate set an utterance is matched against."""
        total = live = 0
        for _tokens, entry in self._match_table:
            if not self._pack_enabled(entry.pack):
                continue
            total += 1
            if self.is_live(entry, context):
                live += 1
        return live, total

    def set_enabled_packs(self, pack_names):
        """Set which packs are active.

        Must be called before freeze(). If never called, all packs are enabled
        (backwards-compatible default).

        Args:
            pack_names: set/iterable of pack name strings
        """
        self._enabled_packs = set(pack_names)

    def _pack_enabled(self, pack: str) -> bool:
        if self._enabled_packs is None:
            return True
        return pack in self._enabled_packs

    def load_builtins(self, commands_dict):
        """Load built-in commands from the parsed commands.json dict.

        Args:
            commands_dict: {"command name": {"type": "hotkey", ...}, ...}
        """
        if self._frozen:
            raise RuntimeError("Cannot load into frozen registry")
        for name, data in commands_dict.items():
            name_lower = name.lower().strip()
            entry = CommandEntry(
                phrase=name_lower,
                source='builtin',
                cmd_type=data.get('type', 'unknown'),
                data=data,
                pack=data.get('pack', 'core'),
                debounce=float(data.get('debounce', 0.0)),
                app_overrides=data.get('app_overrides', {}),
                description=data.get('description', ''),
                ai_visible=data.get('ai_visible', True),
                ai_composable=data.get('ai_composable', False),
                side_effects=data.get('side_effects', []),
                preconditions=data.get('preconditions', []),
                voice_triggerable=data.get('voice_triggerable', True),
                param_schema=data.get('param_schema', {}),
                reversible=data.get('reversible', False),
                preview_template=data.get('preview_template', ''),
                # Whatever commands.json declares, verbatim; the rest UNKNOWN.
                metadata=_declared_metadata(data),
                scope=_resolve_scope(data.get('scope'), data.get('pack', 'core'), name_lower),
            )
            self._entries[name_lower] = entry

    def load_plugins(self, plugin_registry):
        """Load plugin commands from plugin_commands._REGISTRY.

        Args:
            plugin_registry: dict of phrase -> {func, phrase, aliases, source, pack}

        Plugin commands have LOWER priority than built-ins on exact
        phrase collision. But longest-match means a 2-token plugin
        beats a 1-token built-in on prefix match.
        """
        if self._frozen:
            raise RuntimeError("Cannot load into frozen registry")
        # Deduplicate: plugin registry stores aliases as separate keys
        # pointing to the same underlying entry object.
        seen_ids = set()
        for phrase, entry_data in plugin_registry.items():
            entry_id = id(entry_data)
            if entry_id in seen_ids:
                continue  # alias pointing to an entry we already processed
            seen_ids.add(entry_id)

            canonical = entry_data['phrase']
            # Skip if a built-in already claimed this exact phrase
            if canonical in self._entries:
                print(f"[REGISTRY] Plugin '{canonical}' shadowed by built-in")
                continue

            func = entry_data.get('func')
            doc = ''
            if func and func.__doc__:
                doc = func.__doc__.strip().split('\n')[0].strip()

            entry = CommandEntry(
                phrase=canonical,
                source='plugin',
                cmd_type='plugin',
                handler=entry_data['func'],
                aliases=entry_data.get('aliases', []),
                pack=entry_data.get('pack', 'core'),
                debounce=float(entry_data.get('debounce', 0.0)),
                app_overrides=entry_data.get('app_overrides', {}),
                description=doc,
                ai_visible=entry_data.get('ai_visible', True),
                risk_class=entry_data.get('risk_class', UNKNOWN),
                ai_composable=entry_data.get('ai_composable', False),
                side_effects=entry_data.get('side_effects', []),
                preconditions=entry_data.get('preconditions', []),
                voice_triggerable=entry_data.get('voice_triggerable', True),
                param_schema=entry_data.get('param_schema', {}),
                reversible=entry_data.get('reversible', False),
                preview_template=entry_data.get('preview_template', ''),
                # The decorator records what the author actually declared;
                # a hand-built registry dict is taken at its word, key by key.
                metadata=(entry_data['metadata'] if isinstance(entry_data.get('metadata'), dict)
                          else _declared_metadata(entry_data)),
                scope=_resolve_scope(entry_data.get('scope'), entry_data.get('pack', 'core'), canonical),
            )
            self._entries[canonical] = entry
            # Register aliases (skip individually if shadowed)
            for alias in entry.aliases:
                if alias not in self._entries:
                    self._entries[alias] = entry
                else:
                    print(f"[REGISTRY] Plugin alias '{alias}' shadowed by existing command")

    def freeze(self):
        """Sort entries by token count descending and lock the registry.

        After freeze():
        - No more loading allowed
        - match() becomes available
        - Collision warnings printed to console
        """
        # Deduplicate by id so aliases don't produce duplicate sorted entries
        canonical = {id(e): e for e in self._entries.values()}.values()
        self._sorted = sorted(canonical, key=lambda e: e.token_count, reverse=True)

        # Build the match table: one row per canonical + one per alias so
        # alias matching goes through the same longest-first scan.
        self._match_table = []
        for entry in self._sorted:
            self._match_table.append((entry.tokens, entry))
            for alias in entry.aliases:
                alias_tokens = alias.split()
                self._match_table.append((alias_tokens, entry))

        # Re-sort including aliases; a long alias still wins over short canonicals.
        self._match_table.sort(key=lambda x: len(x[0]), reverse=True)

        scoped = [e for e in self._sorted if e.scope is not None]
        self._has_scoped = bool(scoped)
        self._needs_foreground = any(e.scope.needs_foreground for e in scoped)

        self._frozen = True

        unique_entries = len(self._sorted)
        total_phrases = len(self._entries)
        print(f"[REGISTRY] Frozen: {unique_entries} commands, "
              f"{total_phrases} phrases (including aliases)")

    def match(self, text, context=None):
        """Find the best matching command for the given text.

        Uses token-based longest-match: tokenizes the input, then
        checks each registered phrase (longest first) for a token
        prefix match. Commands from disabled packs are skipped --
        the matcher falls through to the next candidate so a shorter
        enabled-pack command can still fire.

        Args:
            text: raw transcribed text (e.g. "find tab github")

        Returns:
            (CommandEntry, remainder_str) or (None, ''). The remainder is the
            ORIGINAL text after the matched phrase (case, quotes, apostrophes,
            filenames and line breaks intact) -- see match_detail().

        Example:
            match("find tab github")
            -> (CommandEntry("find tab"), "github")
        """
        detail = self.match_detail(text, context)
        if detail is None:
            return None, ''
        return detail.entry, detail.remainder

    def match_detail(self, text, context=None):
        """Like match(), but returns a CommandMatch (or None).

        Matching runs on a normalised VIEW of the utterance -- lowercased,
        punctuation removed per token. Whisper adds trailing punctuation to
        short utterances ("Yes." "Yeah.") which would otherwise stop
        single-word commands like "yes" from ever matching. Every view token
        keeps its offsets into the original text, and the argument handed to
        the command is sliced from the original, never rebuilt from the view.

        context: a command_scope.MatchContext for this utterance; captured
        from the provider when omitted (only if any command is scoped).
        """
        if not text or not self._frozen:
            return None

        tokens = view_tokens(text)
        if not tokens:
            return None
        text_tokens = [norm for norm, _start, _end in tokens]
        clean_lower = ' '.join(text_tokens)
        if context is None and self._has_scoped:
            context = self.current_context()

        # Exact match on the view (fastest path; built-ins win on collision)
        if clean_lower in self._entries:
            entry = self._entries[clean_lower]
            if self._pack_enabled(entry.pack) and self.is_live(entry, context):
                return CommandMatch(entry, text_tokens, len(text), '', '')
            # The user said exactly a disabled pack's phrase, or an
            # out-of-scope command's: a miss, never a different command
            # (queue 58 / 68). disabled_pack_for() / out_of_scope_for() say why.
            return None

        # Token prefix matching: longest registered phrase first.
        # Skip entries whose pack is disabled or whose scope is not live --
        # the loop continues so a shorter candidate can still match.
        for phrase_tokens, entry in self._match_table:
            if not self._pack_enabled(entry.pack):
                continue
            if entry.scope is not None and not self.is_live(entry, context):
                continue
            n = len(phrase_tokens)
            if n <= len(text_tokens) and text_tokens[:n] == phrase_tokens:
                start = tokens[n - 1][2]
                return CommandMatch(
                    entry, phrase_tokens, start,
                    argument_text(text, start),
                    ' '.join(text_tokens[n:]),
                )

        return None

    def disabled_pack_for(self, text):
        """The pack name when ``text`` is exactly a phrase of a DISABLED pack
        (so match() treated it as a miss), else None."""
        if not text or not self._frozen:
            return None
        clean_lower = ' '.join(norm for norm, _s, _e in view_tokens(text))
        entry = self._entries.get(clean_lower)
        if entry is None or self._pack_enabled(entry.pack):
            return None
        return entry.pack

    def should_suppress(self, entry) -> bool:
        """Return True if the entry's debounce window has not elapsed.

        Only meaningful when the caller is in command mode; suppresses
        accidental double-fires of media/navigation/destructive commands.
        """
        if entry.debounce <= 0:
            return False
        with self._exec_lock:
            last = self._last_executions.get(entry.phrase, 0.0)
            return (time.monotonic() - last) < entry.debounce

    def record_execution(self, entry) -> None:
        """Record that `entry` was just executed (for debounce tracking)."""
        if entry.debounce > 0:
            with self._exec_lock:
                self._last_executions[entry.phrase] = time.monotonic()

    def list_commands(self):
        """Return all unique registered commands (for debug/settings)."""
        seen = set()
        result = []
        for entry in self._sorted:
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            result.append({
                'phrase': entry.phrase,
                'source': entry.source,
                'type': entry.cmd_type,
                'aliases': entry.aliases,
                'pack': entry.pack,
                'description': entry.description,
                'ai_visible': entry.ai_visible,
                'risk_class': entry.risk_class,
                'ai_composable': entry.ai_composable,
                'side_effects': entry.side_effects,
                'side_effect_category': entry.side_effects,
                'preconditions': entry.preconditions,
                'voice_triggerable': entry.voice_triggerable,
                'param_schema': entry.param_schema,
                'reversible': entry.reversible,
                'preview_template': entry.preview_template,
                'metadata': dict(entry.metadata),
                'scope': entry.scope.to_json() if entry.scope is not None else None,
            })
        return result

    def detect_collisions(self):
        """Check for prefix collisions and print warnings.

        A collision is when a short phrase is a token prefix of a
        longer phrase (e.g. "find" vs "find tab"). With longest-match
        this is handled correctly, but it's worth logging at startup so
        users can see why "find" sometimes doesn't fire Ctrl+F.

        Returns the list of collision tuples (shorter, longer) so tests
        can assert on them.
        """
        collisions = []
        phrases = [e.phrase for e in self._sorted]
        for i, longer in enumerate(phrases):
            longer_tokens = longer.split()
            for shorter in phrases[i + 1:]:
                shorter_tokens = shorter.split()
                if (len(shorter_tokens) < len(longer_tokens) and
                        longer_tokens[:len(shorter_tokens)] == shorter_tokens):
                    print(f"[REGISTRY] Prefix overlap: '{shorter}' is a "
                          f"prefix of '{longer}' (longest-match resolves "
                          f"in favor of '{longer}')")
                    collisions.append((shorter, longer))
        return collisions
