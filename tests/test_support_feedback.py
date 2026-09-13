import json
from pathlib import Path
from urllib.parse import unquote

from samsara.commands import CommandExecutor
from samsara.support_feedback import (
    BETA_FEEDBACK_URL,
    BETA_SUPPORT_EMAIL,
    BETA_SUPPORT_SUBJECT,
    BUG_REPORT_URL,
    SUPPORT_EMAIL_PROMPT,
    build_safe_diagnostic_summary,
    build_support_email_body,
    build_support_email_text,
    build_support_mailto,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_safe_summary_includes_useful_allowlisted_runtime_facts():
    text = build_safe_diagnostic_summary(
        {
            "model_size": "medium",
            "language": "en",
            "device": "cuda",
            "compute_type": "float16",
            "performance_mode": "fast",
            "mode": "hold",
            "command_mode": {"enabled": True},
            "wake_word_enabled": True,
            "ui_scale": 1.25,
        },
        frozen=True,
        platform_text="Windows-11-test",
        python_version="3.11.test",
    )

    assert "Execution: packaged" in text
    assert "Windows: Windows-11-test" in text
    assert "Model: medium" in text
    assert "Requested device: cuda" in text
    assert "HANDS FREE enabled: True" in text
    assert "Wake listener enabled: True" in text


def test_safe_summary_never_copies_secrets_paths_devices_or_dictation():
    forbidden = {
        "api_key": "secret-api-value",
        "supporter_key": "secret-supporter-value",
        "webhook_url": "https://secret.invalid/hook",
        "microphone_name": "Morne's private microphone",
        "config_path": r"C:\Users\Private Person\.samsara\config.json",
        "last_transcription": "private dictated sentence",
        "wake_profiles": [{"target_process": "private-client.exe"}],
    }

    text = build_safe_diagnostic_summary(
        forbidden,
        frozen=False,
        platform_text="Windows-test",
        python_version="3.11.test",
    )

    for secret in forbidden.values():
        if isinstance(secret, str):
            assert secret not in text
    assert "private-client.exe" not in text
    assert "not copied automatically" in text


def test_support_email_contains_prompt_and_diagnostics_but_never_private_values():
    forbidden = {
        "model_size": "medium",
        "api_key": "secret-api-value",
        "supporter_key": "secret-supporter-value",
        "webhook_url": "https://secret.invalid/hook",
        "microphone_name": "Morne's private microphone",
        "config_path": r"C:\Users\Private Person\.samsara\config.json",
        "last_transcription": "private dictated sentence",
        "wake_profiles": [{"target_process": "private-client.exe"}],
    }
    kwargs = dict(frozen=True, platform_text="Windows-test", python_version="3.11.test")

    body = build_support_email_body(forbidden, **kwargs)
    text = build_support_email_text(forbidden, **kwargs)
    mailto = build_support_mailto(forbidden, **kwargs)
    decoded = unquote(mailto)

    assert body == SUPPORT_EMAIL_PROMPT + build_safe_diagnostic_summary(forbidden, **kwargs)
    assert body.startswith("What I said / What it did / What I expected:\n\n\n---\n")
    assert text.startswith(f"To: {BETA_SUPPORT_EMAIL}\nSubject: {BETA_SUPPORT_SUBJECT}\n\n")
    assert text.endswith(body)
    assert "Model: medium" in text
    assert "Execution: packaged" in text

    assert mailto.startswith(f"mailto:{BETA_SUPPORT_EMAIL}?subject=Samsara%20beta%20support&body=")
    assert "\n" not in mailto and " " not in mailto
    assert decoded.endswith(body)

    for rendered in (body, text, decoded):
        for secret in forbidden.values():
            if isinstance(secret, str) and secret != "medium":
                assert secret not in rendered
        assert "private-client.exe" not in rendered
        assert ".samsara" not in rendered


def test_malformed_nested_command_mode_is_safe():
    text = build_safe_diagnostic_summary(
        {"command_mode": "not-a-dict"},
        frozen=False,
        platform_text="Windows-test",
        python_version="3.11.test",
    )

    assert "HANDS FREE enabled: False" in text


def test_feedback_voice_commands_are_always_on_and_open_exact_forms():
    commands = json.loads(
        (PROJECT_ROOT / "commands.json").read_text(encoding="utf-8")
    )["commands"]

    assert commands["report a bug"] == {
        "type": "launch",
        "target": BUG_REPORT_URL,
        "description": "Open the structured Samsara problem report form",
        "pack": "core",
    }
    assert commands["send feedback"] == {
        "type": "launch",
        "target": BETA_FEEDBACK_URL,
        "description": "Open the Samsara beta feedback form",
        "pack": "core",
    }

    executor = CommandExecutor(PROJECT_ROOT / "commands.json")
    assert executor.find_exact_command("report a bug") == "report a bug"
    assert executor.find_exact_command("send feedback") == "send feedback"
