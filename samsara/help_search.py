"""Deterministic offline answers from the help already shipped with Samsara.

Matching is deliberately separate from command-lane admission in help_intents.
No query, profile, catalogue file or network is read or saved here.
"""
from dataclasses import dataclass
from functools import lru_cache
import re

from samsara.help_terms import TERMS, PAGE_HELP


_STOP = frozenset("a an the i my me do does can could how where what is are s "
                  "there to for of and or in on with it its this that please "
                  "choose settings help".split())


def _tokens(text):
    return tuple(t for t in re.findall(r"[a-z0-9]+", str(text).casefold()) if t not in _STOP)


def short_summary(body):
    sentence = re.split(r'(?<=[.!?])\s+', body.strip(), maxsplit=1)[0]
    if len(sentence) <= 180:
        return sentence
    return sentence[:176].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'


@dataclass(frozen=True)
class HelpEntry:
    kind: str
    id: str
    title: str
    body: str
    aliases: tuple[str, ...] = ()

    @property
    def summary(self):
        return short_summary(self.body)


@dataclass(frozen=True)
class RankedEntry:
    entry: HelpEntry
    score: float


@lru_cache(maxsize=1)
def entries():
    # Import only packaged constants, never a window or the live resolvers.
    from samsara.help_intents import TARGETS
    from samsara.ui.quick_reference_qt import _MODE_DESCRIPTIONS
    result = [HelpEntry('term', key, term.title, term.body)
              for key, term in TERMS.items()]
    result.extend(HelpEntry('page', name, name + ' help', body)
                  for name, body in PAGE_HELP.items())
    for mode, body in _MODE_DESCRIPTIONS.items():
        key = mode.name.casefold()
        # These two substitutions are the only dynamic slots in the static prose.
        body = body.replace('{commit_help}', '').replace('{submit_help}', '')
        body = ' '.join(body.split())
        # PLACEHOLDER COPY (1135): names for existing Quick Reference sections.
        title = 'Hands-free dictation mode' if key == 'dictate' else key.capitalize() + ' mode'
        result.append(HelpEntry('quick_reference', key, title, body))
    for key, target in TARGETS.items():
        # A search result explains destinations; it never follows a web link or
        # runs a command, even when a target happens to be an external action.
        title = target.topics[0].removeprefix('the ').capitalize()
        # PLACEHOLDER COPY (1135); all topic/task words come from 943's table.
        body = title + ': ' + '; '.join(target.tasks) + '.'
        result.append(HelpEntry('place', key, title, body, target.topics + target.tasks))
    return tuple(result)


def search(query, *, limit=3):
    """Rank useful lexical matches; generic or unrelated questions get no hit.

    Coverage prevents one incidental word in a long question from choosing a
    destination. Stable source order resolves ties; no fuzzy/domain guessing.
    """
    words = _tokens(query)
    wanted = set(words)
    if not wanted:
        return ()
    ranked = []
    explained_titles = {tuple(_tokens(e.title)) for e in entries() if e.kind in {'term', 'page'}}
    for entry in entries():
        title = _tokens(entry.title)
        if entry.kind == 'place' and title in explained_titles:
            continue
        body = _tokens(' '.join((entry.body, *entry.aliases)))
        overlap = wanted & set(title + body)
        coverage = len(overlap) / len(wanted)
        if coverage < .5 or not overlap:
            continue
        # A single generic word in body prose is too weak for an answer.
        title_hits = len(wanted & set(title))
        if len(overlap) < 2 and not (title and set(title) <= wanted):
            continue
        title_phrase = bool(title) and (' ' + ' '.join(title) + ' ') in (' ' + ' '.join(words) + ' ')
        pairs = set(zip(words, words[1:])) & set(zip(body, body[1:]))
        score = coverage * 10 + title_hits * 3 + title_phrase * 5 + min(len(pairs), 3)
        ranked.append(RankedEntry(entry, score))
    # Prefer explanations over navigation metadata at equal strength.
    priority = {'term': 0, 'page': 1, 'quick_reference': 2, 'place': 3}
    ranked.sort(key=lambda hit: (-hit.score, priority[hit.entry.kind]))
    return tuple(ranked[:max(0, limit)])


