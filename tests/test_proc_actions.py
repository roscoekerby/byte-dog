"""proc_actions.py: tree kill, restart, priority, affinity and details,
exercised against real throwaway process trees (never real apps)."""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proc_actions import (
    PRIORITIES, details, is_elevated, kill_tree, restart, set_affinity, set_priority,
    tree_pids,
)

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CHILD = "import subprocess, sys, time\n" \
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n" \
        "time.sleep(60)"


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def tree():
    """parent -> child -> grandchild python processes."""
    script = "import subprocess, sys, time\n" \
             f"subprocess.Popen([sys.executable, '-c', {CHILD!r}])\n" \
             "time.sleep(60)"
    root = subprocess.Popen([sys.executable, '-c', script], creationflags=NO_WINDOW)
    assert _wait(lambda: len(_python_kids(root.pid)) >= 2)
    everyone = [root.pid] + [c.pid for c in psutil.Process(root.pid).children(recursive=True)]
    yield root
    for p in everyone:
        try:
            psutil.Process(p).kill()
        except psutil.Error:
            pass


def _python_kids(pid):
    """Python descendants only (CREATE_NO_WINDOW also attaches a conhost.exe)."""
    try:
        return [k for k in psutil.Process(pid).children(recursive=True)
                if k.name().lower().startswith('python')]
    except psutil.Error:
        return []


def test_tree_pids_lists_descendants_deepest_first(tree):
    pids = tree_pids(tree.pid)
    kids = _python_kids(tree.pid)
    assert pids[-1] == tree.pid and {k.pid for k in kids} <= set(pids)
    grandchild = next(k for k in kids if k.ppid() != tree.pid)
    assert pids.index(grandchild.pid) < pids.index(grandchild.ppid())


def test_kill_tree_kills_everything(tree):
    pids = tree_pids(tree.pid)
    result = kill_tree(tree.pid, is_protected=lambda pid: False)
    assert sorted(result.killed) == sorted(pids) and not result.skipped
    assert _wait(lambda: not any(psutil.pid_exists(p) for p in pids))


def test_kill_tree_skips_protected_children(tree):
    pids = tree_pids(tree.pid)
    guarded = next(k.pid for k in _python_kids(tree.pid) if k.ppid() != tree.pid)
    result = kill_tree(tree.pid, is_protected=lambda pid: pid == guarded)
    assert result.skipped == [guarded]
    assert psutil.pid_exists(guarded)
    psutil.Process(guarded).kill()


def test_restart_relaunches_same_command(tmp_path):
    marker = tmp_path / 'started.txt'
    code = f"open(r'{marker}', 'a').write('x'); import time; time.sleep(60)"
    proc = subprocess.Popen([sys.executable, '-c', code], cwd=str(tmp_path),
                            creationflags=NO_WINDOW)
    assert _wait(marker.exists)
    new_pid = restart(proc.pid, console=False)
    try:
        assert new_pid and new_pid != proc.pid
        assert _wait(lambda: marker.read_text() == 'xx')
        assert not psutil.pid_exists(proc.pid) or psutil.Process(proc.pid).status() == 'zombie'
        assert Path(psutil.Process(new_pid).cwd()) == tmp_path
    finally:
        psutil.Process(new_pid).kill()


def test_priority_and_affinity(tmp_path):
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                            creationflags=NO_WINDOW)
    try:
        set_priority(proc.pid, 'Below normal')
        assert psutil.Process(proc.pid).nice() == PRIORITIES['Below normal']
        set_affinity(proc.pid, [0])
        assert psutil.Process(proc.pid).cpu_affinity() == [0]
        with pytest.raises(ValueError):
            set_priority(proc.pid, 'Ludicrous')
        with pytest.raises(ValueError):
            set_affinity(proc.pid, [])
    finally:
        proc.kill()


