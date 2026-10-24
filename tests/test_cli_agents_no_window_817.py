"""817: headless GPT/Claude launches retain pipes and hide Windows consoles."""
import io
from types import SimpleNamespace

import pytest

from samsara.agent_channel import cli_agents


@pytest.mark.parametrize("name", ["gpt", "claude"])
@pytest.mark.parametrize("platform,flag", [("nt", 0x08000000), ("nt", None), ("posix", 0x08000000)])
def test_cli_popen_console_flags(monkeypatch, tmp_path, name, platform, flag):
    calls = []
    process = SimpleNamespace(
        stdin=io.StringIO(), stdout=io.StringIO("reply event\n"), stderr=io.StringIO(),
        wait=lambda **kwargs: 0,
    )
    # The active-process set requires a hashable process, like a real Popen.
    class FakeProcess:
        pass

    fake = FakeProcess()
    fake.__dict__.update(vars(process))

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return fake

    subprocess_stub = SimpleNamespace(Popen=popen, PIPE=cli_agents.subprocess.PIPE)
    if flag is not None:
        subprocess_stub.CREATE_NO_WINDOW = flag
    monkeypatch.setattr(cli_agents, "subprocess", subprocess_stub)
    # Replace only this module's os reference; changing global os.name breaks pathlib.
    monkeypatch.setattr(cli_agents, "os", SimpleNamespace(name=platform, environ={}))
    # Keep the committed GPT environment helper isolated from real account files.
    monkeypatch.setattr(cli_agents, "_codex_env", lambda home: {}, raising=False)
    monkeypatch.setattr(cli_agents.thread_registry, "spawn",
                        lambda label, target, args: target(*args))
    agent = cli_agents.CliAgent(
        name, lambda exe, *args: exe + ["test-argument"],
        lambda lines: ("four" if lines == ["reply event"] else "", None),
        executable=["fake-cli"],
    )

    reply = agent.ask("synthetic arithmetic question", {"cwd": str(tmp_path)}, home=tmp_path)

    assert reply.text == "four"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["fake-cli", "test-argument"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess_stub.PIPE
    assert (kwargs["text"], kwargs["encoding"], kwargs["errors"]) == (True, "utf-8", "replace")
    if platform == "nt":
        assert kwargs["creationflags"] == (flag or 0)
    else:
        assert "creationflags" not in kwargs
