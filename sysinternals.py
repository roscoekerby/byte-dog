"""Find, fetch and open Sysinternals Process Explorer.

Process Explorer has no automation API; the one external control is
`procexp64.exe /s:<PID>`, which opens it with that process selected. The
Sysinternals licence does not allow bundling it, so ByteDog uses the user's
copy or downloads the official zip from Microsoft on request. A binary only runs if it is
Microsoft-signed and its version info names Process Explorer, verified on a
locked handle that stays locked until the process has started.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import urllib.request
import zipfile
import zlib
from pathlib import Path

from safety import is_microsoft
from signing import locked_signer

PROCEXP_URL = 'https://download.sysinternals.com/files/ProcessExplorer.zip'
PROCEXP_EXE = 'procexp64.exe'
_LOCALAPPDATA = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
LOCAL_DIR = _LOCALAPPDATA / 'ByteDog' / 'sysinternals'
SEARCH_DIRS = (Path(r'C:\Tools\Sysinternals'), Path(r'C:\SysinternalsSuite'),
               Path(r'C:\Program Files\Sysinternals'), LOCAL_DIR)
DOWNLOAD_TIMEOUT_S = 60
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_EXE_BYTES = 50 * 1024 * 1024
PRODUCT_NAME = 'Process Explorer'


class ToolError(RuntimeError):
    """User-facing failure to locate, verify or launch a tool."""


def product_name(path) -> str:
    """ProductName from the file's version resource ('' if none)."""
    import ctypes
    from ctypes import wintypes as w
    ver = ctypes.WinDLL('version')
    ver.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = w.DWORD
    ver.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
    ver.GetFileVersionInfoW.restype = w.BOOL
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(w.UINT)]
    ver.VerQueryValueW.restype = w.BOOL
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ''
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return ''
    ptr, n = ctypes.c_void_p(), w.UINT(0)
    if not ver.VerQueryValueW(buf, r'\VarFileInfo\Translation', ctypes.byref(ptr),
                              ctypes.byref(n)) or n.value < 4:
        return ''
    lang, cp = ctypes.cast(ptr, ctypes.POINTER(w.WORD))[0:2]
    key = rf'\StringFileInfo\{lang:04x}{cp:04x}\ProductName'
    if not ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
        return ''
    return ctypes.wstring_at(ptr, n.value).rstrip(chr(0) + ' ')


def _verified(signer: str, path) -> bool:
    return is_microsoft(signer) and PRODUCT_NAME.lower() in product_name(path).lower()


def is_trusted(path: Path) -> bool:
    """Microsoft-signed (exact organisation) AND its version info says Process
    Explorer, checked on a locked, uncached read. Never trusts a location."""
    with locked_signer(path) as signer:
        return _verified(signer, path)


def find_procexp(saved: str | None = None) -> Path | None:
    candidates = [Path(saved)] if saved else []
    for exe in (PROCEXP_EXE, 'procexp.exe'):
        found = shutil.which(exe)
        if found:
            candidates.append(Path(found))
    candidates += [d / PROCEXP_EXE for d in SEARCH_DIRS]
    for c in candidates:
        if c.is_file() and is_trusted(c):
            return c
    return None


def download_procexp(dest: Path = LOCAL_DIR, opener=urllib.request.urlopen) -> Path:
    """Fetch the official zip, extract procexp64.exe, verify the signature.
    Raises ToolError; never leaves an unverified binary behind."""
    try:
        with opener(PROCEXP_URL, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            data = resp.read(MAX_ZIP_BYTES + 1)
    except OSError as e:
        raise ToolError(f"download failed: {e}") from e
    if len(data) > MAX_ZIP_BYTES:
        raise ToolError("download too large; refusing")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next((n for n in zf.namelist() if n.lower() == PROCEXP_EXE), None)
            if not member:
                raise ToolError(f"{PROCEXP_EXE} not found in the download")
            if zf.getinfo(member).file_size > MAX_EXE_BYTES:
                raise ToolError(f"{PROCEXP_EXE} in the download is too large; refusing")
            payload = zf.read(member)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError) as e:
        raise ToolError(f"download is not a valid zip: {e}") from e
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / PROCEXP_EXE
    tmp = target.with_suffix('.partial.exe')
    try:
        tmp.write_bytes(payload)
        if not is_trusted(tmp):
            tmp.unlink(missing_ok=True)
            raise ToolError("downloaded file is not Microsoft-signed Process Explorer; deleted it")
        os.replace(tmp, target)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"could not save Process Explorer: {e}") from e
    return target


def open_in_procexp(exe: Path, pid: int) -> None:
    """Launch Process Explorer with `pid` selected. The file stays locked
    (no writes, renames or deletes) from verification until the process has
    been created, so the verified bytes are the bytes that run."""
    with locked_signer(exe) as signer:
        if not _verified(signer, exe):
            raise ToolError(f"{exe} is not Microsoft-signed Process Explorer; refusing to run it")
        try:
            subprocess.Popen([str(exe), '/accepteula', f'/s:{int(pid)}'], close_fds=True)
        except OSError as e:
            raise ToolError(f"could not start Process Explorer: {e}") from e
