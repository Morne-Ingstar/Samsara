"""345a window target checks use a fake native facade, never real windows."""

from dataclasses import replace

import pytest

from samsara.window_targets import (WindowSnapshot, WindowTargetAdapter,
                                    title_fingerprint)


def window(hwnd=101, **changes):
    base = WindowSnapshot(hwnd, hwnd, 0, True, False, 0, "Downloads - Explorer",
                          "CabinetWClass", 42, r"C:\Windows\explorer.exe", 123.5, "ok")
    return replace(base, **changes)


class FakeNative:
    def __init__(self, *windows):
        self.by_hwnd = {item.hwnd: item for item in windows}
        self.point = (10, 20)
        self.hit = windows[0].hwnd if windows else 0
        self.roots = {}
        self.calls = []
        self.fail = None

    def sample_pointer(self):
        self.calls.append("sample_pointer")
        if self.fail == "pointer":
            raise OSError("pointer unavailable")
        return *self.point, self.roots.get(self.hit, self.hit)

    def window(self, hwnd):
        self.calls.append(("window", hwnd))
        if self.fail == "window":
            raise OSError("window API unavailable")
        return self.by_hwnd.get(hwnd)

    def windows(self):
        self.calls.append("windows")
        if self.fail == "enumerate":
            raise OSError("enumeration unavailable")
        return tuple(self.by_hwnd)


def adapter(*windows, own_pid=999, aliases=None):
    native = FakeNative(*windows)
    return WindowTargetAdapter(native, own_pid=own_pid, aliases=aliases or {}), native


def test_two_explorer_windows_remain_numberable_candidates():
    targets, _ = adapter(window(101), window(102, title="Pictures - Explorer"))
    result = targets.enumerate_named("explorer")
    assert result.reason is None
    assert [item.identity.hwnd for item in result.candidates] == [101, 102]
    assert result.candidates[0].identity.pid == result.candidates[1].identity.pid


def test_incomplete_enumeration_cannot_silently_bind_first_match():
    targets, _ = adapter(window(101), window(102, process_name=""))
    result = targets.enumerate_named("explorer")
    assert result.candidates == ()
    assert result.reason == "enumeration_incomplete"


def test_name_matching_uses_alias_without_choosing_ranked_first():
    targets, _ = adapter(window(101, title="Inbox", process_name=r"C:\Apps\msedge.exe"),
                         window(102, title="News", process_name=r"C:\Apps\msedge.exe"),
                         aliases={"edge": "msedge.exe"})
    assert [c.identity.hwnd for c in targets.enumerate_named("edge").candidates] == [101, 102]


def test_child_pointer_resolves_ga_root_not_child():
    targets, native = adapter(window(101))
    native.hit = 201
    native.roots[201] = 101
    result = targets.capture_pointer()
    assert result.ok and result.identity.hwnd == 101
    assert result.point == (10, 20)
    assert ("window", 201) not in native.calls


def test_owned_dialog_is_rejected_instead_of_rebinding_to_owner():
    targets, native = adapter(window(201, owner=101, window_class="#32770"), window(101))
    native.hit = 201
    result = targets.capture_pointer()
    assert result.identity is None and result.reason == "owned_window"
    assert ("window", 101) not in native.calls


@pytest.mark.parametrize("window_class", ["Progman", "WorkerW", "Shell_TrayWnd",
                                         "Shell_SecondaryTrayWnd", "#32770"])
def test_desktop_shell_and_dialog_classes_refused(window_class):
    targets, _ = adapter(window(window_class=window_class))
    assert targets.capture_pointer().reason == "ineligible_window"


def test_own_pid_is_refused():
    targets, _ = adapter(window(pid=8764), own_pid=8764)
    assert targets.capture_pointer().reason == "own_pid"


def test_published_overlay_blocks_click_through_window_beneath_it():
    targets, native = adapter(window())
    targets.publish_overlay_rects(((0, 0, 30, 40),))
    result = targets.capture_pointer()
    assert result.reason == "own_overlay" and result.point == (10, 20)
    assert ("window", 101) not in native.calls
    targets.publish_overlay_rects(())
    assert targets.capture_pointer().ok


def test_overlay_rectangles_are_validated_before_publication():
    targets, _ = adapter(window())
    with pytest.raises(ValueError):
        targets.publish_overlay_rects(((2, 2, 1, 1),))
    assert targets.capture_pointer().ok


@pytest.mark.parametrize("change, reason", [
    ({"pid": 43}, "pid_changed"),
    ({"process_name": r"C:\Other\explorer.exe"}, "process_changed"),
    ({"process_start_time": 124.5}, "process_changed"),
    ({"window_class": "OtherClass"}, "window_class_changed"),
])
def test_exact_hwnd_probe_rejects_identity_changes(change, reason):
    targets, native = adapter(window())
    bound = targets.capture_pointer().identity
    native.by_hwnd[101] = window(**change)
    assert targets.validate(bound).reason == reason
    assert not targets.probe_same_hwnd(bound)


def test_dead_hwnd_fails_without_same_app_fallback():
    targets, native = adapter(window(101), window(102))
    bound = targets.capture_pointer().identity
    del native.by_hwnd[101]
    assert targets.validate(bound).reason == "dead_hwnd"
    assert not targets.probe_same_hwnd(bound)
    assert ("window", 102) not in native.calls


def test_invalid_reference_fails_closed():
    targets, _ = adapter(window())
    assert targets.validate(None).reason == "invalid_identity"


def test_title_change_tolerated_except_when_close_question_binds_title():
    targets, native = adapter(window())
    bound = targets.capture_pointer().identity
    old_hash = title_fingerprint("Downloads - Explorer")
    native.by_hwnd[101] = window(title="Unsaved document - Explorer")
    assert targets.validate(bound).ok
    assert targets.validate(bound, title_hash=old_hash).reason == "title_changed"


@pytest.mark.parametrize("integrity, reason", [("unknown", "integrity_unknown"),
                                                  ("higher", "integrity_higher")])
def test_unknown_or_higher_integrity_fails_closed(integrity, reason):
    targets, _ = adapter(window(integrity=integrity))
    assert targets.capture_pointer().reason == reason


@pytest.mark.parametrize("change", [
    {"pid": 0}, {"process_name": ""}, {"process_start_time": 0.0},
    {"window_class": ""},
])
def test_missing_process_or_class_identity_never_creates_a_target(change):
    targets, _ = adapter(window(**change))
    assert not targets.capture_pointer().ok


@pytest.mark.parametrize("failure", ["pointer", "window", "enumerate"])
def test_unavailable_native_api_returns_explicit_failure(failure):
    targets, native = adapter(window())
    native.fail = failure
    result = targets.enumerate_named("explorer") if failure == "enumerate" else targets.capture_pointer()
    assert result.reason == "api_unavailable"


def test_malformed_native_snapshot_fails_closed():
    targets, native = adapter(window())
    native.by_hwnd[101] = window(title=None)
    assert targets.capture_pointer().reason == "api_unavailable"


@pytest.mark.parametrize("change", [
    {"visible": False}, {"cloaked": True}, {"exstyle": 0x80}, {"title": ""},
])
def test_ineligible_window_cannot_be_bound(change):
    targets, _ = adapter(window(**change))
    assert targets.capture_pointer().reason == "ineligible_window"


def test_validation_never_samples_pointer_or_enumerates_names():
    targets, native = adapter(window())
    identity = targets.capture_pointer().identity
    native.point = (500, 500)
    native.calls.clear()
    assert targets.validate(identity).ok
    assert native.calls == [("window", 101)]
