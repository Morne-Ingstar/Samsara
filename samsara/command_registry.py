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
"""


class CommandEntry:
    """A single registered command (built-in or plugin)."""

    def __init__(self, phrase, source, cmd_type, data=None, handler=None,
                 aliases=None):
        """
        Args:
            phrase: canonical trigger phrase (lowercase, stripped)
            source: 'builtin' or 'plugin'
            cmd_type: 'hotkey', 'launch', 'text', 'plugin', etc.
            data: dict from commands.json (for built-ins)
            handler: callable (for plugins)
            aliases: list of alternative trigger phrases
        """
        self.phrase = phrase.lower().strip()
        self.tokens = self.phrase.split()
        self.token_count = len(self.tokens)
        self.source = source
        self.cmd_type = cmd_type
        self.data = data or {}
        self.handler = handler
        self.aliases = [a.lower().strip() for a in (aliases or [])]


class CommandMatcher:
    """Token-based longest-match command matcher.

    Usage:
        matcher = CommandMatcher()
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
            )
            self._entries[name_lower] = entry

    def load_plugins(self, plugin_registry):
        """Load plugin commands from plugin_commands._REGISTRY.

        Args:
            plugin_registry: dict of phrase -> {func, phrase, aliases, source}

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

            entry = CommandEntry(
                phrase=canonical,
                source='plugin',
                cmd_type='plugin',
                handler=entry_data['func'],
                aliases=entry_data.get('aliases', []),
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

        self._frozen = True

        unique_entries = len(self._sorted)
        total_phrases = len(self._entries)
        print(f"[REGISTRY] Frozen: {unique_entries} commands, "
              f"{total_phrases} phrases (including aliases)")

    def match(self, text):
        """Find the best matching command for the given text.

        Uses token-based longest-match: tokenizes the input, then
        checks each registered phrase (longest first) for a token
        prefix match.

        Args:
            text: raw transcribed text (e.g. "find tab github")

        Returns:
            (CommandEntry, remainder_str) or (None, '')

        Example:
            match("find tab github")
            -> (CommandEntry("find tab"), "github")
        """
        if not text or not self._frozen:
            return None, ''

        text_lower = text.lower().strip()
        text_tokens = text_lower.split()

        if not text_tokens:
            return None, ''

        # Exact match first (fastest path; also guarantees built-in wins
        # over plugin for identical phrases since built-ins load first)
        if text_lower in self._entries:
            return self._entries[text_lower], ''

        # Token prefix matching: longest registered phrase first
        for phrase_tokens, entry in self._match_table:
            n = len(phrase_tokens)
            if n <= len(text_tokens) and text_tokens[:n] == phrase_tokens:
                remainder = ' '.join(text_tokens[n:])
                return entry, remainder

        return None, ''

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