def answer_request(app, request, intent):
    """Called only after 943/608 have admitted a whole help request."""
    definition = re.match(r"^what(?: is|'s)\s+", request)
    if intent.target not in {'unknown', 'confusion'} and not definition:
        return False
    hits = search(request)
    if not hits:
        return False
    # Exact definition phrases change only when an explanation exists, not a
    # loose body match. Other exact 943 destinations retain their old behavior.
    if intent.target not in {'unknown', 'confusion'}:
        topic = set(_tokens(request[definition.end():]))
        hits = tuple(h for h in hits if h.entry.kind == 'term'
                     and topic == set(_tokens(h.entry.title)))
        if not hits:
            return False
    from plugins.commands.core_utils import speak_if_available
    from samsara.ui.context_help_qt import open_help_entry
    best = hits[0]
    if best.entry.kind == 'term':
        from samsara.help_terms import explanation
        reply = short_summary(explanation(best.entry.id, getattr(app, 'config', {})).body)
    else:
        reply = best.entry.summary
    # No help-choice state exists. Open the best and explicitly name the other,
    # as the brief permits; never offer a numbered command that cannot be used.
    if len(hits) > 1 and best.score - hits[1].score <= 2 and hits[1].entry.title != best.entry.title:
        # PLACEHOLDER COPY (1135)
        reply += ' Showing ' + best.entry.title + '. Another match is ' + hits[1].entry.title + '.'
    open_help_entry(app, best.entry)
    speak_if_available(app, reply)
    return True


def show_entry(app, entry):
    """UI-thread implementation of context_help_qt's single public opener."""
    from samsara.help_terms import HelpTerm
    from samsara.ui.context_help_qt import ExplanationCard, HelpController
    if entry.kind == 'quick_reference':
        from samsara.ui.quick_reference_qt import QuickReferenceQt
        wrapper = getattr(app, '_quick_reference_qt', None)
        if wrapper is None:
            wrapper = app._quick_reference_qt = QuickReferenceQt(app)
        # Already on the UI thread: finish initialization before touching it.
        if wrapper._window is None:
            wrapper._init_window()
        else:
            wrapper._window.refresh()
        window = wrapper._window
        if not hasattr(window, '_voice_help'):
            window._voice_help = HelpController(window, lambda: app.config)
        controller = window._voice_help
        controller.dismiss(False)
        # refresh() discards the old body, including any previous explanation.
        card = ExplanationCard(controller)
        window._body_layout.insertWidget(1, card)
        window.show()
        controller.toggle(entry.id, window._refresh_btn, card, page=window,
                          term=HelpTerm(entry.id, entry.title, entry.body, None))
    elif entry.kind == 'page' and entry.id not in {'General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help'}:
        from samsara.ui import qt_runtime
        app.show_main_window()

        def select():
            if not app.open_hub_page(entry.id):
                raise RuntimeError('Samsara could not open ' + entry.id)
            # open_hub_page itself posts selection. Queue the explanation only
            # after that callback, including the cold-window construction path.
            qt_runtime.post(lambda: app.main_window._window._local_page_help.open(app.main_window._window))

        qt_runtime.post(select)
        return
    else:
        from samsara.ui.settings_qt import SettingsQt
        wrapper = getattr(app, '_settings_qt', None)
        if wrapper is None:
            wrapper = app._settings_qt = SettingsQt(app)
        wrapper._show_preferences(entry.id if entry.kind == 'page' else 'Help')
        window = wrapper._window
        if entry.kind == 'page':
            local = window._local_page_help
            local.controller.dismiss(False)
            local.open(window._sidebar)
        else:
            page = window._stack.currentWidget()
            controller, card = page._offline_help, page._help_card
            controller.dismiss(False)
            term = None if entry.kind == 'term' else HelpTerm(entry.id, entry.title, entry.body, None)
            controller.toggle(entry.id, window._widgets['setupGuides'], card, page=page, term=term)
    from samsara.ui.window_activation import surface_requested
    surface_requested(window)
