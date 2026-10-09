"""bytedog.py per-process actions used by the hog panel and Processes tab:
suspend tracking, PID-reuse guard, Resume All, alert fallback messages."""
import os
import subprocess
import sys
import time

import psutil
import pytest


@pytest.fixture
def victim():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    time.sleep(0.5)
    yield proc.pid, psutil.Process(proc.pid).name()
    proc.kill()


def test_suspend_is_tracked_and_resume_all_undoes_it(dog, victim):
    pid, name = victim
    dog.guardian.suspended.clear()
    assert dog.suspend_pid(pid, name) is True
    assert psutil.Process(pid).status() == psutil.STATUS_STOPPED
    assert dog.guardian.suspended == {pid: name}
    dog._resume_all_suspended()
    assert psutil.Process(pid).status() != psutil.STATUS_STOPPED
    assert dog.guardian.suspended == {}


def test_pid_reuse_guard_refuses_a_different_process(dog, victim):
    pid, _ = victim
    assert dog.suspend_pid(pid, 'notepad.exe') is False
    assert psutil.Process(pid).status() != psutil.STATUS_STOPPED
    assert dog.kill_pid(pid, 'notepad.exe') is False
    assert psutil.pid_exists(pid)


def test_failed_resume_keeps_tracking(dog, monkeypatch):
    dog.guardian.suspended.clear()
    dog.guardian.suspended[os.getpid()] = 'python.exe'
    monkeypatch.setattr(dog.process_manager, 'resume_process', lambda pid: False)
    assert dog.resume_pid(os.getpid(), 'python.exe') is False
    assert os.getpid() in dog.guardian.suspended   # still alive -> Resume All retries
    dog.guardian.suspended.clear()


def test_resume_drops_dead_pids(dog, monkeypatch):
    dead = 999_999_99
    dog.guardian.suspended.clear()
    dog.guardian.suspended[dead] = 'gone.exe'
    monkeypatch.setattr(dog.process_manager, 'resume_process', lambda pid: False)
    dog.resume_pid(dead, 'gone.exe')
    assert dead not in dog.guardian.suspended


def test_action_message_without_alert_goes_to_status_bar(dog):
    dog.guardian_alert_window = None
    dog.update_alert_hogs("Could not kill x.exe (PID 1)")
    assert dog.status_label.cget('text') == "Could not kill x.exe (PID 1)"


def test_warn_alert_stays_open_while_in_use(dog, monkeypatch):
    monkeypatch.setattr('hog_panel.HogPanel.refresh', lambda self: None)
    dog.show_guardian_alert({'type': 'warn', 'ram_pct': 76.0, 'used_gb': 48, 'total_gb': 64})
    alert = dog.guardian_alert_window
    try:
        alert.in_use = True
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            dog.root.update()
            time.sleep(0.02)
        assert alert.winfo_exists()
        texts = [w.cget('text') for w in alert.winfo_children()[-1].winfo_children()
                 if hasattr(w, 'cget') and w.winfo_class() == 'Label']
        assert "Kept open while you use it" in texts
    finally:
        alert.destroy()
