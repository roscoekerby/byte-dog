"""sysinternals.py: locate / download / verify Process Explorer. Network is
faked; signature checks run against real files."""
import io
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sysinternals as si

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Authenticode is Windows-only')
MS_SIGNED = Path(r'C:\Windows\System32\notepad.exe')


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


def test_download_accepts_microsoft_signed(tmp_path):
    data = _zip('procexp64.exe', MS_SIGNED.read_bytes())
    exe = si.download_procexp(tmp_path, opener=lambda url, timeout: FakeResp(data))
    assert exe == tmp_path / 'procexp64.exe' and exe.exists()


def test_download_rejects_unsigned_and_cleans_up(tmp_path):
    data = _zip('procexp64.exe', b'MZ' + b'\0' * 2048)
    with pytest.raises(si.ToolError, match='not signed'):
        si.download_procexp(tmp_path, opener=lambda url, timeout: FakeResp(data))
    assert list(tmp_path.iterdir()) == []


def test_download_errors_are_explicit(tmp_path):
    def offline(url, timeout):
        raise OSError('no network')
    with pytest.raises(si.ToolError, match='download failed'):
        si.download_procexp(tmp_path, opener=offline)
    with pytest.raises(si.ToolError, match='not found'):
        si.download_procexp(tmp_path, opener=lambda u, timeout: FakeResp(_zip('other.exe', b'x')))
    with pytest.raises(si.ToolError, match='valid zip'):
        si.download_procexp(tmp_path, opener=lambda u, timeout: FakeResp(b'not a zip'))


def test_find_prefers_saved_trusted_path_and_skips_fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(si, 'SEARCH_DIRS', (tmp_path / 'none',))
    monkeypatch.setattr(si.shutil, 'which', lambda exe: None)
    good = tmp_path / 'good' / 'procexp64.exe'
    good.parent.mkdir()
    shutil.copy(MS_SIGNED, good)
    fake = tmp_path / 'procexp64.exe'
    fake.write_bytes(b'MZ' + b'\0' * 2048)
    assert si.find_procexp(str(fake)) is None
    assert si.find_procexp(str(good)) == good


def test_open_refuses_unsigned(tmp_path):
    fake = tmp_path / 'procexp64.exe'
    fake.write_bytes(b'MZ' + b'\0' * 2048)
    with pytest.raises(si.ToolError, match='not signed'):
        si.open_in_procexp(fake, 4)
