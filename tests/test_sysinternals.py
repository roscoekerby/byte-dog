"""sysinternals.py: locate / download / verify Process Explorer. Network is
faked; signature and version checks run against real files."""
import io
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sysinternals as si

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Authenticode is Windows-only')
MS_SIGNED = Path(r'C:\Windows\System32\notepad.exe')       # signed, but not Process Explorer
REAL_PROCEXP = si.LOCAL_DIR / 'procexp64.exe'
needs_procexp = pytest.mark.skipif(not REAL_PROCEXP.exists(),
                                   reason='official procexp64.exe not downloaded yet')
UNSIGNED = b'MZ' + b'\0' * 2048


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _zip(name, payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(name, payload)
        zf.writestr('Eula.txt', 'eula')
    return buf.getvalue()


def _serve(data):
    return lambda url, timeout: FakeResp(data)


@needs_procexp
def test_download_accepts_official_process_explorer(tmp_path):
    exe = si.download_procexp(tmp_path, opener=_serve(_zip('procexp64.exe',
                                                           REAL_PROCEXP.read_bytes())))
    assert exe == tmp_path / 'procexp64.exe' and exe.exists()


def test_download_rejects_unsigned_and_cleans_up(tmp_path):
    with pytest.raises(si.ToolError, match='not Microsoft-signed'):
        si.download_procexp(tmp_path, opener=_serve(_zip('procexp64.exe', UNSIGNED)))
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_renamed_microsoft_binary(tmp_path):
    with pytest.raises(si.ToolError, match='not Microsoft-signed Process Explorer'):
        si.download_procexp(tmp_path, opener=_serve(_zip('procexp64.exe',
                                                         MS_SIGNED.read_bytes())))


def test_download_errors_are_explicit(tmp_path):
    def offline(url, timeout):
        raise OSError('no network')
    with pytest.raises(si.ToolError, match='download failed'):
        si.download_procexp(tmp_path, opener=offline)
    with pytest.raises(si.ToolError, match='not found'):
        si.download_procexp(tmp_path, opener=_serve(_zip('other.exe', b'x')))
    with pytest.raises(si.ToolError, match='valid zip'):
        si.download_procexp(tmp_path, opener=_serve(b'not a zip'))


def test_oversized_member_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(si, 'MAX_EXE_BYTES', 10)
    with pytest.raises(si.ToolError, match='too large'):
        si.download_procexp(tmp_path, opener=_serve(_zip('procexp64.exe', UNSIGNED)))


@needs_procexp
def test_find_accepts_only_genuine_process_explorer(tmp_path, monkeypatch):
    monkeypatch.setattr(si, 'SEARCH_DIRS', (tmp_path / 'none',))
    monkeypatch.setattr(si.shutil, 'which', lambda exe: None)
    good = tmp_path / 'good' / 'procexp64.exe'
    renamed = tmp_path / 'renamed' / 'procexp64.exe'
    fake = tmp_path / 'fake' / 'procexp64.exe'
    for p in (good, renamed, fake):
        p.parent.mkdir()
    shutil.copy(REAL_PROCEXP, good)
    shutil.copy(MS_SIGNED, renamed)
    fake.write_bytes(UNSIGNED)
    assert si.find_procexp(str(fake)) is None
    assert si.find_procexp(str(renamed)) is None
    assert si.find_procexp(str(good)) == good


def test_open_refuses_unsigned_and_renamed(tmp_path):
    fake = tmp_path / 'procexp64.exe'
    fake.write_bytes(UNSIGNED)
    with pytest.raises(si.ToolError, match='not Microsoft-signed'):
        si.open_in_procexp(fake, 4)
    renamed = tmp_path / 'r' / 'procexp64.exe'
    renamed.parent.mkdir()
    shutil.copy(MS_SIGNED, renamed)
    with pytest.raises(si.ToolError, match='not Microsoft-signed Process Explorer'):
        si.open_in_procexp(renamed, 4)
