"""Multi-select bulk actions and Suspend/Resume Tree, through the real app.
Targets are throwaway processes; protected processes only ever take the
skip / cancel paths."""
import subprocess
import sys
import time

import psutil
import pytest

import process_tools_ui as ui
from conftest import InlineThread

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr('process_tools_ui.threading.Thread', InlineThread)


@pytest.fixture
def victims():
    procs = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                              creationflags=NO_WINDOW) for _ in range(3)]
    time.sleep(0.5)
    yield [(p.pid, psutil.Process(p.pid).name()) for p in procs]
    for p in procs:
        p.kill()


def _pump(dog, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        dog.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def _stopped(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_STOPPED
    except psutil.Error:
        return False


def test_bulk_suspend_then_resume(dog, victims, inline):
    dog.guardian.suspended.clear()
    dog.tools.bulk('suspend', victims, dog.root)
    assert _pump(dog, lambda: all(_stopped(p) for p, _ in victims))
    assert set(dog.guardian.suspended) == {p for p, _ in victims}
    assert 'Suspended 3 process(es)' in dog.status_label.cget('text')
    dog.tools.bulk('resume', victims, dog.root)
    assert _pump(dog, lambda: not any(_stopped(p) for p, _ in victims))
    assert dog.guardian.suspended == {}


def test_bulk_kill_needs_one_confirmation(dog, victims, inline, monkeypatch):
    asked = []
    monkeypatch.setattr(ui.messagebox, 'askyesno', lambda *a, **k: asked.append(a) or True)
    dog.tools.bulk('kill', victims, dog.root)
    assert len(asked) == 1 and 'Kill these 3 processes' in asked[0][1]
    assert _pump(dog, lambda: not any(psutil.pid_exists(p) for p, _ in victims), 15)


def test_bulk_kill_cancel_kills_nothing(dog, victims, inline, monkeypatch):
    monkeypatch.setattr(ui.messagebox, 'askyesno', lambda *a, **k: False)
    dog.tools.bulk('kill', victims, dog.root)
    assert all(psutil.pid_exists(p) for p, _ in victims)


def test_protected_ones_can_be_skipped_or_cancelled(dog, victims, inline, monkeypatch):
    winlogon = next(p for p in psutil.process_iter(['name']) if p.info['name'] == 'winlogon.exe')
    targets = victims[:1] + [(winlogon.pid, 'winlogon.exe')]
    prompts = []
    monkeypatch.setattr(ui.messagebox, 'askyesnocancel',
                        lambda *a, **k: prompts.append(a) or None)
    dog.guardian.suspended.clear()
    dog.tools.bulk('suspend', targets, dog.root)
    assert 'winlogon.exe' in prompts[0][1] and dog.guardian.suspended == {}
    monkeypatch.setattr(ui.messagebox, 'askyesnocancel', lambda *a, **k: False)
    dog.tools.bulk('suspend', targets, dog.root)
    assert _pump(dog, lambda: _stopped(victims[0][0]))
    assert winlogon.pid not in dog.guardian.suspended
    dog.tools.bulk('resume', victims[:1], dog.root)
    _pump(dog, lambda: not _stopped(victims[0][0]))


def test_stale_targets_are_dropped(dog, victims, inline):
    dog.tools.bulk('suspend', [(victims[0][0], 'notepad.exe')], dog.root)
    assert 'Nothing to suspend (1 already gone)' in dog.status_label.cget('text')
    assert not _stopped(victims[0][0])


def test_suspend_tree_tracks_every_paused_process(dog, inline):
    child = "import subprocess, sys, time\n" \
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n" \
            "time.sleep(60)"
    root = subprocess.Popen([sys.executable, '-c', child], creationflags=NO_WINDOW)
    try:
        assert _pump(dog, lambda: any(k.name().lower().startswith('python') for k in
                                      psutil.Process(root.pid).children(recursive=True)))
        all_kids = psutil.Process(root.pid).children(recursive=True)
        kids = [k.pid for k in all_kids if k.name().lower().startswith('python')]
        conhost = [k.pid for k in all_kids if k.name().lower() == 'conhost.exe']
        dog.guardian.suspended.clear()
        dog.tools.suspend_tree(root.pid, psutil.Process(root.pid).name(), dog.root)
        assert _pump(dog, lambda: _stopped(root.pid))
        assert {root.pid, *kids} <= set(dog.guardian.suspended)
        # the console host Windows attaches is a protected Windows component
        assert not set(conhost) & set(dog.guardian.suspended)
        assert 'protected skipped' in dog.status_label.cget('text') or not conhost
        dog._resume_all_suspended()
        assert not _stopped(root.pid) and dog.guardian.suspended == {}
    finally:
        for p in [root.pid] + [k.pid for k in psutil.Process(root.pid).children(recursive=True)]:
            try:
                psutil.Process(p).kill()
            except psutil.Error:
                pass


def test_processes_tab_routes_multi_selection_to_bulk(dog, monkeypatch):
    calls = []
    monkeypatch.setattr(dog.tools, 'bulk', lambda action, targets, parent:
                        calls.append((action, targets)))
    monkeypatch.setattr(dog, '_selected_processes', lambda: [(1, 'a.exe'), (2, 'b.exe')])
    dog.suspend_selected_process()
    dog.kill_selected_process()
    assert calls == [('suspend', [(1, 'a.exe'), (2, 'b.exe')]),
                     ('kill', [(1, 'a.exe'), (2, 'b.exe')])]
