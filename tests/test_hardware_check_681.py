"""681: the PC check survives display mappings without readable GPU data."""
from contextlib import contextmanager
from types import SimpleNamespace

from samsara import hardware_check as hw


PREFIX = "\\Registry\\Machine\\"


def registry_for(adapters):
    """Build a small winreg stand-in: mapping path -> data or open error."""
    entries = [(rf"\Device\Video{i}", PREFIX + path, 1)
               for i, path in enumerate(adapters)]

    @contextmanager
    def open_key(_root, path):
        if path == r"HARDWARE\DEVICEMAP\VIDEO":
            yield "mapping"
            return
        item = adapters[path]
        if isinstance(item, OSError):
            raise item
        yield path

    def enum_value(_mapping, index):
        if index >= len(entries):
            exc = OSError("end of values")
            exc.winerror = 259
            raise exc
        return entries[index]

    def query_value(path, name):
        if path == "cpu" and name == "ProcessorNameString":
            return "Test CPU", 1
        item = adapters[path]
        if isinstance(item, Exception):
            raise item
        if name == "DriverDesc":
            return item["name"], 1
        if "memory" not in item:
            raise FileNotFoundError(name)
        return item["memory"], item.get("kind", 11)

    return SimpleNamespace(HKEY_LOCAL_MACHINE=0, REG_QWORD=11, REG_BINARY=3,
                           OpenKey=open_key, EnumValue=enum_value,
                           QueryValueEx=query_value)


def test_basic_display_open_error_is_skipped():
    path = r"SYSTEM\ControlSet001\Services\BasicDisplay"
    registry = registry_for({path: OSError(161, "invalid path")})
    assert hw._registry_gpus(registry) == ()


def test_missing_memory_is_shared_memory_gpu():
    path = r"SYSTEM\Video\intel\0000"
    registry = registry_for({path: {"name": "Intel UHD Graphics"}})
    assert hw._registry_gpus(registry) == (hw.GPU("Intel UHD Graphics", 0),)


def test_nvidia_qword_keeps_full_capacity():
    path = r"SYSTEM\Video\nvidia\0000"
    registry = registry_for({path: {"name": "NVIDIA Test", "memory": 8 * hw.GIB}})
    assert hw._registry_gpus(registry) == (hw.GPU("NVIDIA Test", 8 * hw.GIB),)


def test_wrong_registry_type_or_short_binary_is_zero():
    path = r"SYSTEM\Video\intel\0000"
    for memory, kind in ((8 * hw.GIB, 3), (b"\x00" * 4, 3), (b"\x00" * 8, 4)):
        registry = registry_for({path: {"name": "Intel UHD Graphics",
                                        "memory": memory, "kind": kind}})
        assert hw._registry_gpus(registry) == (hw.GPU("Intel UHD Graphics", 0),)


def test_repeated_same_intel_adapter_is_deduplicated():
    adapters = {rf"SYSTEM\Video\intel\{i:04d}": {"name": "Intel UHD Graphics"}
                for i in range(3)}
    registry = registry_for(adapters)
    assert hw._registry_gpus(registry) == (hw.GPU("Intel UHD Graphics", 0),)


def test_unreadable_adapters_leave_cpu_memory_and_core_facts(monkeypatch):
    import ctypes
    import sys

    path = r"SYSTEM\Video\broken\0000"
    registry = registry_for({path: OSError(161, "invalid path")})
    registry.OpenKey_original = registry.OpenKey
    registry.OpenKey = lambda root, key: _open_cpu_or_registry(root, key, registry)

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (field, ctypes.c_ulonglong) for field in
            ("total", "available", "page_total", "page_available", "virtual_total",
             "virtual_available", "extended")]

    class Kernel:
        @staticmethod
        def GlobalMemoryStatusEx(pointer):
            ctypes.cast(pointer, ctypes.POINTER(Status)).contents.total = 16 * hw.GIB
            return 1

        @staticmethod
        def GetLogicalProcessorInformationEx(relation, buffer, length):
            if buffer is None:
                length._obj.value = 8
            else:
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32))[1] = 8
            return 1

        @staticmethod
        def IsProcessorFeaturePresent(_feature):
            return 1

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel(), raising=False)
    monkeypatch.setitem(sys.modules, "winreg", registry)
    result = hw._read_windows()
    assert result.gpus == ()
    assert result.ram_bytes > 0
    assert result.processor == "Test CPU"
    assert result.cores == 1
    assert hw.recommend(result).route == "cloud"


def _open_cpu_or_registry(root, key, registry):
    if key == r"HARDWARE\DESCRIPTION\System\CentralProcessor\0":
        @contextmanager
        def cpu_key():
            yield "cpu"
        return cpu_key()
    return registry.OpenKey_original(root, key)


def test_shared_memory_and_no_gpu_recommend_cloud():
    shared = hw.Hardware((hw.GPU("Intel UHD Graphics", 0),), 16 * hw.GIB,
                         "CPU", 8, True)
    absent = hw.Hardware((), 16 * hw.GIB, "CPU", 8, True)
    assert hw.recommend(shared).route == "cloud"
    assert hw.recommend(absent).route == "cloud"


def test_eight_gib_nvidia_and_sixteen_gib_ram_recommend_local():
    facts = hw.Hardware((hw.GPU("NVIDIA Test", 8 * hw.GIB),), 16 * hw.GIB,
                        "CPU", 8, True)
    verdict = hw.recommend(facts)
    assert (verdict.route, verdict.model) == ("local", "qwen2.5:7b")
