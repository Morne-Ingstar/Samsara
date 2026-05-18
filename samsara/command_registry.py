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
- Disabled-pack commands are skipped in match(). If the longest match belongs
  to a disabled pack the matcher falls through to the next match so a shorter
  enabled-pack command can still fire.
"""

import re
import threading
import time


class CommandEntry:
    """A single registered command (built-in or plugin)."""

    def __init__(self, phrase, source, cmd_type, data=None, handler=None,
                 aliases=None, pack='core', debounce=0.0, app_overrides=None,
                 description=''):
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
        prefix match. Commands from disabled packs are skipped --
        the matcher falls through to the next candidate so a shorter
        enabled-pack command can still fire.

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
        # Strip punctuation from tokens for matching only.
        # Whisper adds trailing punctuation to short utterances ("Yes." "Yeah.")
        # which prevents single-word commands like "yes" from ever matching.
        # The original text is NOT modified here — callers hold the raw string
        # and use it for dictation paste if no command matches.
        clean_lower = re.sub(r'[^\w\s]', '', text_lower)
        text_tokens = clean_lower.split()

        if not text_tokens:
            return None, ''

        # Exact match on cleaned text (fastest path; built-ins win on collision)
        if clean_lower in self._entries:
            entry = self._entries[clean_lower]
            if self._pack_enabled(entry.pack):
                return entry, ''
            # Fall through to prefix scan if exact match is from disabled pack

        # Token prefix matching: longest registered phrase first.
        # Skip entries whose pack is disabled -- the loop continues so a
        # shorter enabled-pack command can still match.
        for phrase_tokens, entry in self._match_table:
            if not self._pack_enabled(entry.pack):
                continue
            n = len(phrase_tokens)
            if n <= len(text_tokens) and text_tokens[:n] == phrase_tokens:
                remainder = ' '.join(text_tokens[n:])
                return entry, remainder

        return None, ''

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
