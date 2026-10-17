"""Size policy and focused regression checks, including folder exports."""

from pathlib import Path

import pytest

from tools import size_ratchet as ratchet


def test_source_size_ratchet():
    root = Path(__file__).resolve().parents[1]
    errors = ratchet.violations(root, ratchet.read_baseline(root))
    assert not errors, "\n" + "\n".join(errors)


def make_source(root, name, count):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# line\n" * count)
    return path


@pytest.mark.parametrize("data, expected", [
    (b"", 0), (b"one", 1), (b"one\n", 1),
    (b"one\r\ntwo\r\n", 2), (b"one\ntwo", 2),
    (b"one\x0btwo\n", 1),
])
def test_physical_lines(tmp_path, data, expected):
    path = tmp_path / "sample.py"
    path.write_bytes(data)
    assert ratchet.line_count(path) == expected


@pytest.mark.parametrize("count, fragment", [
    (1601, "1601 lines, ceiling 1600"),
    (1600, None), (1575, None),
    (1574, "This file shrank: lower its ceiling"),
])
def test_ceiling_and_slack_boundaries(tmp_path, count, fragment):
    make_source(tmp_path, "samsara/big.py", count)
    baseline = {"threshold": 1500, "slack": 25, "ceilings": {"samsara/big.py": 1600}}
    errors = ratchet.violations(tmp_path, baseline)
    if fragment:
        assert len(errors) == 1 and fragment in errors[0]
    else:
        assert errors == []


def test_scan_covers_source_groups_and_ignores_tests(tmp_path):
    names = ["root.py"] + [f"{folder}/nested/new.py" for folder in ratchet.SOURCE_FOLDERS]
    for name in names:
        make_source(tmp_path, name, 1501)
    make_source(tmp_path, "tests/large.py", 2000)
    make_source(tmp_path, "samsara/exact_threshold.py", 1500)
    errors = ratchet.violations(tmp_path, {"threshold": 1500, "slack": 25, "ceilings": {}})
    assert len(errors) == len(names)
    assert all(any(name in error for error in errors) for name in names)


def test_missing_file_is_reported_and_lower_does_not_write(tmp_path, capsys):
    baseline = {"threshold": 1500, "slack": 25, "ceilings": {"gone.py": 1600}}
    ratchet.write_baseline(tmp_path, baseline)
    before = (tmp_path / ratchet.FIXTURE).read_bytes()
    assert "gone.py: listed source file is missing" in ratchet.violations(tmp_path, baseline)[0]
    assert ratchet.main(["--lower", "--root", str(tmp_path)]) == 2
    assert "gone.py" in capsys.readouterr().out
    assert (tmp_path / ratchet.FIXTURE).read_bytes() == before


def test_cli_in_plain_folder_and_lower_never_raises(tmp_path, capsys):
    make_source(tmp_path, "samsara/growing.py", 1600)
    make_source(tmp_path, "plugins/shrinking.py", 1600)
    make_source(tmp_path, "tools/dropping.py", 1501)
    assert not (tmp_path / ".git").exists()
    assert ratchet.main(["--init", "--root", str(tmp_path)]) == 0
    assert ratchet.main(["--check", "--root", str(tmp_path)]) == 0
    make_source(tmp_path, "samsara/growing.py", 1601)
    make_source(tmp_path, "plugins/shrinking.py", 1550)
    make_source(tmp_path, "tools/dropping.py", 1500)
    make_source(tmp_path, "new.py", 1501)
    before = (tmp_path / ratchet.FIXTURE).read_bytes()
    assert ratchet.main(["--check", "--root", str(tmp_path)]) == 1
    assert (tmp_path / ratchet.FIXTURE).read_bytes() == before
    assert ratchet.main(["--lower", "--root", str(tmp_path)]) == 0
    assert ratchet.read_baseline(tmp_path)["ceilings"] == {
        "samsara/growing.py": 1600, "plugins/shrinking.py": 1550,
    }
    assert ratchet.main(["--check", "--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert ratchet.GROWTH_GUIDANCE in output
    assert "new.py" in output


def test_generated_model_module_round_trips_snapshot(tmp_path):
    import runpy

    from tools.gen_ai_models import render_module, build_snapshot

    catalogue = {"data": [
        {"id": "openai/gpt-6-luna", "name": "GPT Luna", "created": 10,
         "pricing": {"prompt": "0.1", "completion": "0.2"},
         "supported_parameters": ["tools"], "context_length": 8192,
         "architecture": {"output_modalities": ["text"]}},
        {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek Flash", "created": 10,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools"], "context_length": 8192,
         "architecture": {"output_modalities": ["text"]}},
        {"id": "anthropic/claude-haiku-5-5", "name": "Claude Haiku", "created": 10,
         "pricing": {"prompt": "0.1", "completion": "0.2"},
         "supported_parameters": ["tools"], "context_length": 8192,
         "architecture": {"output_modalities": ["text"]}},
        {"id": "openrouter/openai/gpt-6-luna", "name": "GPT Luna", "created": 10,
         "pricing": {"prompt": "0.1", "completion": "0.2"},
         "supported_parameters": ["tools"], "context_length": 8192,
         "architecture": {"output_modalities": ["text"]}},
    ]}
    target = tmp_path / "generated.py"
    target.write_text(render_module(catalogue, "fixture.json", "fixture-hash"), encoding="utf-8")
    namespace = runpy.run_path(str(target))
    assert namespace["SNAPSHOT"] == build_snapshot(catalogue)
    generated = target.read_text(encoding="utf-8").splitlines()
    model_lines = [line for line in generated if line.startswith("            {")]
    assert model_lines and all(line.rstrip().endswith(",") for line in model_lines)
