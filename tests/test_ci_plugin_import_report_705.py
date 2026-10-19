"""705: the CI diagnostic continues after a real module import failure."""
import sys

from tools import ci_plugin_import_report as report


def test_failed_import_prints_full_traceback_and_exits_zero(tmp_path, capsys, monkeypatch):
    plugins = tmp_path / 'plugins'
    commands = plugins / 'commands'
    commands.mkdir(parents=True)
    (plugins / '__init__.py').write_text('', encoding='utf-8')
    (commands / '__init__.py').write_text('', encoding='utf-8')
    (commands / 'a_failure.py').write_text(
        "raise RuntimeError('synthetic import failure')\n", encoding='utf-8')
    (commands / 'z_success.py').write_text('VALUE = 1\n', encoding='utf-8')
    (plugins / 'top_level.py').write_text('VALUE = 2\n', encoding='utf-8')
    (commands / '_private.py').write_text('raise AssertionError\n', encoding='utf-8')
    # Keep the temporary package and the real plugin registry independent.
    with monkeypatch.context() as patch:
        for name in list(sys.modules):
            if name == 'plugins' or name.startswith('plugins.'):
                patch.delitem(sys.modules, name)
        try:
            assert report.main(tmp_path) == 0
        finally:
            for name in list(sys.modules):
                if name == 'plugins' or name.startswith('plugins.'):
                    del sys.modules[name]
    output = capsys.readouterr().out
    assert 'FAIL plugins.commands.a_failure' in output
    assert 'Traceback (most recent call last)' in output
    assert 'a_failure.py' in output and 'RuntimeError: synthetic import failure' in output
    assert 'OK plugins.commands.z_success' in output
    assert 'OK plugins.top_level' in output
    assert '_private' not in output
