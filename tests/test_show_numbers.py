"""Tests for the show_numbers plugin — overlay management and click parsing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub uiautomation so the module loads cleanly even when not installed.
if 'uiautomation' not in sys.modules:
    sys.modules['uiautomation'] = MagicMock()

# Stub win32 modules if absent.
for _mod in ('win32api', 'win32con', 'win32gui'):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import plugins.commands.show_numbers as sn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_element(rect=(10, 20, 110, 60)):
    ctrl = MagicMock()
    br = MagicMock()
    br.left, br.top, br.right, br.bottom = rect
    ctrl.BoundingRectangle = br
    return {'control': ctrl, 'rect': rect, 'control_type': 'ButtonControl', 'name': 'OK'}


def _populate_state(n=3):
    with sn._overlay_lock:
        sn._current_elements[:] = [MagicMock() for _ in range(n)]
        sn._current_overlays[:] = [MagicMock() for _ in range(n)]


def _clear_state():
    sn._cancel_dismiss_timer()
    with sn._overlay_lock:
        sn._current_overlays.clear()
        sn._current_elements.clear()


# ---------------------------------------------------------------------------
# _parse_click_target
# ---------------------------------------------------------------------------

class TestParseClickTarget:

    def test_simple_number(self):
        assert sn._parse_click_target('7') == (7, 'single')

    def test_double_via_twice(self):
        assert sn._parse_click_target('7 twice') == (7, 'double')

    def test_double_via_double_word(self):
        assert sn._parse_click_target('double 7') == (7, 'double')

    def test_right_click(self):
        assert sn._parse_click_target('right 5') == (5, 'right')

    def test_spoken_seven(self):
        assert sn._parse_click_target('seven') == (7, 'single')

    def test_spoken_twelve(self):
        assert sn._parse_click_target('twelve') == (12, 'single')

    def test_no_number_returns_none(self):
        number, _ = sn._parse_click_target('hello')
        assert number is None

    def test_strips_click_verb(self):
        assert sn._parse_click_target('click 3') == (3, 'single')

    def test_strips_tap_verb(self):
        assert sn._parse_click_target('tap 5') == (5, 'single')

    def test_two_digit_number(self):
        assert sn._parse_click_target('15') == (15, 'single')

    def test_spoken_number_with_twice(self):
        number, modifier = sn._parse_click_target('seven twice')
        assert number == 7
        assert modifier == 'double'

    def test_right_with_spoken_number(self):
        number, modifier = sn._parse_click_target('right seven')
        assert number == 7
        assert modifier == 'right'

    def test_whitespace_only(self):
        number, _ = sn._parse_click_target('   ')
        assert number is None

    def test_default_modifier_is_single(self):
        _, modifier = sn._parse_click_target('3')
        assert modifier == 'single'


# ---------------------------------------------------------------------------
# _hide_overlays
# ---------------------------------------------------------------------------

class TestHideOverlays:

    def setup_method(self):
        _populate_state(3)

    def teardown_method(self):
        _clear_state()

    def test_hide_clears_overlay_list(self):
        sn._hide_overlays()
        with sn._overlay_lock:
            assert sn._current_overlays == []

    def test_hide_clears_element_list(self):
        sn._hide_overlays()
        with sn._overlay_lock:
            assert sn._current_elements == []

    def test_hide_calls_destroy_on_each_overlay(self):
        mocks = [MagicMock(), MagicMock()]
        with sn._overlay_lock:
            sn._current_overlays[:] = mocks
        sn._hide_overlays()
        for m in mocks:
            m.destroy.assert_called_once()

    def test_hide_tolerates_destroy_exception(self):
        bad = MagicMock()
        bad.destroy.side_effect = RuntimeError("already destroyed")
        with sn._overlay_lock:
            sn._current_overlays[:] = [bad]
        sn._hide_overlays()  # must not raise

    def test_hide_cancels_dismiss_timer(self):
        import threading
        t = threading.Timer(60, lambda: None)
        t.daemon = True
        t.start()
        sn._dismiss_timer = t
        sn._hide_overlays()
        assert sn._dismiss_timer is None
        t.cancel()

    def test_hide_idempotent_on_empty_state(self):
        sn._hide_overlays()
        sn._hide_overlays()  # second call must not raise


# ---------------------------------------------------------------------------
# _show_overlays
# ---------------------------------------------------------------------------

class TestShowOverlays:

    def teardown_method(self):
        _clear_state()

    def test_no_elements_logs_and_does_not_crash(self, capsys):
        with patch.object(sn, 'enumerate_clickable_elements', return_value=[]):
            sn._show_overlays(None)
        out = capsys.readouterr().out
        assert 'No clickable' in out

    def test_elements_stored_in_current_elements(self):
        elements = [_make_element() for _ in range(5)]
        with patch.object(sn, 'enumerate_clickable_elements', return_value=elements):
            sn._show_overlays(None)
        with sn._overlay_lock:
            assert len(sn._current_elements) == 5

    def test_capped_at_99_elements(self):
        elements = [_make_element() for _ in range(200)]
        with patch.object(sn, 'enumerate_clickable_elements', return_value=elements):
            sn._show_overlays(None)
        with sn._overlay_lock:
            assert len(sn._current_elements) == 99

    def test_enumeration_exception_handled_gracefully(self, capsys):
        with patch.object(sn, 'enumerate_clickable_elements',
                          side_effect=RuntimeError("UIA crashed")):
            sn._show_overlays(None)  # must not raise
        out = capsys.readouterr().out
        assert 'failed' in out.lower()

    def test_clears_previous_overlays_before_show(self):
        _populate_state(2)
        old_overlays = list(sn._current_overlays)
        elements = [_make_element()]
        with patch.object(sn, 'enumerate_clickable_elements', return_value=elements):
            sn._show_overlays(None)
        for m in old_overlays:
            m.destroy.assert_called()

    def test_no_overlay_widgets_without_root(self):
        elements = [_make_element() for _ in range(3)]
        with patch.object(sn, 'enumerate_clickable_elements', return_value=elements):
            sn._show_overlays(None)
        with sn._overlay_lock:
            assert sn._current_overlays == []  # no widgets without a tk root


# ---------------------------------------------------------------------------
# handle_click
# ---------------------------------------------------------------------------

class TestHandleClick:

    def setup_method(self):
        _populate_state(3)

    def teardown_method(self):
        _clear_state()

    def test_click_valid_element(self):
        with patch.object(sn, '_do_click') as mock_click, \
             patch.object(sn, '_hide_overlays'):
            sn.handle_click(None, '2')
        mock_click.assert_called_once()

    def test_click_selects_correct_element(self):
        elem2 = sn._current_elements[1]
        with patch.object(sn, '_do_click') as mock_click, \
             patch.object(sn, '_hide_overlays'):
            sn.handle_click(None, '2')
        args = mock_click.call_args[0]
        assert args[0] is elem2

    def test_click_passes_modifier(self):
        with patch.object(sn, '_do_click') as mock_click, \
             patch.object(sn, '_hide_overlays'):
            sn.handle_click(None, '1 twice')
        assert mock_click.call_args[0][1] == 'double'

    def test_click_out_of_range_logs_error(self, capsys):
        with patch.object(sn, '_do_click') as mock_click:
            sn.handle_click(None, '10')
        mock_click.assert_not_called()
        out = capsys.readouterr().out
        assert '10' in out

    def test_click_hides_after_success(self):
        with patch.object(sn, '_do_click'), \
             patch.object(sn, '_hide_overlays') as mock_hide:
            sn.handle_click(None, '1')
        mock_hide.assert_called_once()

    def test_click_does_not_hide_on_out_of_range(self):
        with patch.object(sn, '_do_click'), \
             patch.object(sn, '_hide_overlays') as mock_hide:
            sn.handle_click(None, '99')
        mock_hide.assert_not_called()

    def test_empty_remainder_returns_true(self):
        result = sn.handle_click(None, '')
        assert result is True

    def test_whitespace_remainder_returns_true(self):
        result = sn.handle_click(None, '   ')
        assert result is True

    def test_unparseable_returns_true(self, capsys):
        result = sn.handle_click(None, 'hello world')
        assert result is True

    def test_no_overlay_active_message(self, capsys):
        _clear_state()
        sn.handle_click(None, '1')
        out = capsys.readouterr().out
        assert 'No overlay' in out

    def test_spoken_number_click(self):
        with patch.object(sn, '_do_click') as mock_click, \
             patch.object(sn, '_hide_overlays'):
            sn.handle_click(None, 'two')
        mock_click.assert_called_once()
        args = mock_click.call_args[0]
        assert args[0] is sn._current_elements[1]

    def test_right_click_modifier(self):
        with patch.object(sn, '_do_click') as mock_click, \
             patch.object(sn, '_hide_overlays'):
            sn.handle_click(None, 'right 1')
        assert mock_click.call_args[0][1] == 'right'


# ---------------------------------------------------------------------------
# handle_show_numbers / handle_hide_numbers
# ---------------------------------------------------------------------------

class TestCommandHandlers:

    def teardown_method(self):
        _clear_state()

    def test_show_numbers_returns_true(self):
        with patch.object(sn, '_show_overlays'):
            result = sn.handle_show_numbers(None, '')
        assert result is True

    def test_hide_numbers_returns_true(self):
        with patch.object(sn, '_hide_overlays'):
            result = sn.handle_hide_numbers(None, '')
        assert result is True

    def test_show_numbers_calls_show_overlays(self):
        with patch.object(sn, '_show_overlays') as mock_show:
            sn.handle_show_numbers(None, '')
        mock_show.assert_called_once()

    def test_hide_numbers_calls_hide_overlays(self):
        with patch.object(sn, '_hide_overlays') as mock_hide:
            sn.handle_hide_numbers(None, '')
        mock_hide.assert_called_once()
