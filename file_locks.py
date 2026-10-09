"""Which process holds a file open (Windows Restart Manager, the API behind
"file in use" prompts; works without admin), and one process's open files
and loaded DLLs (psutil). Replaces Sysinternals handle.exe, which only
enumerates other processes' handles when run as administrator.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass

import psutil

ERROR_MORE_DATA = 234
CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63


@dataclass(frozen=True)
class Holder:
    pid: int
    name: str
    app: str        # Restart Manager's friendly app/service name


def _rm():
    from ctypes import wintypes as w

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [('dwProcessId', w.DWORD), ('ProcessStartTime', w.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [('Process', RM_UNIQUE_PROCESS),
                    ('strAppName', w.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
                    ('strServiceShortName', w.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
                    ('ApplicationType', ctypes.c_int), ('AppStatus', w.ULONG),
                    ('TSSessionId', w.DWORD), ('bRestartable', w.BOOL)]

    rm = ctypes.WinDLL('rstrtmgr')
    rm.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    rm.RmStartSession.restype = w.DWORD
    rm.RmRegisterResources.argtypes = [w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR), w.UINT,
                                       ctypes.c_void_p, w.UINT, ctypes.c_void_p]
    rm.RmRegisterResources.restype = w.DWORD
    rm.RmGetList.argtypes = [w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT),
                             ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(w.DWORD)]
    rm.RmGetList.restype = w.DWORD
    rm.RmEndSession.argtypes = [w.DWORD]
    rm.RmEndSession.restype = w.DWORD
    return rm, RM_PROCESS_INFO


def who_locks(paths: list) -> list:
    """Processes holding any of `paths` (files, not folders) open."""
    if not paths:
        raise ValueError("give at least one file path")
    from ctypes import wintypes as w
    rm, RM_PROCESS_INFO = _rm()
    session = w.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    err = rm.RmStartSession(ctypes.byref(session), 0, key)
    if err:
        raise OSError(err, "RmStartSession failed")
    try:
        arr = (w.LPCWSTR * len(paths))(*[str(p) for p in paths])
        err = rm.RmRegisterResources(session, len(paths), arr, 0, None, 0, None)
        if err:
            raise OSError(err, "RmRegisterResources failed")
        needed, count, reasons = w.UINT(0), w.UINT(0), w.DWORD(0)
        infos = (RM_PROCESS_INFO * 1)()
        for _ in range(3):
            err = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                               infos, ctypes.byref(reasons))
            if err != ERROR_MORE_DATA:
                break
            infos = (RM_PROCESS_INFO * (needed.value + 4))()
            count = w.UINT(len(infos))
        if err:
            raise OSError(err, "RmGetList failed")
        return [_holder(infos[i]) for i in range(count.value)]
    finally:
        rm.RmEndSession(session)


def _holder(info) -> Holder:
    pid = info.Process.dwProcessId
    try:
        name = psutil.Process(pid).name()
    except psutil.Error:
        name = '?'
    return Holder(pid, name, info.strAppName or info.strServiceShortName)


def files_and_dlls(pid: int) -> tuple:
    """(open files, loaded DLLs), each sorted. Raises psutil errors."""
    p = psutil.Process(pid)
    files = sorted({f.path for f in p.open_files()})
    dlls = sorted({m.path for m in p.memory_maps()
                   if m.path.lower().endswith(('.dll', '.exe', '.sys'))})
    return files, dlls
