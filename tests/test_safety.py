"""safety.py: protection is decided by what Windows says about the process
(critical flag, protected-process level, Microsoft-signed system binary),
not by its name. Pure decision tests plus real-System32 integration tests."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety import Facts, ProtectionChecker, decide
from signing import signer_of

WIN = r'C:\Windows'
win_only = pytest.mark.skipif(sys.platform != 'win32', reason='Windows APIs')


def F(**kw):
    base = dict(pid=1234, name='app.exe', path=r'C:\Apps\app.exe', critical=False,
                ppl=0, signer='', opened=True)
    base.update(kw)
    return Facts(**base)


def test_critical_process_is_protected():
    v = decide(F(critical=True), WIN)
    assert v.protected and 'critical' in v.reason.lower()


def test_protected_process_light_is_protected():
    v = decide(F(ppl=1, path=r'C:\ProgramData\Microsoft\Windows Defender\MsMpEng.exe'), WIN)
    assert v.protected and 'protected process' in v.reason.lower()


def test_microsoft_signed_system_binary_is_protected():
    v = decide(F(name='svchost.exe', path=r'C:\Windows\System32\svchost.exe',
                 signer='Microsoft Corporation'), WIN)
    assert v.protected and 'windows component' in v.reason.lower()


def test_fake_svchost_outside_windows_is_not_protected():
    v = decide(F(name='svchost.exe', path=r'C:\Users\x\AppData\Local\Temp\svchost.exe',
                 signer='Microsoft Corporation'), WIN)
    assert not v.protected


def test_unsigned_binary_in_windows_dir_is_not_protected():
    v = decide(F(name='svchost.exe', path=r'C:\Windows\Temp\svchost.exe', signer=''), WIN)
    assert not v.protected


def test_windows_temp_is_never_trusted_even_if_signed():
    v = decide(F(path=r'C:\Windows\Temp\copied.exe', signer='Microsoft Corporation'), WIN)
    assert not v.protected


def test_third_party_signed_in_windows_dir_is_not_protected():
    v = decide(F(path=r'C:\Windows\System32\vendor.exe', signer='NVIDIA Corporation'), WIN)
    assert not v.protected


def test_kernel_pseudo_processes_are_protected():
    for name in ('System', 'Registry', 'Memory Compression', 'Secure System', 'vmmemWSL'):
        v = decide(F(name=name, path='', opened=False), WIN)
        assert v.protected, name
    assert decide(F(pid=4, name='whatever', path=''), WIN).protected


def test_unopenable_process_fails_safe():
    v = decide(F(path='', opened=False), WIN)
    assert v.protected and 'could not verify' in v.reason.lower()


def test_user_list_and_self():
    assert decide(F(name='Steam.exe'), WIN, user_names={'steam.exe'}).protected
    assert decide(F(pid=77), WIN, self_pid=77).protected


def test_normal_app_is_not_protected():
    v = decide(F(name='chrome.exe', path=r'C:\Program Files\Google\Chrome\chrome.exe',
                 signer='Google LLC'), WIN)
    assert not v.protected and v.reason == ''


def test_checker_caches_by_pid_and_create_time():
    calls = []

    def gather(pid, name):
        calls.append(pid)
        return F(pid=pid, name=name)
    c = ProtectionChecker(gather=gather, windows_dir=WIN)
    c.check({'pid': 5, 'name': 'a.exe', 'ctime': 1})
    c.check({'pid': 5, 'name': 'a.exe', 'ctime': 1})
    c.check({'pid': 5, 'name': 'a.exe', 'ctime': 2})   # PID reused
    assert calls == [5, 5]


def test_checker_reads_user_list_live():
    names = {'x.exe'}
    c = ProtectionChecker(gather=lambda pid, name: F(pid=pid, name=name), windows_dir=WIN,
                          user_names=lambda: names)
    assert c.check({'pid': 1, 'name': 'x.exe'}).protected
    names.clear()
    assert not c.check({'pid': 1, 'name': 'x.exe'}).protected


# ── real Windows ─────────────────────────────────────────────────────────

@win_only
def test_real_signers():
    assert signer_of(r'C:\Windows\System32\svchost.exe').startswith('Microsoft')
    assert signer_of(r'C:\Windows\System32\notepad.exe').startswith('Microsoft')  # catalog-signed
    assert signer_of(r'C:\Windows\System32\does-not-exist.exe') == ''


@win_only
def test_unsigned_file_has_no_signer(tmp_path):
    f = tmp_path / 'svchost.exe'
    f.write_bytes(b'MZ' + b'\0' * 1024)
    assert signer_of(str(f)) == ''


@win_only
def test_real_processes():
    import psutil
    c = ProtectionChecker()
    winlogon = next(p for p in psutil.process_iter(['name']) if p.info['name'] == 'winlogon.exe')
    assert c.check({'pid': winlogon.pid, 'name': 'winlogon.exe'}).protected
    assert c.check({'pid': 4, 'name': 'System'}).protected
    me = c.check({'pid': os.getpid(), 'name': 'python.exe'})
    assert not me.protected


@win_only
def test_copied_svchost_running_from_temp_is_not_protected(tmp_path):
    """A genuine Microsoft-signed binary copied out of System32 loses protection:
    location matters, not the name or the signature alone."""
    fake = tmp_path / 'svchost.exe'
    shutil.copy(sys.executable, fake)   # runnable stand-in with the spoofed name
    proc = subprocess.Popen([str(fake), '-c', 'import time; time.sleep(30)'],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        time.sleep(0.5)
        v = ProtectionChecker().check({'pid': proc.pid, 'name': 'svchost.exe'})
        assert not v.protected, v
    finally:
        proc.kill()


# ── review fixes: fail safe, never cache an unknown ──────────────────────

def test_unknown_signature_in_windows_dir_is_protected():
    v = decide(F(path=r'C:\Windows\explorer.exe', signer='', signer_known=False), WIN)
    assert v.protected and 'could not be checked' in v.reason


def test_unknown_signature_outside_windows_is_not_protected():
    assert not decide(F(signer='', signer_known=False), WIN).protected


def test_missing_path_or_unknown_critical_is_protected():
    assert decide(F(path=''), WIN).protected
    assert decide(F(critical=None), WIN).protected


def test_signer_must_match_microsoft_exactly():
    path = r'C:\Windows\System32\x.exe'
    assert decide(F(path=path, signer='Microsoft Corporation'), WIN).protected
    assert not decide(F(path=path, signer='Microsoftish Labs LLC'), WIN).protected


def test_second_bytedog_instance_is_protected_by_image_path():
    exe = r'C:\Apps\ByteDog\ByteDog.exe'
    assert decide(F(pid=999, path=exe), WIN, self_pid=1, self_exe=exe).protected


def test_incomplete_facts_and_missing_ctime_are_not_cached():
    calls = []

    def gather(pid, name):
        calls.append(pid)
        return F(pid=pid, name=name, path=r'C:\Windows\explorer.exe', signer_known=False)
    c = ProtectionChecker(gather=gather, windows_dir=WIN)
    c.check({'pid': 5, 'name': 'explorer.exe', 'ctime': 1})
    c.check({'pid': 5, 'name': 'explorer.exe', 'ctime': 1})
    c.check({'pid': 6, 'name': 'a.exe', 'ctime': None})
    c.check({'pid': 6, 'name': 'a.exe', 'ctime': None})
    assert calls == [5, 5, 6, 6]


@win_only
def test_signature_failure_is_not_cached(monkeypatch):
    import signing
    path = r'C:\Windows\System32\winver.exe'
    signing._cache.clear()

    def boom(p, h):
        raise signing._Unknown('crypto service busy')
    monkeypatch.setattr(signing, '_signer_from_handle', boom)
    assert signing.signature(path) == ('', False)
    monkeypatch.undo()
    assert signing.signature(path) == ('Microsoft Corporation', True)


@win_only
def test_check_pid_uses_live_start_time():
    c = ProtectionChecker(self_pid=os.getpid())
    assert c.check_pid(os.getpid(), 'python.exe').protected      # self
    assert c.check_pid(999_999_99, 'gone.exe').protected         # gone -> unverifiable
