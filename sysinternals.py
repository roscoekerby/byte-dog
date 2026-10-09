"""Find, fetch and open Sysinternals Process Explorer.

Process Explorer has no automation API; the one external control is
`procexp64.exe /s:<PID>`, which opens it with that process selected. The
Sysinternals licence does not allow bundling it, so ByteDog uses the user's
copy or downloads the official zip from Microsoft on request. Any binary is
checked for a Microsoft Authenticode signature before it is ever run.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from signing import signer_of

PROCEXP_URL = 'https://download.sysinternals.com/files/ProcessExplorer.zip'
PROCEXP_EXE = 'procexp64.exe'
LOCAL_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ByteDog' / 'sysinternals'
SEARCH_DIRS = (Path(r'C:\Tools\Sysinternals'), Path(r'C:\SysinternalsSuite'),
               Path(r'C:\Program Files\Sysinternals'), LOCAL_DIR)
STORE_ALIAS_DIR = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WindowsApps'
DOWNLOAD_TIMEOUT_S = 60
MAX_ZIP_BYTES = 50 * 1024 * 1024


class ToolError(RuntimeError):
    """User-facing failure to locate, verify or launch a tool."""


def is_trusted(path: Path) -> bool:
    """Microsoft-signed, or a Microsoft Store app alias (Store packages are
    signature-checked by Windows at install; the alias itself is a link)."""
    path = Path(path)
    if STORE_ALIAS_DIR and path.parent == STORE_ALIAS_DIR:
        return True
    return signer_of(str(path)).lower().startswith('microsoft')


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
            payload = zf.read(member)
    except zipfile.BadZipFile as e:
        raise ToolError(f"download is not a valid zip: {e}") from e
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / PROCEXP_EXE
    tmp = target.with_suffix('.partial.exe')
    tmp.write_bytes(payload)
    if not is_trusted(tmp):
        tmp.unlink(missing_ok=True)
        raise ToolError("downloaded file is not signed by Microsoft; deleted it")
    os.replace(tmp, target)
    return target


def open_in_procexp(exe: Path, pid: int) -> None:
    """Launch Process Explorer with `pid` selected."""
    if not is_trusted(exe):
        raise ToolError(f"{exe} is not signed by Microsoft; refusing to run it")
    try:
        subprocess.Popen([str(exe), '/accepteula', f'/s:{int(pid)}'], close_fds=True)
    except OSError as e:
        raise ToolError(f"could not start Process Explorer: {e}") from e
