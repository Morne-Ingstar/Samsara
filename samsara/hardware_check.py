"""Opt-in Windows capacity facts and a bounded, local-only model speed check.

No probe runs at import time. The recognition DLL/presence checks cannot supply
VRAM capacity; active Windows display mappings supply 64-bit registry values.
All thresholds are advisory: a real model turn is still needed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import sys

GIB = 1024 ** 3


@dataclass(frozen=True)
class GPU:
    name: str
    dedicated_bytes: int


@dataclass(frozen=True)
class Hardware:
    gpus: tuple[GPU, ...]
    ram_bytes: int
    processor: str
    cores: int
    avx2: bool | None

    @property
    def nvidia_gib(self):
        return max((gpu.dedicated_bytes / GIB for gpu in self.gpus
                    if "nvidia" in gpu.name.lower()), default=0)


@dataclass(frozen=True)
class Verdict:
    route: str
    model: str
    reason: str


# STAND-IN COPY (617). Ordered bands: one table owns thresholds and reasons.
BANDS = (
    (lambda h: h.avx2 is False or 0 < h.cores < 4,
     Verdict("cloud", "qwen2.5:3b", "Cloud recommended: the processor may struggle with a local model.")),
    (lambda h: h.ram_bytes < 12 * GIB,
     Verdict("cloud", "qwen2.5:3b", "Cloud recommended: there is limited system memory for a local model alongside Samsara.")),
    (lambda h: h.nvidia_gib >= 8 and h.ram_bytes >= 16 * GIB,
     Verdict("local", "qwen2.5:7b", "On this PC recommended: enough NVIDIA graphics memory for a larger local model. Test its speed next.")),
    (lambda h: h.nvidia_gib >= 4,
     Verdict("local", "qwen2.5:3b", "On this PC with a small model recommended: graphics memory is limited. Test its speed next.")),
    (lambda h: True,
     Verdict("cloud", "qwen2.5:3b", "Cloud recommended: no NVIDIA card with enough dedicated memory was found. A CPU reply might take roughly 10 to 30 seconds or longer; only a speed test can measure this PC.")),
)


def recommend(hardware: Hardware) -> Verdict:
    """Hardware capacity is a suggestion, never evidence of model quality."""
    return next(verdict for matches, verdict in BANDS if matches(hardware))


def check_hardware(*, consent=False, reader=None) -> Hardware:
    """Enforce explicit opt-in even for callers outside the guide."""
    if consent is not True:
        raise PermissionError("PC check requires permission")
    return (reader or _read_windows)()


def dedicated_bytes(value):
    """Never fall back to 32-bit AdapterRAM or the registry MemorySize value."""
    if isinstance(value, bytes) and len(value) == 8:
        return int.from_bytes(value, "little", signed=False)
    if type(value) is int and value >= 0:
        return value
    raise ValueError("Dedicated graphics memory unavailable")


def _registry_gpus(registry):
    """Read active display mappings, avoiding stale adapters in driver classes."""
    paths = set()
    with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\VIDEO") as mapping:
        index = 0
        while True:
            try:
                name, value, _kind = registry.EnumValue(mapping, index)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 259:
                    raise
                break
            index += 1
            prefix = "\\Registry\\Machine\\"
            if name.lower().startswith("\\device\\video") and value.lower().startswith(prefix.lower()):
                paths.add(value[len(prefix):])
    gpus = []
    for path in sorted(paths):
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, path) as adapter:
            name = registry.QueryValueEx(adapter, "DriverDesc")[0]
            memory, kind = registry.QueryValueEx(adapter, "HardwareInformation.qwMemorySize")
            if kind not in (registry.REG_QWORD, registry.REG_BINARY):
                raise ValueError("64-bit dedicated graphics memory unavailable")
            gpus.append(GPU(str(name), dedicated_bytes(memory)))
    if not gpus:
        raise ValueError("No active display adapter details")
    return tuple(gpus)


def _read_windows():
    if sys.platform != "win32":
        raise OSError("Windows PC check unavailable")
    import ctypes
    from ctypes import wintypes
    import winreg

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    query = kernel.GetLogicalProcessorInformationEx
    query.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    length = wintypes.DWORD()
    query(0, None, ctypes.byref(length))  # RelationProcessorCore, not thread count.
    if not length.value:
        raise OSError("Core count unavailable")
    buffer = ctypes.create_string_buffer(length.value)
    if not query(0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    offset = cores = 0
    while offset < length.value:
        size = wintypes.DWORD.from_buffer(buffer, offset + 4).value
        if size < 8 or offset + size > length.value:
            raise ValueError("Core information unavailable")
        cores += 1
        offset += size
    kernel.IsProcessorFeaturePresent.argtypes = [wintypes.DWORD]
    kernel.IsProcessorFeaturePresent.restype = wintypes.BOOL
    avx2 = bool(kernel.IsProcessorFeaturePresent(40))  # PF_AVX2_INSTRUCTIONS_AVAILABLE.
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as cpu:
        processor = winreg.QueryValueEx(cpu, "ProcessorNameString")[0]
    return Hardware(_registry_gpus(winreg), memory.total, str(processor).strip(), cores, avx2)


def measure_local(config, *, request=None):
    """A fixed, small completion; no history, speech or user text is sent.

    Ten-second request timeout includes cold loading. A timeout is not a speed
    measurement and the guide offers a retry instead of inventing tokens/sec.
    Shared transport pins loopback and refuses redirects and proxy inheritance.
    """
    from samsara import ai_transport
    cfg = config.get("ollama", {}) or {}
    host = cfg.get("host") or "http://localhost:11434"
    response = (request or ai_transport.post_local)(
        host.rstrip("/") + "/api/generate",
        json={"model": cfg.get("model"), "prompt": "Count from one to fifty in words.",
              "stream": False, "options": {"num_predict": 48}},
        timeout=10, ollama_host=host)
    try:
        response.raise_for_status()
        result = response.json()
        count, duration = result.get("eval_count"), result.get("eval_duration")
        if (result.get("done") is not True or type(count) is not int or count <= 0
                or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            raise ValueError("No speed measurement")
        return count * 1_000_000_000 / duration
    finally:
        response.close()


def speed_verdict(tokens_per_second):
    # STAND-IN COPY (617). Throughput describes speed, never answer quality.
    if tokens_per_second >= 10:
        return "Local speed looks comfortable for short replies. Answer quality still depends on the model."
    if tokens_per_second >= 4:
        return "Local replies may feel slow. Try short requests, or choose cloud for quicker replies."
    return "Local replies look slow on this PC. Cloud is recommended for quicker replies."
