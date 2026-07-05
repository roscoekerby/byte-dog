"""GPU monitoring backend.

Aggregate metrics come from NVML via nvidia-ml-py (in-process, no subprocess,
so no console flashes under pythonw and no Norton-triggering process spawns).

Per-process VRAM: NVML reports usedGpuMemory=None for every process under
Windows WDDM driver mode, so on Windows we read the PDH performance counter
'\\GPU Process Memory(*)\\Dedicated Usage' instead (Task Manager's own data
source, also in-process via pdh.dll). NVML process lists remain the fallback
for TCC mode and non-Windows platforms.
"""
from __future__ import annotations

import platform
import re

try:
    import pynvml
except ImportError:
    pynvml = None

_MB = 1024 * 1024
_initialized = False
_init_failed = False

_PDH_FMT_LARGE = 0x00000400
_PDH_MORE_DATA = 0x800007D2
_PID_RE = re.compile(r'^pid_(\d+)_')


def _nvml_handle():
    """Handle for GPU 0, initializing NVML on first use. None if unavailable."""
    global _initialized, _init_failed
    if pynvml is None or _init_failed:
        return None
    if not _initialized:
        try:
            pynvml.nvmlInit()
            _initialized = True
        except Exception:
            _init_failed = True
            return None
    try:
        return pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:
        return None


def gpu_available() -> bool:
    return _nvml_handle() is not None


def get_gpu_info() -> dict | None:
    """Aggregate GPU metrics; same dict shape the GPUtil backend returned."""
    handle = _nvml_handle()
    if handle is None:
        return None
    try:
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        try:
            temp = pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU)
        except Exception:
            temp = 0
        used_mb = mem.used / _MB
        total_mb = mem.total / _MB
        return {
            'name': name,
            'load': float(util.gpu),
            'memory_used': used_mb,
            'memory_total': total_mb,
            'memory_percent': (used_mb / total_mb) * 100 if total_mb > 0 else 0,
            'temperature': temp,
        }
    except Exception:
        return None


def get_process_vram() -> dict[int, float]:
    """Dedicated VRAM per pid, in MB."""
    if platform.system() == 'Windows':
        vram = _pdh_process_vram()
        if vram:
            return vram
    return _nvml_process_vram()


def _nvml_process_vram() -> dict[int, float]:
    handle = _nvml_handle()
    if handle is None:
        return {}
    vram: dict[int, float] = {}
    for getter in ('nvmlDeviceGetGraphicsRunningProcesses',
                   'nvmlDeviceGetComputeRunningProcesses'):
        try:
            procs = getattr(pynvml, getter)(handle)
        except Exception:
            continue
        for p in procs:
            if p.usedGpuMemory is None:  # WDDM hides per-process memory
                continue
            vram[p.pid] = max(vram.get(p.pid, 0.0), p.usedGpuMemory / _MB)
    return vram


def _parse_pid(instance: str) -> int | None:
    m = _PID_RE.match(instance or '')
    return int(m.group(1)) if m else None


def _pdh_process_vram() -> dict[int, float]:
    """Sum '\\GPU Process Memory(*)\\Dedicated Usage' per pid via pdh.dll."""
    import ctypes
    from ctypes import wintypes

    class Value(ctypes.Structure):
        _fields_ = [('CStatus', wintypes.DWORD),
                    ('largeValue', ctypes.c_longlong)]

    class Item(ctypes.Structure):
        _fields_ = [('szName', ctypes.c_wchar_p), ('FmtValue', Value)]

    try:
        pdh = ctypes.WinDLL('pdh')
    except OSError:
        return {}
    pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_uint32

    query = wintypes.HANDLE()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)):
        return {}
    try:
        counter = wintypes.HANDLE()
        path = r'\GPU Process Memory(*)\Dedicated Usage'
        if pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter)):
            return {}
        if pdh.PdhCollectQueryData(query):
            return {}

        buf_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_LARGE,
            ctypes.byref(buf_size), ctypes.byref(item_count), None)
        if status != _PDH_MORE_DATA or buf_size.value == 0:
            return {}
        buffer = (ctypes.c_byte * buf_size.value)()
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_LARGE,
            ctypes.byref(buf_size), ctypes.byref(item_count), buffer)
        if status != 0:
            return {}

        items = ctypes.cast(buffer, ctypes.POINTER(Item))
        vram: dict[int, float] = {}
        for i in range(item_count.value):
            pid = _parse_pid(items[i].szName)
            if pid is None:
                continue
            vram[pid] = vram.get(pid, 0.0) + items[i].FmtValue.largeValue / _MB
        return vram
    except Exception:
        return {}
    finally:
        pdh.PdhCloseQuery(query)
