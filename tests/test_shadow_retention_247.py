"""Queue 247: shadow text is opt-in and bounded by filename date."""

from datetime import datetime

from samsara.intent import shadow


class _Spawn:
    def __call__(self, *args, **kwargs):
        return type("Worker", (), {"is_alive": lambda self: True})()


class _Resolver:
    def resolve(self, text):
        return type("Resolution", (), {
            "kind": "dictation", "canonical_id": None, "confidence": 0.0,
            "tier": None, "t12_ms": 0.0, "suggestions": (), "chain": (),
            "rules_version": None, "blocked": None, "forced": False,
            "literal": False,
        })()


def _shadow(tmp_path, config, now=datetime(2026, 9, 18)):
    return shadow.IntentShadow(lambda: _Resolver(), lambda: config,
                               spawn=_Spawn(), pid_fn=lambda: None, now=lambda: now)


def test_fresh_default_is_off_and_explicit_true_survives(tmp_path):
    assert shadow.DEFAULT_ENABLED is False
    assert shadow.shadow_enabled({}) is False
    config = {"intent": {"shadow_enabled": True, "shadow_dir": str(tmp_path)}}
    observer = _shadow(tmp_path, config)
    assert observer.record("hello", "staged", datetime(2026, 9, 18), None)
    assert list(tmp_path.glob("intent-*.jsonl"))


def test_schema_declares_opt_in_bounded_retention():
    from samsara.config_schema import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["intent.shadow_enabled"]["default"] is False
    assert SETTINGS_SCHEMA["intent.shadow_retention_days"] == {
        "type": "int", "min": 1, "max": 90, "default": 14, "tab": "advanced",
    }


def test_old_shadow_files_are_deleted_younger_and_unrelated_files_kept(tmp_path):
    config = {"intent": {"shadow_enabled": True, "shadow_dir": str(tmp_path),
                         "shadow_retention_days": 14}}
    (tmp_path / "intent-2026-09-03.jsonl").write_text("old\n", encoding="utf-8")
    (tmp_path / "intent-2026-09-04.jsonl").write_text("boundary\n", encoding="utf-8")
    (tmp_path / "intent-2026-09-10.jsonl").write_text("young\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "intent-not-a-date.jsonl").write_text("keep\n", encoding="utf-8")

    observer = _shadow(tmp_path, config)
    observer.record("hello", "staged", datetime(2026, 9, 18), None)

    assert not (tmp_path / "intent-2026-09-03.jsonl").exists()
    assert (tmp_path / "intent-2026-09-04.jsonl").exists()
    assert (tmp_path / "intent-2026-09-10.jsonl").exists()
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "intent-not-a-date.jsonl").exists()


def test_retention_runs_once_per_day_per_process(tmp_path, monkeypatch):
    config = {"intent": {"shadow_enabled": True, "shadow_dir": str(tmp_path),
                         "shadow_retention_days": 14}}
    calls = []
    monkeypatch.setattr(shadow, "_prune_old_files",
                        lambda folder, today, retention_days: calls.append(
                            (folder, today, retention_days)) or 0)
    observer = _shadow(tmp_path, config)
    for index in range(3):
        observer.record(f"hello {index}", "staged", datetime(2026, 9, 18), None)
    assert len(calls) == 1
    observer._now = lambda: datetime(2026, 9, 19)
    observer.record("next day", "staged", datetime(2026, 9, 19), None)
    assert len(calls) == 2
