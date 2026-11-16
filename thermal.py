"""Heat sensors: CPU clock speed and per-process CPU / GPU use, read in-process
from Windows performance counters (pdh.dll, the same source Task Manager uses).
No subprocesses and no per-process handle opens, so it stays quick under
security software. Everything returns None or empty when a counter is missing.

There is deliberately no CPU temperature here: on many laptops the ACPI
thermal zone Windows exposes reads a fixed value. The clock-speed counter is
the signal that actually shows throttling.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
CPU_SPEED = r'\Processor Information(_Total)\% Processor Performance'
CPU_BUSY = r'\Processor Information(_Total)\% Processor Time'
PROCESS_CPU = r'\Process V2(*)\% Processor Time'
GPU_ENGINE = r'\GPU Engine(*)\Utilization Percentage'
_GPU_PID = re.compile(r'^pid_(\d+)_')
_PROCESS_V2 = re.compile(r'^(.*):(\d+)$')


class _Value(ctypes.Structure):
    _fields_ = [('CStatus', wintypes.DWORD), ('doubleValue', ctypes.c_double)]


class _Item(ctypes.Structure):
    _fields_ = [('szName', ctypes.c_wchar_p), ('FmtValue', _Value)]


class PdhQuery:
    """A query kept open between reads: rate counters need two collections."""

    def __init__(self, paths, pdh=None):
        self.ok = [False] * len(paths)
        self._counters = [wintypes.HANDLE() for _ in paths]
        self._query = wintypes.HANDLE()
        self._pdh = pdh
        if self._pdh is None and sys.platform == 'win32':
            try:
                self._pdh = ctypes.WinDLL('pdh')
                self._pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_uint32
                self._pdh.PdhGetFormattedCounterValue.restype = ctypes.c_uint32
            except OSError:
                self._pdh = None
        if self._pdh is None or self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)):
            self._pdh = None
            return
        for i, path in enumerate(paths):
            self.ok[i] = self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counters[i])) == 0
        self.collect()

    def collect(self) -> bool:
        return self._pdh is not None and self._pdh.PdhCollectQueryData(self._query) == 0

    def value(self, index: int) -> float | None:
        if self._pdh is None or not self.ok[index]:
            return None
        out = _Value()
        status = self._pdh.PdhGetFormattedCounterValue(
            self._counters[index], PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(out))
        return out.doubleValue if status == 0 and out.CStatus in (0, 1) else None

    def array(self, index: int) -> dict:
        """instance name -> value for a wildcard counter. Same-named instances are summed."""
        if self._pdh is None or not self.ok[index]:
            return {}
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        status = self._pdh.PdhGetFormattedCounterArrayW(
            self._counters[index], fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status != PDH_MORE_DATA or size.value == 0:
            return {}
        buffer = (ctypes.c_byte * size.value)()
        if self._pdh.PdhGetFormattedCounterArrayW(
                self._counters[index], fmt, ctypes.byref(size), ctypes.byref(count), buffer) != 0:
            return {}
        items = ctypes.cast(buffer, ctypes.POINTER(_Item))
        out: dict = {}
        for i in range(count.value):
            name = items[i].szName or ''
            out[name] = out.get(name, 0.0) + items[i].FmtValue.doubleValue
        return out

    def close(self) -> None:
        if self._pdh is not None:
            self._pdh.PdhCloseQuery(self._query)
            self._pdh = None


class CpuSpeedReader:
    """CPU clock speed as a % of normal, and how busy the CPU is."""

    def __init__(self, query: PdhQuery | None = None):
        self._query = query or PdhQuery([CPU_SPEED, CPU_BUSY])

    def read(self) -> tuple:
        """``(speed_pct, busy_pct)``; either may be None."""
        if not self._query.collect():
            return None, None
        busy = self._query.value(1)
        return self._query.value(0), None if busy is None else min(max(busy, 0.0), 100.0)

    def close(self) -> None:
        self._query.close()


def parse_process_cpu(raw: dict, logical_cpus: int) -> tuple:
    """Process V2 instances ('chrome:1234' -> % of one core) to
    ``(names, cpu)``: pid -> image name, pid -> % of the whole machine."""
    names, cpu = {}, {}
    for instance, value in raw.items():
        match = _PROCESS_V2.match(instance)
        if not match:
            continue
        pid = int(match.group(2))
        if pid == 0:
            continue
        names[pid] = match.group(1)
        cpu[pid] = max(value, 0.0) / max(logical_cpus, 1)
    return names, cpu


def parse_gpu_engines(raw: dict) -> dict:
    """GPU Engine instances ('pid_1234_luid_..._engtype_3D' -> %) to pid -> % of the GPU.
    A process's engines are summed and capped at 100."""
    gpu: dict = {}
    for instance, value in raw.items():
        match = _GPU_PID.match(instance)
        if match:
            pid = int(match.group(1))
            gpu[pid] = gpu.get(pid, 0.0) + max(value, 0.0)
    return {pid: min(value, 100.0) for pid, value in gpu.items()}


class HeatSourceReader:
    """Per-process CPU and GPU use. The first read after opening has no rates yet."""

    def __init__(self, query: PdhQuery | None = None, logical_cpus: int | None = None):
        self._query = query or PdhQuery([PROCESS_CPU, GPU_ENGINE])
        self._cpus = logical_cpus or os.cpu_count() or 1

    def read(self) -> tuple:
        """``(names, cpu, gpu)`` as taken by ``thermal_model.rank_heat``."""
        if not self._query.collect():
            return {}, {}, {}
        names, cpu = parse_process_cpu(self._query.array(0), self._cpus)
        return names, cpu, parse_gpu_engines(self._query.array(1))

    def close(self) -> None:
        self._query.close()
