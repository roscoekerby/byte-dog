"""HogPanel widget: row order, highlight, grouping and action routing,
driven with a fake app (no real processes are touched)."""
import sys
import tkinter as tk
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian import DEFAULT_PROTECTED
from hog_model import build_rows
from hog_panel import HogPanel
from conftest import InlineThread

GB = 1024 ** 3
PROCS = [
    {'pid': 1, 'name': 'Memory Compression', 'rss': 12 * GB},
    {'pid': 2, 'name': 'chrome.exe', 'rss': GB // 2, 'ctype': 'renderer'},
    {'pid': 3, 'name': 'chrome.exe', 'rss': GB // 3, 'ctype': 'renderer'},
    {'pid': 4, 'name': 'llama-server.exe', 'rss': GB},
    {'pid': 5, 'name': 'claude.exe', 'rss': GB // 4},
]


class FakeGuardian:
    def __init__(self):
        self.suspended = {}
        self.config = type('C', (), {'protected_names': lambda self: DEFAULT_PROTECTED})()


class FakeSource:
    def __init__(self, guardian):
        self.guardian = guardian

    def rows(self, protected, suspended, self_pid):
        return build_rows(PROCS, protected, suspended_pids=suspended,
                          labels={5: 'bd-1 [busy] Hog panel (byte-dog)'})

    def summarise(self, pid):
        return 'note'


class FakeApp:
    def __init__(self):
        self.guardian = FakeGuardian()
        self.hog_source = FakeSource(self.guardian)
        self.calls = []

    def suspend_pid(self, pid, name, parent=None):
        self.calls.append(('suspend', pid))
        self.guardian.suspended[pid] = name
        return True

    def resume_pid(self, pid, name, parent=None):
        self.calls.append(('resume', pid))
        self.guardian.suspended.pop(pid, None)
        return True

    def kill_pid(self, pid, name, parent=None):
        self.calls.append(('kill', pid))
        return None  # user cancelled

    def open_hog_panel(self):
        self.calls.append(('open',))


@pytest.fixture
def panel(dog):
    app = FakeApp()
    p = HogPanel(dog.root, app, compact=True)
    p.refresh = lambda **k: None      # no background threads in tests
    rows = app.hog_source.rows(DEFAULT_PROTECTED, set(), None)
    p._loaded(rows)
    yield p, app
    p.destroy()


def test_flat_order_is_rank_order_with_top_highlighted(panel):
    p, _ = panel
    iids = p.tree.get_children()
    assert iids[:4] == ('p:4', 'p:2', 'p:3', 'p:5')
    assert 'next' in p.tree.item('p:4', 'tags')
    assert p.tree.set('p:4', 'rank') == '1'
    assert p.tree.set('p:1', 'state') == 'protected'
    assert 'llama-server.exe' in p.status.cget('text')


def test_group_mode_nests_processes_under_apps(panel):
    p, _ = panel
    p.group_var.set(True)
    p._render()
    groups = p.tree.get_children()
    assert groups[:2] == ('g:llama-server.exe', 'g:chrome.exe')
    assert p.tree.get_children('g:chrome.exe') == ('p:2', 'p:3')
    assert p.tree.item('g:chrome.exe', 'text') == 'chrome.exe x2'


def test_actions_route_to_app_and_need_a_process_row(panel):
    p, app = panel
    p._act('suspend')
    assert app.calls == [] and 'Select a single process' in p.status.cget('text')
    p.tree.selection_set('p:2')
    p._act('suspend')
    assert app.calls == [('suspend', 2)]
    assert 'Suspended chrome.exe (PID 2)' in p.status.cget('text')
    p._act('kill')
    assert 'cancelled' in p.status.cget('text')


def test_ai_note_only_for_claude_rows(panel):
    p, _ = panel
    p.tree.selection_set('p:2')
    p._summarise()
    assert 'claude.exe rows' in p.status.cget('text')
    assert str(p.note_btn.cget('state')) == 'normal'


def test_label_column_shows_session_info(panel):
    p, _ = panel
    assert p.tree.set('p:5', 'what') == 'bd-1 [busy] Hog panel (byte-dog)'


def test_full_view_button_opens_panel(panel):
    p, app = panel
    buttons = [w for bar in p.winfo_children() for w in bar.winfo_children()
               if isinstance(w, tk.Button) and w.cget('text') == 'Full view']
    assert len(buttons) == 1
    buttons[0].invoke()
    assert app.calls == [('open',)]


def test_failed_scan_reports_and_keeps_refreshing(dog, monkeypatch):
    """Regression: the worker's lambda used the except-bound name after the
    block ended, so _failed never ran and _busy stuck True forever."""
    monkeypatch.setattr('hog_panel.threading.Thread', InlineThread)
    app = FakeApp()

    def boom(*a):
        raise RuntimeError('odd session json')
    app.hog_source.rows = boom
    p = HogPanel(dog.root, app, compact=True)
    try:
        p.refresh(force=True)
        for _ in range(200):
            dog.root.update()
            if 'Scan failed' in p.status.cget('text'):
                break
            dog.root.after(10)
        assert 'Scan failed: odd session json' in p.status.cget('text')
        assert p._busy is False and p._timer
    finally:
        p.destroy()


def test_using_the_panel_pins_its_window(panel):
    p, _ = panel
    top = p.winfo_toplevel()
    top.in_use = False
    p.tree.selection_set('p:2')
    p._act('resume')
    assert top.in_use is True


def test_render_keeps_scroll_position(dog):
    app = FakeApp()
    many = [{'pid': 100 + i, 'name': f'p{i}.exe', 'rss': (300 - i) * 1024 ** 2} for i in range(200)]
    app.hog_source.rows = lambda *a: build_rows(many, DEFAULT_PROTECTED)
    win = tk.Toplevel(dog.root)
    p = HogPanel(win, app)
    p.refresh = lambda **k: None
    p.pack(fill='both', expand=True)
    try:
        p._loaded(app.hog_source.rows())
        dog.root.update()
        p.tree.yview_moveto(0.5)
        dog.root.update()
        before = p.tree.yview()[0]
        p._render()
        dog.root.update()
        assert abs(p.tree.yview()[0] - before) < 0.02 and before > 0.3
    finally:
        win.destroy()
