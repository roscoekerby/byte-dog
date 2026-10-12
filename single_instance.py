"""One ByteDog at a time, held by a named mutex for the life of the process.

Two instances would run two guardians against the same hogs. A restart as
administrator launches the new instance before the old one has exited, so it
passes RESTART_FLAG and waits for the mutex instead of giving up."""
from __future__ import annotations

import ctypes
import sys
import time

MUTEX_NAME = 'Local\\ByteDog.SingleInstance'
RESTART_FLAG = '--after-restart'
RESTART_WAIT_S = 15.0
_POLL_S = 0.2
_ERROR_ALREADY_EXISTS = 183


def _kernel32():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return k32


def acquire(name: str = MUTEX_NAME, wait_s: float = 0.0) -> int | None:
    """The mutex handle if no other instance holds it (keep it open), else None.
    An elevated instance's mutex is access-denied to a normal one: still taken."""
    if sys.platform != 'win32':
        return 1
    k32 = _kernel32()
    deadline = time.monotonic() + wait_s
    while True:
        handle = k32.CreateMutexW(None, False, name)
        if handle and ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
            return handle
        if handle:
            k32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_S)


def release(handle: int | None) -> None:
    if handle and sys.platform == 'win32':
        _kernel32().CloseHandle(handle)
