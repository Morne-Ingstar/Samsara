"""595 must not retire another test's queued Qt objects during teardown."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from tests import test_friendlier_settings_595 as friendlier


def test_595_teardown_leaves_unrelated_deferred_delete_for_its_owner(qapp, monkeypatch):
    fixture = friendlier.window.__wrapped__(qapp, monkeypatch)
    window = next(fixture)
    unrelated = QWidget()
    destroyed = []
    unrelated.destroyed.connect(lambda: destroyed.append('unrelated'))
    window.destroyed.connect(lambda: destroyed.append('settings'))
    try:
        # Exercise the last CI-reported test, not a substitute Settings tree.
        friendlier.test_notice_is_lazy_one_per_page_and_dismiss_hides_all_pages(window)
        children = window.findChildren(QWidget)
        # Earlier fixtures can leave deletes queued after their patches end.
        # A harmless recorder stands in for their now-unsafe native callbacks.
        unrelated.deleteLater()
        with pytest.raises(StopIteration):
            next(fixture)
        assert not isValid(window)
        assert all(not isValid(child) for child in children)
        assert isValid(unrelated), '595 teardown delivered another fixture\'s delete'
        assert destroyed == ['settings']

        # Reaching the following real 595 path also proves teardown returned.
        reopened = friendlier.window.__wrapped__(qapp, monkeypatch)
        next_window = next(reopened)
        try:
            friendlier.test_dismissal_resets_when_settings_reopens(next_window, qapp)
        finally:
            with pytest.raises(StopIteration):
                next(reopened)
        assert not isValid(next_window)
        assert isValid(unrelated)
    finally:
        fixture.close()
        if isValid(window):
            window.hide()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
        if isValid(unrelated):
            QCoreApplication.sendPostedEvents(unrelated, QEvent.Type.DeferredDelete)
        assert not isValid(unrelated)


def test_595_reopen_does_not_leave_retired_visit_trees_for_later_tests(qapp, monkeypatch):
    fixture = friendlier.window.__wrapped__(qapp, monkeypatch)
    window = next(fixture)
    retired = [window.centralWidget(), window._source_store, window._search_debounce]
    try:
        friendlier.test_dismissal_resets_when_settings_reopens(window, qapp)
        with pytest.raises(StopIteration):
            next(fixture)
        assert not isValid(window)
        assert all(not isValid(obj) for obj in retired), 'retired visit escaped Settings ownership'
    finally:
        fixture.close()
        for obj in [window, *retired]:
            if isValid(obj):
                obj.deleteLater()
                QCoreApplication.sendPostedEvents(obj, QEvent.Type.DeferredDelete)
