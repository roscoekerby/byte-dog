"""Process Explorer-style tools for ByteDog's UI: the advanced right-click
items (kill tree, restart, priority, affinity, file location, open files /
DLLs, Open in Process Explorer), the details pane, and "Who is locking this
file?". Every destructive action goes through ByteDog's verified safeguards
(app._verdict + the protected-process override dialog).
"""
from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import psutil

import proc_actions
import sysinternals
from file_locks import files_and_dlls, who_locks

BG, FG, BTN = '#1e1e1e', '#ffffff', '#2d2d2d'
DETAIL_FIELDS = (('path', 'Image'), ('cmdline', 'Command line'), ('cwd', 'Working folder'),
                 ('parent', 'Parent'), ('user', 'User'), ('started', 'Started'),
                 ('priority', 'Priority'), ('threads', 'Threads / handles'),
                 ('elevated', 'Admin'), ('signer', 'Verified signer'),
                 ('protection', 'Protection'))


class ProcessTools:
    def __init__(self, app):
        self.app = app

    # ── helpers ─────────────────────────────────────────────────────────
    def _status(self, msg, ok=True):
        self.app.report_status(msg, ok)

    def _post(self, fn):
        """Run fn on the Tk thread from a worker; harmless if ByteDog closed."""
        try:
            self.app.root.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _in_background(self, name, work, done):
        """work() on a worker thread, then done(result, error) on the Tk thread."""
        def _run():
            try:
                result, err = work(), None
            except Exception as e:
                result, err = None, e
            self._post(lambda: done(result, err))
        threading.Thread(target=_run, daemon=True, name=name).start()

    def _allowed(self, pid, name, action, parent) -> bool:
        """Protected processes need the explicit override dialog."""
        if self.app._is_protected(name, pid):
            return self.app._show_protected_override_dialog(name, pid, action, parent)
        return True

    def _same(self, pid, name) -> bool:
        if self.app._same_process(pid, name):
            return True
        self._status(f"PID {pid} is no longer {name}; refresh and try again", ok=False)
        return False

    # ── menu ────────────────────────────────────────────────────────────
    def add_menu_items(self, menu, pid, name, parent):
        """Append the advanced items for one process to a context menu."""
        menu.add_separator()
        menu.add_command(label="Kill Process Tree",
                         command=lambda: self.kill_tree(pid, name, parent))
        menu.add_command(label="Restart", command=lambda: self.restart(pid, name, parent))
        prio = tk.Menu(menu, tearoff=0, bg=BTN, fg=FG)
        for label in proc_actions.PRIORITIES:
            prio.add_command(label=label,
                             command=lambda lb=label: self.set_priority(pid, name, lb, parent))
        menu.add_cascade(label="Set Priority", menu=prio)
        menu.add_command(label="Set Affinity...",
                         command=lambda: self.affinity_dialog(pid, name, parent))
        menu.add_separator()
        menu.add_command(label="Open File Location", command=lambda: self.open_location(pid))
        menu.add_command(label="Copy Path", command=lambda: self.copy(pid, 'path'))
        menu.add_command(label="Copy Command Line", command=lambda: self.copy(pid, 'cmdline'))
        menu.add_command(label="Open Files && DLLs...",
                         command=lambda: self.files_window(pid, name, parent))
        menu.add_command(label="Open in Process Explorer",
                         command=lambda: self.open_in_procexp(pid, parent))

    # ── actions ─────────────────────────────────────────────────────────
    def kill_tree(self, pid, name, parent):
        if not self._same(pid, name) or not self._allowed(pid, name, 'kill', parent):
            return
        count = len(proc_actions.tree_pids(pid))
        if not messagebox.askyesno("Kill Process Tree",
                                   f"Kill '{name}' (PID {pid}) and its {count - 1} "
                                   f"descendant process(es)?\nProtected descendants are skipped.",
                                   parent=parent):
            return

        def is_protected(child_pid):
            try:
                child_name = psutil.Process(child_pid).name()
            except psutil.NoSuchProcess:
                return False
            except psutil.Error:
                return True     # cannot verify -> leave it alone
            return self.app._is_protected(child_name, child_pid)

        self._status(f"Killing {name}'s process tree...")
        self._in_background('ByteDogKillTree',
                            lambda: proc_actions.kill_tree(pid, is_protected),
                            lambda r, err: self._tree_killed(pid, name, r, err))

    def _tree_killed(self, pid, name, r, err):
        if err:
            self._status(f"Kill tree of {name} failed: {err}", ok=False)
            return
        for p in r.killed:
            self.app.guardian.suspended.pop(p, None)
        self.app.guardian.log_event('action', f"Killed tree of {name} (PID {pid}): "
                                              f"{len(r.killed)} killed")
        extra = (f", {len(r.skipped)} protected skipped" if r.skipped else '') + \
                (f", {len(r.failed)} failed (access denied)" if r.failed else '')
        self._status(f"Killed {len(r.killed)} process(es) in {name}'s tree{extra}",
                     ok=not r.failed)

    def restart(self, pid, name, parent):
        if not self._same(pid, name):
            return
        if self.app._is_protected(name, pid):
            messagebox.showwarning("Restart", f"'{name}' is protected; ByteDog won't restart "
                                              f"it.\n\n{self.app._protection_text(pid, name)}",
                                   parent=parent)
            return
        if not messagebox.askyesno("Restart", f"Close '{name}' (PID {pid}) and start it again "
                                              f"with the same command line?\nUnsaved work in "
                                              f"it will be lost.", parent=parent):
            return
        self._status(f"Restarting {name}...")
        self._in_background('ByteDogRestart', lambda: proc_actions.restart(pid),
                            lambda new_pid, err: self._restarted(pid, name, new_pid, err))

    def _restarted(self, pid, name, new_pid, err):
        if err:
            self._status(f"Restart of {name} failed: {err}", ok=False)
            return
        self.app.guardian.suspended.pop(pid, None)
        self.app.guardian.log_event('action', f"Restarted {name}: PID {pid} -> {new_pid}")
        self._status(f"Restarted {name}: new PID {new_pid}")

    def set_priority(self, pid, name, label, parent):
        if not self._same(pid, name) or not self._allowed(pid, name, 'change priority of',
                                                          parent):
            return
        try:
            proc_actions.set_priority(pid, label)
            self._status(f"{name} (PID {pid}) priority set to {label}")
        except (psutil.Error, ValueError) as e:
            self._status(f"Could not set priority: {e}", ok=False)

    def affinity_dialog(self, pid, name, parent):
        if not self._same(pid, name):
            return
        try:
            current = set(psutil.Process(pid).cpu_affinity())
        except psutil.Error as e:
            self._status(f"Could not read affinity: {e}", ok=False)
            return
        win = tk.Toplevel(parent)
        win.title(f"CPU affinity: {name} ({pid})")
        win.configure(bg=BG)
        win.transient(parent)
        win.attributes('-topmost', True)
        cpus = list(range(psutil.cpu_count() or 1))
        vars_ = [tk.BooleanVar(value=c in current) for c in cpus]
        grid = tk.Frame(win, bg=BG)
        grid.pack(padx=12, pady=10)
        for c, v in zip(cpus, vars_):
            tk.Checkbutton(grid, text=f"CPU {c}", variable=v, bg=BG, fg=FG, selectcolor=BTN,
                           activebackground=BG).grid(row=c // 4, column=c % 4, sticky='w')

        def apply():
            chosen = [c for c, v in zip(cpus, vars_) if v.get()]
            if not self._allowed(pid, name, 'change affinity of', win):
                return
            try:
                proc_actions.set_affinity(pid, chosen)
                self._status(f"{name} (PID {pid}) limited to CPUs {chosen}")
                win.destroy()
            except (psutil.Error, ValueError) as e:
                messagebox.showerror("Affinity", str(e), parent=win)

        tk.Button(win, text="Apply", bg=BTN, fg=FG, command=apply).pack(pady=(0, 10))

    def open_location(self, pid):
        d = proc_actions.details(pid)
        if not d or not d['path']:
            self._status("Image path unavailable (access denied or process gone)", ok=False)
            return
        subprocess.Popen(['explorer.exe', '/select,', d['path']], close_fds=True)

    def copy(self, pid, field):
        d = proc_actions.details(pid)
        value = (d or {}).get(field) or ''
        if not value:
            self._status(f"{field} unavailable (access denied or process gone)", ok=False)
            return
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(value)
        self._status(f"Copied {field} of PID {pid}")

    def files_window(self, pid, name, parent):
        self._status(f"Reading open files and DLLs of {name}...")
        self._in_background('ByteDogFiles', lambda: files_and_dlls(pid),
                            lambda r, err: self._show_files(pid, name, parent, r, err))

    def _show_files(self, pid, name, parent, result, err):
        if err:
            self._status(f"Could not read open files of {name}: {err}", ok=False)
            return
        files, dlls = result
        self._status(f"{name}: {len(files)} open files, {len(dlls)} modules")
        win = tk.Toplevel(parent)
        win.title(f"Open files & DLLs: {name} ({pid})")
        win.configure(bg=BG)
        win.geometry("900x560")
        nb = ttk.Notebook(win)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        for title, items in ((f"Files ({len(files)})", files), (f"DLLs ({len(dlls)})", dlls)):
            frame = tk.Frame(nb, bg=BG)
            lb = tk.Listbox(frame, bg=BTN, fg=FG, font=('Consolas', 9), activestyle='none')
            sb = ttk.Scrollbar(frame, orient='vertical', command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            lb.pack(fill='both', expand=True)
            for item in items:
                lb.insert('end', item)
            nb.add(frame, text=title)

    def open_in_procexp(self, pid, parent):
        exe = sysinternals.find_procexp()
        if exe:
            self._launch_procexp(exe, pid)
            return
        if not messagebox.askyesno(
                "Process Explorer",
                "Process Explorer (Microsoft Sysinternals) isn't installed.\n\n"
                "Download it from download.sysinternals.com (about 2 MB) into "
                f"{sysinternals.LOCAL_DIR}?\nByteDog checks it is signed by Microsoft "
                "before running it.", parent=parent):
            return
        self._status("Downloading Process Explorer from Microsoft...")

        self._in_background('ByteDogProcexpFetch', sysinternals.download_procexp,
                            lambda exe2, err: self._downloaded(exe2, err, pid))

    def _downloaded(self, exe, err, pid):
        if err:
            self._status(f"Process Explorer download failed: {err}", ok=False)
            return
        self._status(f"Process Explorer verified (Microsoft-signed) at {exe}")
        self._launch_procexp(exe, pid)

    def _launch_procexp(self, exe, pid):
        try:
            sysinternals.open_in_procexp(exe, pid)
            self._status(f"Opened Process Explorer on PID {pid}")
        except sysinternals.ToolError as e:
            self._status(str(e), ok=False)

    # ── who is locking a file ───────────────────────────────────────────
    def who_locks_dialog(self, parent=None):
        parent = parent or self.app.root
        paths = filedialog.askopenfilenames(parent=parent,
                                            title="Which process is locking this file?")
        if not paths:
            return
        try:
            holders = who_locks([os.path.normpath(p) for p in paths])
        except OSError as e:
            messagebox.showerror("Who is locking this file?", str(e), parent=parent)
            return
        win = tk.Toplevel(parent)
        win.title("Who is locking this file?")
        win.configure(bg=BG)
        win.geometry("640x320")
        tk.Label(win, text="\n".join(paths), bg=BG, fg='#aaaaaa', font=('Consolas', 8),
                 justify='left', anchor='w').pack(fill='x', padx=10, pady=(10, 4))
        if not holders:
            tk.Label(win, text="No process has this file open.", bg=BG, fg='#4caf50',
                     font=('Arial', 10)).pack(padx=10, pady=10, anchor='w')
            return
        tree = ttk.Treeview(win, columns=('pid', 'app'), show='tree headings', height=8)
        tree.heading('#0', text='Process')
        tree.heading('pid', text='PID')
        tree.heading('app', text='App / service')
        tree.column('pid', width=70)
        apps = {}
        for h in holders:     # several services in one svchost share a PID
            apps.setdefault(h.pid, (h.name, []))[1].append(h.app)
        for hpid, (hname, names) in apps.items():
            tree.insert('', 'end', iid=str(hpid), text=hname,
                        values=(hpid, ', '.join(n for n in names if n)))
        tree.pack(fill='both', expand=True, padx=10)

        def kill_selected():
            sel = tree.selection()
            if sel:
                pid = int(sel[0])
                ok = self.app.kill_pid(pid, tree.item(sel[0], 'text'), parent=win)
                if ok:
                    tree.delete(sel[0])

        tk.Button(win, text="Kill selected (safeguarded)", bg='#5a1a1a', fg=FG,
                  command=kill_selected).pack(pady=8)


class DetailsPane(tk.Frame):
    """Process Explorer-style lower pane: everything about the selected PID.
    Loads on a worker thread; protection comes from the app's verified check."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self._shown = None
        self.values = {}
        for row, (key, label) in enumerate(DETAIL_FIELDS):
            tk.Label(self, text=label, bg=BG, fg='#888888', font=('Arial', 8),
                     anchor='e').grid(row=row // 2, column=(row % 2) * 2, sticky='e', padx=(8, 4))
            v = tk.Label(self, text='', bg=BG, fg=FG, font=('Consolas', 8), anchor='w',
                         justify='left', wraplength=420)
            v.grid(row=row // 2, column=(row % 2) * 2 + 1, sticky='w')
            self.values[key] = v
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

    def show(self, pid, name):
        self._shown = pid

        def _worker():
            d = proc_actions.details(pid)
            verdict = self.app._verdict(pid, name)
            try:
                self.after(0, lambda: self._fill(pid, d, verdict))
            except (RuntimeError, tk.TclError):
                pass        # pane closed while loading

        threading.Thread(target=_worker, daemon=True, name='ByteDogDetails').start()

    def _fill(self, pid, d, verdict):
        if pid != self._shown or not self.winfo_exists():
            return
        if not d:
            for v in self.values.values():
                v.config(text='')
            self.values['path'].config(text='(process has exited)')
            return
        shown = {**d,
                 'parent': f"{d['parent_name']} ({d['parent_pid']})" if d['parent_pid'] else '',
                 'threads': f"{d['threads']} / {d['handles']}",
                 'elevated': {True: 'yes', False: 'no', None: '?'}[d['elevated']],
                 'signer': verdict.signer or 'unsigned / unverified',
                 'protection': verdict.reason or 'not protected'}
        for key, label in self.values.items():
            label.config(text=str(shown.get(key, '') or ''))
        self.values['protection'].config(fg='#ff9800' if verdict.protected else '#4caf50')
