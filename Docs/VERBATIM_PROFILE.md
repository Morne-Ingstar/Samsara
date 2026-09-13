# Verbatim profile — dictating code, commands and URLs

Normal dictation is tuned for prose: it capitalises sentences, adds punctuation,
and runs an LLM cleanup pass. In a terminal or a URL bar that is all wrong:

| You say | Normally you get | With verbatim |
|---|---|---|
| echo hello | `Echo, hello?` | `echo hello` |
| git log dash dash oneline dash five | `Git log dash dash 1 line dash 5.` | `git log --oneline -5` |
| github dot com | `Github. com.` | `github.com` |

The profile turns **off** capitalisation, auto-punctuation, the trailing period,
smart quotes and smart corrections, and turns **on** a spoken-symbol map.

## When it turns on

In precedence order — the first one that matches wins:

1. **You said so.** `"literal on"` (or `"verbatim on"`) forces it everywhere until
   you say `"literal off"` / `"verbatim off"`, or the hands-free session ends.
   The preview box shows a `[literal]` badge while it is forced.
2. **You are in a terminal.** Warp, Windows Terminal, cmd, PowerShell, pwsh,
   conhost, Alacritty, WezTerm, VS Code.
3. **You are in a browser address bar.** Chrome/Edge/Brave/Vivaldi omnibox and
   the Firefox URL bar.
4. Otherwise, normal dictation.

When the profile is active you get one log line per utterance saying which rule
fired, e.g. `[VERBATIM] rule=process:warp.exe applied: ... -> ...`.

## Spoken symbols

Say the name, get the character. Case does not matter, and it only fires on a
whole word — "dashboard" is still a word.

| Say | Get | | Say | Get |
|---|---|---|---|---|
| dash, minus, hyphen | `-` | | caret | `^` |
| dash dash, double dash | `--` | | ampersand | `&` |
| dot, period, point | `.` | | asterisk, star | `*` |
| slash | `/` | | equals | `=` |
| backslash | `\` | | plus | `+` |
| underscore | `_` | | open/close paren | `(` `)` |
| colon | `:` | | open/close bracket | `[` `]` |
| semicolon | `;` | | open/close brace | `{` `}` |
| pipe | `\|` | | open/close angle | `<` `>` |
| tilde | `~` | | single quote | `'` |
| at sign | `@` | | double quote | `"` |
| hash, pound | `#` | | space | (a space) |
| dollar | `$` | | tab | (a tab) |
| percent | `%` | | newline, enter | (a newline) |

## Spacing

These get **no space on either side**, so words either side run together:

```
.  /  \  _  :  -  @
```

That is what makes `github dot com` → `github.com` and `a slash b` → `a/b`.
Everything else gets a single space, brackets hug their contents, and `$`, `#`,
`~` stick to what follows (`dollar HOME slash bin` → `$HOME/bin`).

**Two rules worth knowing:**

- A dash **after a word** starts a new flag: `ls dash la` → `ls -la`, not
  `ls-la`. In a terminal that is almost always what you want. The cost: a
  hyphenated word (`well dash known`) comes out `well -known` — spell it or type
  the hyphen instead.
- Because `/` never takes a space, `cd slash usr` gives `cd/usr`. Say the space:
  `cd space slash usr slash local` → `cd /usr/local`.

## Numbers

A spelled digit becomes a numeral **next to a symbol or another digit**:

- `dash five` → `-5`
- `port colon eight zero eight zero` → `port:8080`
- `one line` stays `one line` — two ordinary words

## Spelling things out

- `spell g i t` → `git`
- `spell cap g i t` → `Git` — `cap` capitalises the next letter only

Use it for anything the recogniser mangles, or to get a hyphen without the flag
spacing rule.

## Settings

All optional — the defaults are what is described above.

```jsonc
"verbatim": {
  "enabled": true,            // master switch
  "address_bar": true,        // detect browser URL bars
  "processes": [              // replaces the built-in list
    "warp", "windowsterminal", "cmd", "powershell", "pwsh",
    "conhost", "alacritty", "wezterm", "code"
  ]
}
```

Process names are matched case-insensitively, with or without `.exe`.

## Extending it

Everything is a table at the top of `samsara/verbatim.py` — no code to change:

- `SYMBOLS` — one spoken word → one character
- `PHRASE_SYMBOLS` — several words → one character (matched first, longest first)
- `DIGITS` — spelled digits
- `NO_SPACE_AROUND` / `NO_SPACE_AFTER` / `NO_SPACE_BEFORE` — the spacing rules
- `DEFAULT_PROCESSES` — the terminal list
- `ADDRESS_BAR_AUTOMATION_IDS` / `ADDRESS_BAR_NAME_MARKERS` — URL bar detection
- `TOGGLE_ON` / `TOGGLE_OFF` — the spoken toggle phrases

Add a row, restart Samsara, done. For example, to say "bang" for `!`:

```python
SYMBOLS = {
    ...
    "bang": "!",
}
```

## Known limits

- If Whisper hears `oneline` as two words (`one line`), the profile cannot rejoin
  them — it has no dictionary. Say `spell o n e l i n e`, or accept the space.
- The address-bar rule needs the `uiautomation` package; without it that rule is
  simply skipped (the process list and the toggle still work).
