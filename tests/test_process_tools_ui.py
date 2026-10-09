"""ProcessTools / DetailsPane driven through the real app (dialogs answered
by monkeypatch, targets are throwaway processes)."""
import os
import subprocess
import sys
import time
import tkinter as tk

import psutil
import pytest

import process_tools_ui as ui

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _spawn_tree():
    child = "import subprocess, sys, time\n" \
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n" \
            "time.sleep(60)"
    root = subprocess.Popen([sys.executable, '-c', child], creationflags=NO_WINDOW)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        kids = [k for k in psutil.Process(root.pid).children(recursive=True)
                if k.name().lower().startswith('python')]
        if kids:
            return root, kids
        time.sleep(0.05)
    raise AssertionError('child never started')


def _pump(dog, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        dog.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_kill_tree_through_ui(dog, monkeypatch):
    monkeypatch.setattr(ui.messagebox, 'askyesno', lambda *a, **k: True)
    root, kids = _spawn_tree()
    name = psutil.Process(root.pid).name()
    dog.tools.kill_tree(root.pid, name, dog.root)
    assert _pump(dog, lambda: not psutil.pid_exists(root.pid)
                 and not any(psutil.pid_exists(k.pid) for k in kids))
    assert 'Killed' in dog.status_label.cget('text')


def test_kill_tree_cancel_does_nothing(dog, monkeypatch):
    monkeypatch.setattr(ui.messagebox, 'askyesno', lambda *a, **k: False)
    root, kids = _spawn_tree()
    try:
        dog.tools.kill_tree(root.pid, psutil.Process(root.pid).name(), dog.root)
        assert psutil.pid_exists(root.pid)
    finally:
        for p in [root.pid] + [k.pid for k in kids]:
            try:
                psutil.Process(p).kill()
            except psutil.Error:
                pass


def test_restart_refuses_protected(dog, monkeypatch):
    warned = []
    monkeypatch.setattr(ui.messagebox, 'showwarning', lambda *a, **k: warned.append(a))
    winlogon = next(p for p in psutil.process_iter(['name']) if p.info['name'] == 'winlogon.exe')
    dog.tools.restart(winlogon.pid, 'winlogon.exe', dog.root)
    assert warned and 'protected' in warned[0][1]
    assert psutil.pid_exists(winlogon.pid)


def test_stale_pid_is_refused(dog):
    dog.tools.kill_tree(os.getpid(), 'notepad.exe', dog.root)
    assert 'no longer notepad.exe' in dog.status_label.cget('text')


def test_menu_has_the_advanced_items(dog):
    menu = tk.Menu(dog.root, tearoff=0)
    dog.tools.add_menu_items(menu, os.getpid(), 'python.exe', dog.root)
    labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1)
              if menu.type(i) in ('command', 'cascade')]
    for want in ('Kill Process Tree', 'Restart', 'Set Priority', 'Set Affinity...',
                 'Open File Location', 'Copy Command Line', 'Open in Process Explorer'):
        assert want in labels


def test_copy_command_line(dog):
    dog.tools.copy(os.getpid(), 'cmdline')
    assert 'pytest' in dog.root.clipboard_get()


def test_details_pane_shows_process_and_protection(dog, monkeypatch):
    from conftest import InlineThread
    monkeypatch.setattr('process_tools_ui.threading.Thread', InlineThread)
    pane = ui.DetailsPane(dog.root, dog)
    try:
        pane.show(os.getpid(), psutil.Process().name())
        assert _pump(dog, lambda: pane.values['path'].cget('text') != '')
        assert 'python' in pane.values['path'].cget('text').lower()
        assert pane.values['protection'].cget('text') == 'ByteDog itself'
    finally:
        pane.destroy()
