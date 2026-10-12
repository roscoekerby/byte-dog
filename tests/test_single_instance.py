"""Only one ByteDog runs at a time (two guardians would fight over the same
hogs), and a restart-as-admin waits for the old instance to let go."""
import sys
import threading
import time
import uuid

import pytest

import single_instance
from guardian import _elevate_command

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='named mutexes are Windows-only')


def _name():
    return f"Local\\ByteDogTest.{uuid.uuid4().hex}"


def test_second_acquire_is_refused_until_the_first_releases():
    name = _name()
    first = single_instance.acquire(name)
    assert first is not None
    assert single_instance.acquire(name) is None
    single_instance.release(first)
    again = single_instance.acquire(name)
    assert again is not None
    single_instance.release(again)


def test_waiting_acquire_gets_it_once_the_old_instance_exits():
    name = _name()
    old = single_instance.acquire(name)
    threading.Timer(0.3, single_instance.release, args=(old,)).start()
    started = time.monotonic()
    new = single_instance.acquire(name, wait_s=5)
    assert new is not None and time.monotonic() - started < 3
    single_instance.release(new)


def test_restart_command_carries_the_wait_flag(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['bytedog.py'])
    _, params = _elevate_command([single_instance.RESTART_FLAG])
    assert single_instance.RESTART_FLAG in params
    _, plain = _elevate_command()
    assert single_instance.RESTART_FLAG not in plain


def test_admin_prompt_restarts_elevated_and_quits(dog, monkeypatch):
    launched, quit_calls = [], []
    monkeypatch.setattr('bytedog.messagebox.askyesno', lambda *a, **k: True)
    monkeypatch.setattr('bytedog.relaunch_elevated', lambda extra: launched.append(extra) or True)
    monkeypatch.setattr(dog.root, 'quit', lambda: quit_calls.append(1))
    assert dog.offer_admin_restart("Changing startup entries")
    assert launched == [[single_instance.RESTART_FLAG]] and quit_calls == [1]


def test_declined_admin_prompt_keeps_running(dog, monkeypatch):
    monkeypatch.setattr('bytedog.messagebox.askyesno', lambda *a, **k: False)
    monkeypatch.setattr('bytedog.relaunch_elevated', lambda extra: pytest.fail('relaunched'))
    assert not dog.offer_admin_restart("Changing startup entries")


def test_machine_wide_startup_toggle_offers_the_restart(dog, monkeypatch):
    offered = []
    monkeypatch.setattr('bytedog.is_admin', lambda: False)
    monkeypatch.setattr(dog, 'offer_admin_restart', lambda action: offered.append(action))
    monkeypatch.setattr(dog, '_startup_set_approved_state', lambda *a: pytest.fail('changed'))
    monkeypatch.setattr(dog, '_startup_items', {'hklm:x': {
        'name': 'Vendor', 'command': 'v.exe', 'source': 'HKLM Run', 'enabled': True,
        'requires_admin': True, 'value_name': 'Vendor', 'folder': 'Run'}})
    dog.startup_tree.insert('', 'end', iid='hklm:x', values=('Vendor', 'v.exe', 'HKLM Run', 'Enabled'))
    dog.startup_tree.selection_set('hklm:x')
    dog._startup_on_select()
    assert str(dog.startup_toggle_btn.cget('state')) == 'normal'
    dog.startup_toggle_btn.invoke()
    assert offered and 'HKLM' in offered[0]
    dog.startup_tree.delete('hklm:x')
