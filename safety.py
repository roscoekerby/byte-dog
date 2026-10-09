"""Verified process protection: what Windows says about a process, not its name.

A process is protected when any of these hold:
- Windows marks it critical (IsProcessCritical: killing it bugchecks the machine)
- it runs as a Protected Process / Protected Process Light (csrss, smss, Defender...)
- its image lives under %SystemRoot% (not Windows\\Temp) AND is Authenticode-signed
  by Microsoft (embedded or catalog signature)
- it is a kernel pseudo-process (System, Registry, Memory Compression, vmmem...)
- it cannot be opened at all (fail safe: unverifiable means hands off)
- it is ByteDog itself or on the user's "never touch" list

A spoofed svchost.exe in Temp is therefore not protected, and the real one is.
Verdicts are cached per (pid, create time), so each process is opened once.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable

import psutil

from signing import signature

PSEUDO_PROCESS_NAMES = frozenset({
    'system', 'registry', 'memory compression', 'memcompression', 'secure system',
    'idle', 'system idle process', 'vmmem', 'vmmemwsl',
})
KERNEL_PIDS = frozenset({0, 4})
UNTRUSTED_WINDOWS_SUBDIRS = ('temp',)
MICROSOFT_SIGNERS = frozenset({'microsoft corporation', 'microsoft windows'})


@dataclass(frozen=True)
class Facts:
    pid: int
    name: str
    path: str
    critical: bool | None
    ppl: int            # 0 none, 1 protected light, 2 protected
    signer: str         # organisation of the leaf signing cert, '' if unsigned
    opened: bool        # could ByteDog open the process at all
    signer_known: bool = True   # False: verification failed to complete (not cached)


@dataclass(frozen=True)
class Verdict:
    protected: bool
    reason: str = ''
    path: str = ''
    signer: str = ''


def _in_windows_dir(path: str, windows_dir: str) -> bool:
    try:
        rel = os.path.relpath(os.path.normcase(os.path.realpath(path)),
                              os.path.normcase(windows_dir))
    except ValueError:      # different drive
        return False
    if rel.startswith('..') or os.path.isabs(rel):
        return False
    return rel.split(os.sep)[0] not in UNTRUSTED_WINDOWS_SUBDIRS


def is_microsoft(signer: str) -> bool:
    return (signer or '').lower() in MICROSOFT_SIGNERS


def decide(f: Facts, windows_dir: str, user_names=frozenset(), self_pid=None,
           self_exe: str | None = None) -> Verdict:
    """Pure protection decision from gathered facts."""
    name = (f.name or '').lower()
    v = lambda reason: Verdict(True, reason, f.path, f.signer)  # noqa: E731
    if f.pid == self_pid or (self_exe and f.path and
                             os.path.normcase(f.path) == os.path.normcase(self_exe)):
        return v("ByteDog itself")
    if name in {n.lower() for n in user_names}:
        return v("On your never-touch list")
    if f.pid in KERNEL_PIDS or name in PSEUDO_PROCESS_NAMES:
        return v("Windows kernel / virtual machine memory process")
    if f.critical:
        return v("Windows marks this process critical: killing it crashes Windows (blue screen)")
    if f.ppl:
        return v("Windows protected process: the OS restricts touching it")
    if not f.opened or not f.path or f.critical is None:
        return v("Could not verify this process (access denied), treated as protected")
    if _in_windows_dir(f.path, windows_dir):
        if not f.signer_known:
            return v("Windows-folder program whose signature could not be checked right "
                     "now, treated as protected")
        if is_microsoft(f.signer):
            return v("Microsoft-signed Windows component")
    return Verdict(False, '', f.path, f.signer)


class ProtectionChecker:
    """Thread-safe, cached protection verdicts for snapshot procs."""

    def __init__(self, gather: Callable[[int, str], Facts] | None = None,
                 windows_dir: str | None = None,
                 user_names: Callable[[], set] = lambda: frozenset(),
                 self_pid: int | None = None, self_exe: str | None = None):
        self._gather = gather or gather_facts
        self._windows_dir = windows_dir or os.environ.get('SystemRoot', r'C:\Windows')
        self._user_names = user_names
        self._self_pid = self_pid
        self._self_exe = self_exe
        self._lock = threading.Lock()
        self._facts = {}    # pid -> (ctime, Facts)

    def facts(self, p: dict) -> Facts:
        pid, ctime = p['pid'], p.get('ctime')
        with self._lock:
            cached = self._facts.get(pid)
        # Cached per (pid, start time in ms). Without a start time (process
        # gone) nothing is cached; nor are facts whose signature check did
        # not complete, so they are retried instead of trusted.
        if cached and ctime is not None and cached[0] == ctime:
            return cached[1]
        facts = self._gather(pid, p.get('name') or '')
        if ctime is not None and facts.signer_known:
            with self._lock:
                self._facts[pid] = (ctime, facts)
        return facts

    def check(self, p: dict) -> Verdict:
        return decide(self.facts(p), self._windows_dir, user_names=self._user_names(),
                      self_pid=self._self_pid, self_exe=self._self_exe)

    def check_pid(self, pid: int, name: str) -> Verdict:
        """For callers without a snapshot: identifies the process by its live
        start time, so a cached verdict never outlives PID reuse."""
        return self.check({'pid': pid, 'name': name, 'ctime': ctime_ms(pid)})

    def prune(self, live_pids) -> None:
        live = set(live_pids)
        with self._lock:
            self._facts = {pid: v for pid, v in self._facts.items() if pid in live}


# ── Win32 fact gathering ─────────────────────────────────────────────────

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ProcessProtectionInformation = 61


def gather_facts(pid: int, name: str) -> Facts:
    if sys.platform != 'win32' or pid in KERNEL_PIDS:
        return Facts(pid, name, '', None, 0, '', False)
    k32, ntdll = _k32(), _ntdll()
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return Facts(pid, name, '', None, 0, '', False)
    try:
        path = _image_path(k32, h)
        crit = ctypes.c_int(0)
        critical = bool(crit.value) if k32.IsProcessCritical(h, ctypes.byref(crit)) else None
        prot = ctypes.c_ubyte(0)
        status = ntdll.NtQueryInformationProcess(h, _ProcessProtectionInformation,
                                                 ctypes.byref(prot), 1, None)
        ppl = (prot.value & 0x7) if status == 0 else 0
    finally:
        k32.CloseHandle(h)
    signer, known = '', True
    if path and _in_windows_dir(path, os.environ.get('SystemRoot', r'C:\Windows')):
        signer, known = signature(path)    # only Windows-folder binaries need it
    return Facts(pid, name, path, critical, ppl, signer, True, known)


def ctime_ms(pid: int) -> int | None:
    """Process start time in ms since the epoch (matches snapshot 'ctime')."""
    try:
        return round(psutil.Process(pid).create_time() * 1000)
    except psutil.Error:
        return None


def _image_path(k32, h) -> str:
    size = ctypes.c_ulong(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ''


_k32_cache = []
_ntdll_cache = []


def _k32():
    if not _k32_cache:
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR,
                                                 ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.IsProcessCritical.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_int)]
        k.IsProcessCritical.restype = w.BOOL
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        _k32_cache.append(k)
    return _k32_cache[0]


def _ntdll():
    if not _ntdll_cache:
        from ctypes import wintypes as w
        n = ctypes.WinDLL('ntdll')
        n.NtQueryInformationProcess.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                w.ULONG, ctypes.POINTER(w.ULONG)]
        n.NtQueryInformationProcess.restype = ctypes.c_long
        _ntdll_cache.append(n)
    return _ntdll_cache[0]

