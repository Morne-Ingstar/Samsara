"""Characterize real presentation accessors without bootstrapping dictation."""

import ast
import copy
import importlib
import itertools
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUBJECT_PATH = "samsara/presentation_state.py"
NAMES = ("_get_mode_display", "_idle_animation_enabled", "_tray_mark")


def load_subject():
    # Before: exact original methods. After: exact delegates plus real module.
    source = (ROOT / "dictation.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    namespace = {}
    if SUBJECT_PATH != "dictation.py":
        assert (ROOT / SUBJECT_PATH).is_file()
        namespace["_presentation_state"] = importlib.import_module("samsara.presentation_state")
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(ROOT / "dictation.py"), "exec"), namespace)
    return type("PresentationAccessors", (), {name: namespace[name] for name in NAMES})


@pytest.fixture
def subject():
    return load_subject()()


@pytest.mark.parametrize("flags", itertools.product((False, True), repeat=6))
@pytest.mark.parametrize("heard", [None, "heard", "armed", "", False, 0])
def test_tray_priority(subject, flags, heard):
    recording, ava, command, continuous, wake, snoozed = flags
    subject.__dict__.update(recording=recording, ava_mode_active=ava,
                            command_mode_active=command, continuous_active=continuous,
                            wake_word_active=wake, snoozed=snoozed, _tray_heard_eye=heard)
    capture = ("recording" if recording else "ava" if ava else
               "listening" if command or continuous or wake else "idle")
    eye = heard if heard is not None else "asleep" if snoozed else "armed" if wake else "off"
    before = copy.deepcopy(subject.__dict__)
    assert subject._tray_mark() == (capture, eye)
    assert subject.__dict__ == before


def test_defaults_and_plain_data(subject):
    assert subject._tray_mark() == ("idle", "off")
    subject.__dict__.update(recording="yes", wake_word_active=[1], snoozed={},
                            _tray_heard_eye="keyframe")
    assert subject._tray_mark() == ("recording", "keyframe")


@pytest.mark.parametrize("config,state,expected", [
    ({}, {}, "Hold"),
    ({"mode": "continuous"}, {}, "Continuous"),
    ({"mode": "AVA", "wake_word_enabled": True}, {}, "Ava + Wake"),
    ({"wake_word_enabled": True}, {"_wake_models_state": "ready"}, "Hold + Wake"),
    ({"wake_word_enabled": True}, {"_wake_models_state": "loading"}, "Hold + Wake (loading)"),
    ({"wake_word_enabled": True}, {"_wake_models_state": None}, "Hold + Wake (loading)"),
    ({"mode": "", "wake_word_enabled": True}, {}, " + Wake"),
    ({"wake_word_enabled": False}, {"_wake_models_state": "failed"}, "Hold"),
])
def test_mode_display(subject, config, state, expected):
    subject.config = config
    subject.__dict__.update(state)
    assert subject._get_mode_display() == expected


@pytest.mark.parametrize("config", [None, [], {"mode": None}, {"mode": 2}])
def test_malformed_mode(subject, config):
    subject.config = config
    with pytest.raises(AttributeError):
        subject._get_mode_display()


@pytest.mark.parametrize("config,expected", [
    ({}, True), ({"ui": None}, True), ({"ui": []}, True),
    ({"ui": "bad"}, True), ({"ui": 0}, True), ({"ui": {}}, True),
    ({"ui": {"idle_animation": True}}, True),
    ({"ui": {"idle_animation": False}}, False),
    ({"ui": {"idle_animation": None}}, False),
    ({"ui": {"idle_animation": ""}}, False),
    ({"ui": {"idle_animation": "yes"}}, True),
])
def test_idle_animation(subject, config, expected):
    subject.config = config
    assert subject._idle_animation_enabled() is expected


def test_no_logs_or_mutation(subject, caplog):
    subject.config = {"mode": "hold", "wake_word_enabled": True, "ui": {}}
    before = copy.deepcopy(subject.__dict__)
    with caplog.at_level("DEBUG"):
        assert subject._get_mode_display() == "Hold + Wake"
        assert subject._idle_animation_enabled() is True
        assert subject._tray_mark() == ("idle", "off")
    assert not caplog.records
    assert subject.__dict__ == before


def test_class_and_descriptor_access(subject):
    cls = type(subject)
    subject.config = {}
    for name, expected in zip(NAMES, ("Hold", True, ("idle", "off"))):
        method = getattr(cls, name)
        assert method(subject) == expected
        assert method.__get__(subject)() == expected
        assert len(__import__("inspect").signature(method).parameters) == 1
