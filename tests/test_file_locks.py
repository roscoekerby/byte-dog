"""file_locks.py: 'who is locking this file' via the Windows Restart Manager."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from file_locks import files_and_dlls, who_locks

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Restart Manager is Windows-only')


@pytest.fixture
def locker(tmp_path):
    target = tmp_path / 'locked.txt'
    proc = subprocess.Popen(
        [sys.executable, '-c', f"f=open(r'{target}','w'); import time; time.sleep(30)"],
        creationflags=subprocess.CREATE_NO_WINDOW)
    end = time.monotonic() + 5
    while not target.exists() and time.monotonic() < end:
        time.sleep(0.05)
    time.sleep(0.3)
    yield proc, target
    proc.kill()


def test_who_locks_finds_the_holder(locker):
    proc, target = locker
    holders = who_locks([str(target)])
    assert proc.pid in {h.pid for h in holders}
    h = next(h for h in holders if h.pid == proc.pid)
    assert h.name.lower().startswith('python')


def test_who_locks_unlocked_file_is_empty(tmp_path):
    f = tmp_path / 'free.txt'
    f.write_text('x')
    assert who_locks([str(f)]) == []


def test_who_locks_rejects_empty_input():
    with pytest.raises(ValueError):
        who_locks([])


def test_files_and_dlls_of_a_process(locker):
    proc, target = locker
    files, dlls = files_and_dlls(proc.pid)
    assert str(target) in files
    assert any(d.lower().endswith('kernel32.dll') for d in dlls)
    assert files == sorted(files) and dlls == sorted(dlls)
