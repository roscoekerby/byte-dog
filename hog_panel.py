"""Hog panel: every process ranked the way Suspend Top / Kill Top Hog act,
with per-row Suspend / Resume / Kill and on-demand AI notes for Claude
sessions. Embedded compact in the Guardian alert and opened full-size via
"Full view". The app supplies hog_source, guardian and the *_pid actions.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk

from hog_model import group_rows
from session_notes import SUMMARY_MODEL, SummaryError

REFRESH_MS = 5000
COLUMNS = ('rank', 'mem', 'state', 'what')
HEADINGS = {'rank': '#', 'mem': 'Memory', 'state': 'State', 'what': 'What it is'}
WIDTHS = {'#0': 170, 'rank': 40, 'mem': 80, 'state': 95, 'what': 520}
COMPACT_WIDTHS = {'#0': 140, 'rank': 30, 'mem': 70, 'state': 80, 'what': 260}
BG = '#1a1a1a'
BTN = '#2d2d2d'


def fmt_mem(rss: int) -> str:
    gb = rss / (1024 ** 3)
    return f"{gb:.2f} GB" if gb >= 1 else f"{rss / (1024 ** 2):.0f} MB"


def row_values(r) -> tuple:
    state = 'suspended' if r.suspended else r.reason
    return (r.rank or '', fmt_mem(r.rss), state, r.label)


def row_tags(r) -> tuple:
    if r.suspended:
        return ('suspended',)
    if r.rank == 1:
        return ('next',)
    if r.protected:
        return ('protected',)
    return ()


class HogPanel(tk.Frame):
    def __init__(self, parent, app, compact: bool = False):
        super().__init__(parent, bg=BG)
        self.app = app
        self.compact = compact
        self.rows = []
        self._busy = False
        self._pending = False
        self._timer = None
        self._last_action = ('', '#aaaaaa')
        self.group_var = tk.BooleanVar(value=False)
        self._build_toolbar()
        self._build_tree()
        self.status = tk.Label(self, text="", bg=BG, fg='#aaaaaa', font=('Arial', 8),
                               anchor='w', justify='left')
        self.status.pack(fill='x', pady=(2, 0))
        self.bind('<Destroy>', self._on_destroy)
        self._schedule(50)

    # ── layout ──────────────────────────────────────────────────────────
    def _button(self, bar, text, cmd, color=BTN):
        b = tk.Button(bar, text=text, bg=color, fg='white', relief='flat',
                      font=('Arial', 8), command=cmd)
        b.pack(side='left', padx=(0, 4))
        return b

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill='x', pady=(0, 4))
        # right-hand buttons packed first so a narrow alert squeezes the
        # checkbox, not the Full view button
        tk.Button(bar, text="Full view" if self.compact else "Maximise",
                  bg=BTN, fg='white', relief='flat', font=('Arial', 8),
                  command=self.app.open_hog_panel if self.compact else self._toggle_max
                  ).pack(side='right')
        tk.Button(bar, text="Refresh", bg=BTN, fg='white', relief='flat', font=('Arial', 8),
                  command=self.refresh).pack(side='right', padx=(0, 4))
        self._button(bar, "Suspend", lambda: self._act('suspend'))
        self._button(bar, "Resume", lambda: self._act('resume'))
        self._button(bar, "Kill", lambda: self._act('kill'), '#5a1a1a')
        self.note_btn = self._button(bar, "AI note", self._summarise, '#1a3a5a')
        tk.Checkbutton(bar, text="Group by app", variable=self.group_var,
                       command=lambda: (self._mark_in_use(), self._render()), bg=BG, fg='white', selectcolor=BTN,
                       activebackground=BG, activeforeground='white',
                       font=('Arial', 8)).pack(side='left', padx=(6, 0))

    def _build_tree(self):
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show='tree headings',
                                 height=8 if self.compact else 25, selectmode='browse')
        widths = COMPACT_WIDTHS if self.compact else WIDTHS
        self.tree.heading('#0', text='Process', anchor='w')
        self.tree.column('#0', width=widths['#0'], anchor='w', stretch=False)
        for col in COLUMNS:
            self.tree.heading(col, text=HEADINGS[col], anchor='w')
            self.tree.column(col, width=widths[col], anchor='w', stretch=(col == 'what'))
        self.tree.tag_configure('next', background='#5a2a00')
        self.tree.tag_configure('suspended', foreground='#64b5f6')
        self.tree.tag_configure('protected', foreground='#888888')
        self.tree.tag_configure('group', font=('Arial', 9, 'bold'))
        ys = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side='right', fill='y')
        xs.pack(side='bottom', fill='x')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Button-3>', self._context_menu)
        self.tree.bind('<ButtonPress>', lambda e: self._mark_in_use(), add='+')

    def _toggle_max(self):
        top = self.winfo_toplevel()
        top.state('normal' if top.state() == 'zoomed' else 'zoomed')

    # ── data ────────────────────────────────────────────────────────────
    def set_status(self, text, color='#aaaaaa'):
        if self.winfo_exists():
            self.status.config(text=text, fg=color)

    def _schedule(self, delay=REFRESH_MS):
        if self._timer:
            self.after_cancel(self._timer)
        self._timer = self.after(delay, self.refresh) if self.winfo_exists() else None

    def refresh(self):
        if not self.winfo_exists():
            return
        if self._busy:
            self._pending = True
            return
        self._busy, self._pending = True, False
        if not self.rows:
            self.set_status("Scanning processes...")
        app = self.app
        protected = app.guardian.config.protected_names()
        suspended = set(app.guardian.suspended)

        def _worker():
            try:
                rows = app.hog_source.rows(protected, suspended, os.getpid())
                self.after(0, lambda: self._loaded(rows))
            except Exception as e:
                self.after(0, lambda err=e: self._failed(err))

        threading.Thread(target=_worker, daemon=True, name='ByteDogHogScan').start()

    def _loaded(self, rows):
        self._busy = False
        if not self.winfo_exists():
            return
        self.rows = rows
        self._render()
        top = next((r for r in rows if r.rank == 1), None)
        nxt = f"Next target: {top.name} (PID {top.pid}, {fmt_mem(top.rss)})" if top \
            else "No eligible target (everything left is protected)"
        last, color = self._last_action
        self.set_status(f"{nxt}. The highlighted row is what Suspend Top / Kill Top Hog hit."
                        + (f"\nLast: {last}" if last else ''), color)
        self._schedule(0 if self._pending else REFRESH_MS)

    def _failed(self, err):
        self._busy = False
        self.set_status(f"Scan failed: {err}", '#f44336')
        if self.winfo_exists():
            self._schedule()

    def _mark_in_use(self):
        """Tell the hosting window the user is working in the panel (the warn
        alert stops its auto-close countdown)."""
        self.winfo_toplevel().in_use = True

    def _on_destroy(self, event):
        if event.widget is self and self._timer:
            self.after_cancel(self._timer)
            self._timer = None

    def _render(self):
        selected = self.selected()
        scroll = self.tree.yview()[0]
        open_groups = {iid for iid in self.tree.get_children() if self.tree.item(iid, 'open')}
        self.tree.delete(*self.tree.get_children())
        if self.group_var.get():
            for g in group_rows(self.rows):
                gid = f"g:{g.name}"
                self.tree.insert('', 'end', iid=gid, text=f"{g.name} x{g.count}",
                                 values=(g.best_rank or '', fmt_mem(g.rss), '', ''),
                                 tags=('group',), open=gid in open_groups)
                for r in g.rows:
                    self._insert(gid, r)
        else:
            for r in self.rows:
                self._insert('', r)
        if selected and self.tree.exists(f"p:{selected[0]}"):
            self.tree.selection_set(f"p:{selected[0]}")
        self.tree.yview_moveto(scroll)

    def _insert(self, parent, r):
        lock = '\U0001F512 ' if r.protected else ''
        self.tree.insert(parent, 'end', iid=f"p:{r.pid}", text=f"{lock}{r.name}",
                         values=row_values(r), tags=row_tags(r))

    def selected(self):
        """(pid, name) of the selected process row, or None for none/group."""
        sel = self.tree.selection()
        if not sel or not sel[0].startswith('p:'):
            return None
        pid = int(sel[0][2:])
        row = next((r for r in self.rows if r.pid == pid), None)
        return (pid, row.name) if row else None

    # ── actions ─────────────────────────────────────────────────────────
    def _act(self, action):
        self._mark_in_use()
        target = self.selected()
        if not target:
            self.set_status("Select a single process row first (not an app group).", '#ff9800')
            return
        pid, name = target
        handler = {'suspend': self.app.suspend_pid, 'resume': self.app.resume_pid,
                   'kill': self.app.kill_pid}[action]
        self.set_status(f"{action.title()} {name} (PID {pid})...")
        ok = handler(pid, name, parent=self.winfo_toplevel())
        if ok is None:
            self.set_status(f"{action.title()} cancelled.")
            return
        done = {'suspend': 'Suspended', 'resume': 'Resumed', 'kill': 'Killed'}[action]
        self._last_action = (f"{done} {name} (PID {pid})." if ok
                             else f"Could not {action} {name} (PID {pid}): access denied or gone.",
                             '#4caf50' if ok else '#f44336')
        self.set_status(*self._last_action)
        self.refresh()

    def _summarise(self):
        self._mark_in_use()
        if str(self.note_btn.cget('state')) == 'disabled':
            self.set_status("An AI note is already being generated; wait for it to finish.",
                            '#ff9800')
            return
        target = self.selected()
        if not target or target[1].lower() != 'claude.exe':
            self.set_status("AI note works on claude.exe rows: select a Claude session.",
                            '#ff9800')
            return
        pid, _ = target
        self.note_btn.config(state='disabled')
        self.set_status(f"Reading the session transcript and asking Claude ({SUMMARY_MODEL}) "
                        f"for a note on PID {pid}. Takes about 10s...")

        def _worker():
            try:
                note, err = self.app.hog_source.summarise(pid), None
            except SummaryError as e:
                note, err = None, str(e)
            except Exception as e:
                note, err = None, f"unexpected error: {e}"
            self.after(0, lambda: self._summarised(pid, note, err))

        threading.Thread(target=_worker, daemon=True, name='ByteDogSessionNote').start()

    def _summarised(self, pid, note, err):
        if not self.winfo_exists():
            return
        self.note_btn.config(state='normal')
        self._last_action = ((f"AI note failed for PID {pid}: {err}", '#f44336') if err
                             else (f"AI note for PID {pid}: {note}", '#4caf50'))
        self.set_status(*self._last_action)
        if not err:
            self.refresh()

    def _context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid.startswith('p:'):
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0, bg=BTN, fg='white')
        menu.add_command(label="Suspend", command=lambda: self._act('suspend'))
        menu.add_command(label="Resume", command=lambda: self._act('resume'))
        menu.add_command(label="Kill", command=lambda: self._act('kill'))
        menu.add_separator()
        menu.add_command(label="AI note (Claude sessions)", command=self._summarise)
        tools = getattr(self.app, 'tools', None)
        target = self.selected()
        if tools and target:
            tools.add_menu_items(menu, target[0], target[1], self.winfo_toplevel())
        menu.post(event.x_root, event.y_root)
