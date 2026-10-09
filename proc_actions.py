"""Process Explorer-style operations: kill a whole tree, restart, priority,
CPU affinity, details. Callers apply ByteDog's safeguards (safety.py) before
calling; kill_tree re-checks every descendant itself.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import psutil

PRIORITIES = {
    'Realtime': getattr(psutil, 'REALTIME_PRIORITY_CLASS', -20),
    'High': getattr(psutil, 'HIGH_PRIORITY_CLASS', -10),
    'Above normal': getattr(psutil, 'ABOVE_NORMAL_PRIORITY_CLASS', -5),
    'Normal': getattr(psutil, 'NORMAL_PRIORITY_CLASS', 0),
    'Below normal': getattr(psutil, 'BELOW_NORMAL_PRIORITY_CLASS', 5),
    'Idle': getattr(psutil, 'IDLE_PRIORITY_CLASS', 19),
}
KILL_WAIT_S = 3.0


@dataclass
class TreeResult:
    killed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)    # protected descendants left alone
    failed: list = field(default_factory=list)


def tree_pids(pid: int) -> list:
    """pid and all descendants, deepest first (children die before parents,
    so nothing gets re-parented mid-kill)."""
    try:
        root = psutil.Process(pid)
        kids = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    depth = {}
    for k in kids:
        d, cur = 0, k
        try:
            while cur.pid != pid and d < 64:
                cur = psutil.Process(cur.ppid())
                d += 1
        except psutil.Error:
            pass
        depth[k.pid] = d
    return sorted(depth, key=lambda p: -depth[p]) + [pid]


def kill_tree(pid: int, is_protected: Callable[[int], bool]) -> TreeResult:
    result = TreeResult()
    for p in tree_pids(pid):
        if p != pid and is_protected(p):
            result.skipped.append(p)
            continue
        try:
            psutil.Process(p).kill()
            result.killed.append(p)
        except psutil.NoSuchProcess:
            result.killed.append(p)
        except psutil.Error:
            result.failed.append(p)
    return result


def set_priority(pid: int, label: str) -> None:
    if label not in PRIORITIES:
        raise ValueError(f"unknown priority {label!r}; choose one of {list(PRIORITIES)}")
    psutil.Process(pid).nice(PRIORITIES[label])


def set_affinity(pid: int, cpus: list) -> None:
    if not cpus:
        raise ValueError("pick at least one CPU")
    psutil.Process(pid).cpu_affinity(sorted(set(cpus)))


def priority_label(nice) -> str:
    return next((k for k, v in PRIORITIES.items() if v == nice), str(nice))


def details(pid: int) -> dict | None:
    """Everything the details pane shows; fields the OS refuses are ''."""
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            def get(fn, default=''):
                try:
                    return fn()
                except (psutil.Error, OSError):    # incl. a parent that has exited
                    return default
            ppid = get(p.ppid, None)
            parent = get(lambda: psutil.Process(ppid).name()) if ppid else ''
            return {
                'pid': pid, 'name': get(p.name), 'path': get(p.exe),
                'cmdline': subprocess.list2cmdline(get(p.cmdline, [])),
                'cwd': get(p.cwd), 'parent_pid': ppid, 'parent_name': parent,
                'user': get(p.username),
                'started': datetime.fromtimestamp(get(p.create_time, 0)).strftime(
                    '%Y-%m-%d %H:%M:%S'),
                'priority': priority_label(get(p.nice, None)),
                'threads': get(p.num_threads, 0), 'handles': get(p.num_handles, 0),
                'elevated': is_elevated(pid),
            }
    except psutil.NoSuchProcess:
        return None


# ── restart ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RestartPlan:
    exe: str
    cmd: str
    cwd: str
    shell_pid: int | None    # launch under this explorer.exe (de-elevated) when set


def plan_restart(pid: int) -> RestartPlan:
    """Work out exactly how to relaunch pid BEFORE anything is killed.
    Raises psutil.Error / OSError if the relaunch could not be done safely.
    When ByteDog is elevated and the target is not known to be elevated, the
    relaunch must go under the user's own Explorer (non-admin token); if that
    is impossible the restart is refused rather than silently run as admin."""
    proc = psutil.Process(pid)
    exe, cmdline, cwd = proc.exe(), proc.cmdline(), proc.cwd()
    if not exe or not cmdline:
        raise OSError("image path or command line unavailable (access denied)")
    shell = None
    if sys.platform == 'win32' and is_elevated(_self_pid()) and not is_elevated(pid):
        shell = _shell_pid()
        if not shell:
            raise OSError("no desktop shell for this user to relaunch it without admin "
                          "rights; restart it yourself")
    return RestartPlan(exe, subprocess.list2cmdline(cmdline), cwd, shell)


def restart(pid: int, console: bool = True) -> int:
    """Kill pid and relaunch the same image + command line in the same folder
    (see plan_restart). Returns the new pid; raises psutil.Error / OSError."""
    plan = plan_restart(pid)
    proc = psutil.Process(pid)
    proc.kill()
    try:
        proc.wait(KILL_WAIT_S)
    except psutil.TimeoutExpired:
        raise OSError(f"PID {pid} did not exit within {KILL_WAIT_S}s; not relaunched")
    flags = 0x10 if console else 0x08000000     # CREATE_NEW_CONSOLE / CREATE_NO_WINDOW
    try:
        if plan.shell_pid:
            return _spawn_under(plan.shell_pid, plan.exe, plan.cmd, plan.cwd, flags)
        return subprocess.Popen(plan.cmd, executable=plan.exe, cwd=plan.cwd,
                                creationflags=flags, close_fds=True).pid
    except OSError as e:
        raise OSError(f"the old process was closed but relaunching failed: {e}. "
                      f"Command was: {plan.cmd}") from e


def _self_pid() -> int:
    import os
    return os.getpid()


def _shell_pid() -> int | None:
    """Explorer in the current session (the desktop shell's token)."""
    import os
    try:
        session = psutil.Process(os.getpid()).as_dict(['username'])['username']
    except psutil.Error:
        session = None
    for p in psutil.process_iter(['name', 'username']):
        if (p.info['name'] or '').lower() == 'explorer.exe' and p.info['username'] == session:
            return p.pid
    return None


def is_elevated(pid: int) -> bool | None:
    """Token elevation of a process; None when it cannot be read."""
    if sys.platform != 'win32':
        return None
    from ctypes import wintypes as w
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.OpenProcess.restype = w.HANDLE
    k32.CloseHandle.argtypes = [w.HANDLE]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.OpenProcessToken.restype = w.BOOL
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.GetTokenInformation.restype = w.BOOL
    h = k32.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        tok = w.HANDLE()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return None
        try:
            elev, size = w.DWORD(0), w.DWORD(0)
            ok = adv.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(size))
            return bool(elev.value) if ok else None
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def _spawn_under(parent_pid: int, exe: str, cmd: str, cwd: str, flags: int) -> int:
    """CreateProcess with PROC_THREAD_ATTRIBUTE_PARENT_PROCESS: the child
    inherits the parent's token (de-elevation from an admin ByteDog)."""
    from ctypes import wintypes as w

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                    ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                    ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                    ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
                    ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                    ('lpReserved2', ctypes.c_void_p), ('hStdInput', w.HANDLE),
                    ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE),
                    ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.OpenProcess.restype = w.HANDLE
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.InitializeProcThreadAttributeList.restype = w.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t,
                                              ctypes.c_void_p, ctypes.c_size_t,
                                              ctypes.c_void_p, ctypes.c_void_p]
    k32.UpdateProcThreadAttribute.restype = w.BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                   w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                                   ctypes.POINTER(STARTUPINFOEXW),
                                   ctypes.POINTER(PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = w.BOOL

    hparent = k32.OpenProcess(0x0080, False, parent_pid)     # PROCESS_CREATE_PROCESS
    if not hparent:
        raise OSError(ctypes.get_last_error(), "cannot open the desktop shell process")
    size = ctypes.c_size_t(0)
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
        k32.CloseHandle(hparent)
        raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList")
    try:
        hp = w.HANDLE(hparent)
        if not k32.UpdateProcThreadAttribute(attrs, 0, 0x00020000,   # PARENT_PROCESS
                                             ctypes.byref(hp), ctypes.sizeof(hp), None, None):
            raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute")
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        pi = PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmd)
        extended = 0x00080000                                         # EXTENDED_STARTUPINFO_PRESENT
        if not k32.CreateProcessW(exe, buf, None, None, False, flags | extended, None,
                                  cwd or None, ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(ctypes.get_last_error(), "CreateProcessW")
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return pi.dwProcessId
    finally:
        k32.DeleteProcThreadAttributeList(attrs)
        k32.CloseHandle(hparent)
