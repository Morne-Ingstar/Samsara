"""Linux import and unavailable-service contract for LP03."""

import ctypes
import importlib
import io
import json
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux ctypes exports")
def test_native_core_modules_import_and_report_unavailable_services():
    assert not hasattr(ctypes, "windll")
    assert not hasattr(ctypes, "WINFUNCTYPE")
    assert not hasattr(ctypes, "HRESULT")

    mouse_hook = importlib.import_module("samsara.mouse_hook")
    ducking_host = importlib.import_module("samsara.ducking_host")
    overlay = importlib.import_module("samsara.ui.numbers_overlay_qt")

    hook = mouse_hook.MouseHook(lambda *_: None)
    assert hook.start() is False
    assert "unavailable" in hook.install_error.lower()

    output = io.StringIO()
    assert ducking_host.main(stdin=io.StringIO(), stdout=output) == 1
    outcome = json.loads(output.getvalue())
    assert outcome["available"] is False
    assert "unavailable" in outcome["err"].lower()

    assert overlay._win32_monitor_rects() == []
