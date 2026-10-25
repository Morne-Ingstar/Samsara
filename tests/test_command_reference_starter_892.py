"""892: pinned first commands reuse Home's safe example picker."""
from samsara import command_catalog
from samsara.ui import command_cheatsheet_qt as cs, command_marquee


def _rows():
    phrases = ["open settings", "scroll down", "show windows", "scratch that",
               "new line", "what can I say", "open printer", "close alarm"]
    return [dict(phrase=phrase, canonical_id={"scratch that": "builtin.scratch_that", "new line": "builtin.new_line", "what can I say": "core_utils.what_can_i_say"}.get(phrase, f"fixture.{i}"), plugin="builtin",
                 pack="core", args=[], aliases=[], display_aliases=[],
                 description=f"Description {i}", risk="ui", whole_utterance=False,
                 needs_ava=False, live=True)
            for i, phrase in enumerate(phrases)]


def _open(qapp, tmp_path, monkeypatch):
    rows = _rows()
    monkeypatch.setattr(cs, "catalog_rows", lambda _: rows)
    monkeypatch.setattr(cs, "_scope_context", lambda: None)
    monkeypatch.setattr(cs, "_disabled_packs", lambda *a: set())
    monkeypatch.setattr(cs, "_foreground_screen", lambda _: None, raising=False)
    win = cs._CheatSheetWindow(lambda _: None, lambda: [], tmp_path / "palette.json",
                               config_cb=lambda: {})
    win.show()
    qapp.processEvents()
    return win


def _starter_phrases(win):
    items = [win._list.item(i) for i in range(win._list.count())]
    assert items[0].data(cs.Qt.ItemDataRole.UserRole + 4)
    phrases = []
    for item in items:
        phrases.append(item.data(cs.Qt.ItemDataRole.UserRole))
        if item.data(cs.Qt.ItemDataRole.UserRole + 5):
            break
    return items, 0, phrases


def test_start_here_is_first_is_bounded_safe_and_uses_home_picker(qapp, tmp_path, monkeypatch):
    calls = []

    def picker(rows, *, ctx=None, count, rng):
        calls.append((rows, count, rng))
        return ["open settings", "scroll down", "show windows", "scratch that", "new line", "what can I say"]

    monkeypatch.setattr(command_marquee, "example_phrases", picker)
    win = _open(qapp, tmp_path, monkeypatch)
    try:
        items, header, phrases = _starter_phrases(win)
        assert header == 0 and items[0].data(cs.Qt.ItemDataRole.UserRole + 4)
        assert phrases == ["open settings", "scroll down", "show windows",
                           "scratch that", "new line", "what can I say"]
        assert len(phrases) <= 8 and calls
        for row in cs.starter_rows(win._all):
            assert row["plugin"] == "builtin" and not row["needs_ava"]
            assert row["risk"] in command_marquee.SAFE_RISKS
            assert row["pack"] in command_marquee.EXAMPLE_ALLOWED_PACKS
            assert not row["whole_utterance"]
            assert not any(arg.get("required") for arg in row["args"]
                           if isinstance(arg, dict))
            assert row.get("plugin") not in command_catalog.EXAMPLE_EXCLUDED_PLUGINS
            assert row.get("verb") not in command_catalog.EXAMPLE_EXCLUDED_VERBS
        ids = [item.data(cs.Qt.ItemDataRole.UserRole + 1) for item in items]
        assert len(ids) == len(set(ids)) == len(win._all)
        assert set(ids) == {row["canonical_id"] for row in win._all}
        tail = [item.data(cs.Qt.ItemDataRole.UserRole) for item in items[len(phrases):]]
        assert tail == [row["phrase"] for row in win._all if row["phrase"] not in phrases]
        first = items[0]
        win._list.setCurrentItem(first)
        win._on_item_clicked(first)
        assert win._detail_card.phrase == first.data(cs.Qt.ItemDataRole.UserRole)
        delegate = win._list.itemDelegate()
        assert delegate.header_height() == 2 * cs.CommandRowDelegate.header_height(delegate)
    finally:
        win.close()


def test_search_and_category_hide_group_then_clearing_restores_it(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(command_marquee, "example_phrases",
                        lambda rows, **kw: ["open settings", "scroll down", "show windows"])
    win = _open(qapp, tmp_path, monkeypatch)
    try:
        assert _starter_phrases(win)[1] == 0
        win._filter.setText("alarm")
        assert all(not item.data(cs.Qt.ItemDataRole.UserRole + 4)
                   for item in (win._list.item(i) for i in range(win._list.count())))
        win._filter.clear()
        win._set_category("not-a-category")
        assert all(not win._list.item(i).data(cs.Qt.ItemDataRole.UserRole + 4)
                   for i in range(win._list.count()))
        win._set_category("All")
        assert _starter_phrases(win)[1] == 0
    finally:
        win.close()


def test_live_only_applies_to_starters_and_order_is_stable_between_opens(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(command_marquee, "example_phrases",
                        lambda rows, **kw: ["open settings", "scroll down", "show windows"])
    first = _open(qapp, tmp_path, monkeypatch)
    try:
        expected = _starter_phrases(first)[2]
    finally:
        first.close()
    second = _open(qapp, tmp_path, monkeypatch)
    try:
        assert _starter_phrases(second)[2] == expected
        second._all[0]["live"] = False
        second._apply_filter("")
        assert "open settings" not in _starter_phrases(second)[2]
    finally:
        second.close()


def test_unsafe_and_unregistered_starters_are_excluded():
    rows = _rows()
    unsafe = [dict(rows[0], canonical_id="unsafe.args", args=[{"required": True}]),
              dict(rows[0], canonical_id="unsafe.ava", needs_ava=True),
              dict(rows[0], canonical_id="unsafe.risk", risk="destructive"),
              dict(rows[0], canonical_id="unsafe.pack", pack="integration"),
              dict(rows[0], canonical_id="unsafe.plugin", plugin="integration"),
              dict(rows[0], canonical_id="unsafe.whole", whole_utterance=True)]
    for row in unsafe:
        row["phrase"] = row["canonical_id"]
    chosen = cs.starter_rows(rows + unsafe)
    assert chosen and len(chosen) <= 8
    assert all(not row["canonical_id"].startswith("unsafe.") for row in chosen)

