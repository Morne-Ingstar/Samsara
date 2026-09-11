import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "probes" / "inject_matrix.py"
spec = importlib.util.spec_from_file_location("inject_matrix", MODULE_PATH)
inject_matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inject_matrix)

def test_sentence_comparison_is_exact():
    assert inject_matrix.compare_sentence(inject_matrix.TEXT)
    assert not inject_matrix.compare_sentence(inject_matrix.TEXT + " ")
    assert inject_matrix.summarize(inject_matrix.TEXT) == "PASS"
    assert inject_matrix.summarize("wrong") == "FAIL"

def test_strategy_table_has_production_and_experiments():
    table = inject_matrix.build_strategy_table("sample")
    assert "prod_paste_with_preservation" in table
    assert "prod_type_text_unicode" in table
    assert "experiment_clipboard_ctrl_v" in table
    assert all(name.startswith(("prod_", "experiment_")) for name in table)

def test_gate_selects_only_production_clipboard():
    assert inject_matrix.strategy_names(True, "experiment_uni_char_gap0") == ("prod_paste_with_preservation",)

def test_browser_path_detection_uses_existing_candidate(monkeypatch):
    expected = inject_matrix._BROWSER_CANDIDATES["chrome"][1]
    monkeypatch.setattr(inject_matrix.os.path, "exists", lambda path: path == expected)
    assert inject_matrix.detect_browser_path("chrome") == expected

def test_browser_auto_detection_order(monkeypatch):
    expected = inject_matrix._BROWSER_CANDIDATES["edge"][0]
    monkeypatch.setattr(inject_matrix.os.path, "exists", lambda path: path == expected)
    assert inject_matrix.detect_browser_path("auto") == expected

def test_hold_hotkey_abort_detects_ctrl(monkeypatch):
    monkeypatch.setattr(inject_matrix.user32, "GetAsyncKeyState", lambda key: 0x8000 if key == inject_matrix.VK_CONTROL else 0)
    assert inject_matrix.hold_hotkey_down()