def test_details_has_the_essentials():
    d = details(os.getpid())
    assert d['pid'] == os.getpid()
    assert Path(d['path']).name.lower().startswith('python')
    assert d['parent_pid'] == os.getppid()
    assert d['threads'] >= 1 and d['started']
    assert 'cmdline' in d and 'user' in d and 'priority' in d


def test_details_of_missing_process():
    assert details(999_999_99) is None


@pytest.mark.skipif(sys.platform != 'win32', reason='token elevation is Windows-only')
def test_is_elevated_returns_bool_or_none():
    assert is_elevated(os.getpid()) in (True, False)
    assert is_elevated(4) is None


@pytest.mark.skipif(sys.platform != 'win32', reason='parent-process attribute is Windows-only')
def test_spawn_under_shell_inherits_the_shell_as_parent(tmp_path):
    """The de-elevation path of restart(): the new process is created as a
    child of Explorer, so it gets Explorer's (non-admin) token."""
    from proc_actions import _shell_pid, _spawn_under
    shell = _shell_pid()
    assert shell, "no explorer.exe in this session"
    cmd = subprocess.list2cmdline([sys.executable, '-c', 'import time; time.sleep(30)'])
    pid = _spawn_under(shell, sys.executable, cmd, str(tmp_path), 0x08000000)
    try:
        child = psutil.Process(pid)
        assert child.ppid() == shell
        assert Path(child.cwd()) == tmp_path
        assert is_elevated(pid) == is_elevated(shell)
    finally:
        psutil.Process(pid).kill()


def test_details_survives_an_exited_parent():
    """Regression: explorer.exe's parent is long gone; looking up the parent's
    name raised NoSuchProcess and details() reported the process itself as exited."""
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "                     stderr=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    orphan = int(out.stdout.strip())
    try:
        d = details(orphan)
        assert d is not None and d['pid'] == orphan
        assert d['parent_name'] == ''
    finally:
        psutil.Process(orphan).kill()


def test_restart_refuses_before_killing_when_it_cannot_de_elevate(monkeypatch):
    """An elevated ByteDog with no user shell must not relaunch a normal app
    as admin, and must not have killed it either."""
    import proc_actions
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                            creationflags=NO_WINDOW)
    try:
        monkeypatch.setattr(proc_actions, 'is_elevated',
                            lambda pid: pid == proc_actions._self_pid())
        monkeypatch.setattr(proc_actions, '_shell_pid', lambda: None)
        with pytest.raises(OSError, match='without admin'):
            restart(proc.pid, console=False)
        assert psutil.pid_exists(proc.pid) and proc.poll() is None
    finally:
        proc.kill()


def test_plan_uses_exact_image_path():
    from proc_actions import plan_restart
    plan = plan_restart(os.getpid())
    assert Path(plan.exe) == Path(psutil.Process().exe())


def _status(pid):
    try:
        return psutil.Process(pid).status()
    except psutil.Error:
        return None


def test_suspend_and_resume_tree(tree):
    from proc_actions import resume_tree, suspend_tree
    pids = [k.pid for k in _python_kids(tree.pid)] + [tree.pid]
    r = suspend_tree(tree.pid, is_protected=lambda pid: False)
    assert set(pids) <= set(r.done) and not r.failed
    assert all(_status(p) == psutil.STATUS_STOPPED for p in pids)
    r2 = resume_tree(tree.pid)
    assert set(pids) <= set(r2.done)
    assert all(_status(p) != psutil.STATUS_STOPPED for p in pids)


def test_suspend_tree_pauses_root_first_and_skips_protected(tree):
    from proc_actions import resume_tree, suspend_tree
    order = []
    guarded = next(k.pid for k in _python_kids(tree.pid) if k.ppid() != tree.pid)

    def is_protected(pid):
        order.append(pid)
        return pid == guarded
    r = suspend_tree(tree.pid, is_protected)
    try:
        assert r.done[0] == tree.pid
        assert r.skipped == [guarded] and _status(guarded) != psutil.STATUS_STOPPED
    finally:
        resume_tree(tree.pid)
