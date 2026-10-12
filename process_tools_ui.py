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
from ui_theme import COLORS as C, font, mono
BULK_LIST_MAX = 15
VERBS = {'suspend': ('Suspend', 'Suspended'), 'resume': ('Resume', 'Resumed'),
         'kill': ('Kill', 'Killed')}
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
        menu.add_command(label="Suspend Process Tree",
                         command=lambda: self.suspend_tree(pid, name, parent))
        menu.add_command(label="Resume Process Tree",
                         command=lambda: self.resume_tree(pid, name, parent))
        menu.add_command(label="Restart", command=lambda: self.restart(pid, name, parent))
        prio = tk.Menu(menu, tearoff=0)
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

        self._status(f"Killing {name}'s process tree...")
        self._in_background('ByteDogKillTree',
                            lambda: proc_actions.kill_tree(pid, self._child_is_protected),
                            lambda r, err: self._tree_killed(pid, name, r, err))

    def _tree_killed(self, pid, name, r, err):
        if err:
            self._status(f"Kill tree of {name} failed: {err}", ok=False)
            return
        for p in r.killed:
            self.app.guardian.suspended.pop(p, None)
        self.app.guardian.log_event('action', f"Killed tree of {name} (PID {pid}): "
                                              f"{len(r.killed)} killed")
        self._status(f"Killed {len(r.killed)} process(es) in {name}'s tree"
                     + _skips(r.skipped, r.failed), ok=not r.failed)
        self.app.update_alert_hogs()

    def _child_is_protected(self, child_pid):
        try:
            child_name = psutil.Process(child_pid).name()
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return True     # cannot verify -> leave it alone
        return self.app._is_protected(child_name, child_pid)

    def suspend_tree(self, pid, name, parent):
        """Pause a process and all its descendants (protected ones skipped);
        every paused process is tracked so Resume All undoes it."""
        if not self._same(pid, name) or not self._allowed(pid, name, 'suspend', parent):
            return
        self._status(f"Pausing {name}'s process tree...")
        self._in_background('ByteDogSuspendTree',
                            lambda: proc_actions.suspend_tree(pid, self._child_is_protected),
                            lambda r, err: self._tree_paused(pid, name, r, err))

    def _tree_paused(self, pid, name, r, err):
        if err:
            self._status(f"Pausing {name}'s tree failed: {err}", ok=False)
            return
        for p in r.done:
            self.app.guardian.suspended[p] = _name_of(p, name)
        self.app.guardian.log_event('action', f"Suspended tree of {name} (PID {pid}): "
                                              f"{len(r.done)} paused")
        self._status(f"Paused {len(r.done)} process(es) in {name}'s tree"
                     + _skips(r.skipped, r.failed) + ". Resume All undoes it", ok=not r.failed)
        self.app.update_alert_hogs()

    def resume_tree(self, pid, name, parent):
        self._status(f"Resuming {name}'s process tree...")
        self._in_background('ByteDogResumeTree', lambda: proc_actions.resume_tree(pid),
                            lambda r, err: self._tree_resumed(pid, name, r, err))

    def _tree_resumed(self, pid, name, r, err):
        if err:
            self._status(f"Resuming {name}'s tree failed: {err}", ok=False)
            return
        for p in r.done:
            self.app.guardian.suspended.pop(p, None)
        self.app.guardian.log_event('action', f"Resumed tree of {name} (PID {pid})")
        self._status(f"Resumed {len(r.done)} process(es) in {name}'s tree"
                     + _skips([], r.failed), ok=not r.failed)
        self.app.update_alert_hogs()

    # ── many processes at once ──────────────────────────────────────────
    def bulk(self, action, targets, parent):
        """Suspend / resume / kill several selected processes: one decision
        about protected ones, one confirmation, then a worker with progress."""
        verb, _ = VERBS[action]
        targets = list(dict.fromkeys(targets))
        live = [(p, n) for p, n in targets if self.app._same_process(p, n)]
        gone = len(targets) - len(live)
        if action != 'resume':
            prot = [(p, n) for p, n in live if self.app._is_protected(n, p)]
            if prot:
                choice = messagebox.askyesnocancel(
                    f"{verb} {len(live)} processes",
                    f"{len(prot)} of the {len(live)} selected are protected:\n\n"
                    + _listing(prot) + "\n\nYes: include them anyway (override)\n"
                    "No: skip them\nCancel: do nothing", icon='warning', parent=parent)
                if choice is None:
                    return
                if choice is False:
                    live = [t for t in live if t not in prot]
        if not live:
            self._status(f"Nothing to {action}" + (f" ({gone} already gone)" if gone else ''))
            return
        if action == 'kill' and not messagebox.askyesno(
                f"Kill {len(live)} processes", f"Kill these {len(live)} processes?\n\n"
                + _listing(live) + "\n\nUnsaved work in them will be lost.", parent=parent):
            return
        self._status(f"{verb}ing {len(live)} processes...")
        self._in_background(f'ByteDogBulk{verb}', lambda: self._bulk_work(action, live),
                            lambda r, err: self._bulk_done(action, r, err, gone))

    def _bulk_work(self, action, targets):
        """Worker thread. Returns (ok, failed) lists of (pid, name)."""
        ok, failed = [], []
        pm, suspended = self.app.process_manager, self.app.guardian.suspended
        for i, (pid, name) in enumerate(targets, 1):
            self._post(lambda i=i, n=name: self._status(
                f"{VERBS[action][0]} {i}/{len(targets)}: {n}"))
            if action == 'suspend':
                success = pm.suspend_process(pid)
                if success:
                    suspended[pid] = name
            elif action == 'resume':
                success = pm.resume_process(pid)
                if success or not psutil.pid_exists(pid):
                    suspended.pop(pid, None)
            else:
                success = pm.kill_process(pid)
                if success:
                    suspended.pop(pid, None)
            (ok if success else failed).append((pid, name))
        return ok, failed

    def _bulk_done(self, action, result, err, gone):
        verb, done_verb = VERBS[action]
        if err:
            self._status(f"{verb} failed: {err}", ok=False)
            return
        ok, failed = result
        self.app.guardian.log_event('action', f"Bulk {action}: {len(ok)} done, "
                                              f"{len(failed)} failed")
        extra = (f", {len(failed)} failed (access denied)" if failed else '') + \
                (f", {gone} already gone" if gone else '')
        self._status(f"{done_verb} {len(ok)} process(es){extra}", ok=not failed)
        self.app.update_alert_hogs()

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
        win.configure(bg=C['bg'])
        win.transient(parent)
        win.attributes('-topmost', True)
        cpus = list(range(psutil.cpu_count() or 1))
        vars_ = [tk.BooleanVar(value=c in current) for c in cpus]
        grid = tk.Frame(win, bg=C['bg'])
        grid.pack(padx=12, pady=10)
        for c, v in zip(cpus, vars_):
            tk.Checkbutton(grid, text=f"CPU {c}", variable=v, bg=C['bg'], fg=C['fg'],
                           activeforeground=C['fg']).grid(row=c // 4, column=c % 4, sticky='w')

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

        tk.Button(win, text="Apply", bg=C['accent'], fg=C['on_accent'], command=apply).pack(pady=(0, 10))

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
        win.configure(bg=C['bg'])
        win.geometry("900x560")
        nb = ttk.Notebook(win)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        for title, items in ((f"Files ({len(files)})", files), (f"DLLs ({len(dlls)})", dlls)):
            frame = tk.Frame(nb, bg=C['bg'])
            lb = tk.Listbox(frame, bg=C['surface'], fg=C['fg'], font=mono(9), activestyle='none',
                            selectbackground=C['select'], selectforeground=C['fg'])
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
        win.configure(bg=C['bg'])
        win.geometry("640x320")
        tk.Label(win, text="\n".join(paths), bg=C['bg'], fg=C['muted'], font=mono(8),
                 justify='left', anchor='w').pack(fill='x', padx=10, pady=(10, 4))
        if not holders:
            tk.Label(win, text="No process has this file open.", bg=C['bg'], fg=C['success'],
                     font=font(10)).pack(padx=10, pady=10, anchor='w')
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

        tk.Button(win, text="Kill selected (safeguarded)", bg=C['danger_bg'], fg=C['error'],
                  command=kill_selected).pack(pady=8)


def _name_of(pid, fallback):
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return fallback


def _skips(skipped, failed) -> str:
    return (f", {len(skipped)} protected skipped" if skipped else '') + \
           (f", {len(failed)} failed (access denied)" if failed else '')


def _listing(targets) -> str:
    lines = [f"  {n} (PID {p})" for p, n in targets[:BULK_LIST_MAX]]
    if len(targets) > BULK_LIST_MAX:
        lines.append(f"  ...and {len(targets) - BULK_LIST_MAX} more")
    return "\n".join(lines)


class DetailsPane(tk.Frame):
    """Process Explorer-style lower pane: everything about the selected PID.
    Loads on a worker thread; protection comes from the app's verified check."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=C['surface'], highlightthickness=1,
                         highlightbackground=C['border'], padx=8, pady=6)
        self.app = app
        self._shown = None
        self.values = {}
        for row, (key, label) in enumerate(DETAIL_FIELDS):
            tk.Label(self, text=label, bg=C['surface'], fg=C['muted'], font=font(8),
                     anchor='e').grid(row=row // 2, column=(row % 2) * 2, sticky='e', padx=(8, 4))
            v = tk.Label(self, text='', bg=C['surface'], fg=C['fg'], font=mono(8), anchor='w',
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
        self.values['protection'].config(fg=C['warning'] if verdict.protected else C['success'])
