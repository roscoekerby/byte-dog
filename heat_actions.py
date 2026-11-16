"""What ByteDog does about heat: the notice, the Heat tab's Pause / Resume /
Slow down / Close, and the optional automatic relief. Mixed into ByteDogApp,
which supplies thermal (ThermalWatch), guardian, process_manager, the *_pid
actions and the protection check.

Pause is a suspend: the process is frozen in place and loses nothing. Slow
down gives the process half the processor cores and a lower priority, which
cuts the heat a CPU-bound job makes; both are undone by Resume.
"""
from __future__ import annotations

import os

import psutil

from heat_toast import build_heat_toast
from proc_actions import PRIORITIES
from thermal_model import AUTO_PAUSE, AUTO_SLOW, fmt_duration

SLOW_PRIORITY = 'Below normal'


def slow_cores(current: list) -> list:
    """The later half of a process's cores (on hybrid CPUs these are the efficient ones)."""
    cores = sorted(current)
    return cores[len(cores) // 2:] if len(cores) > 1 else cores


class HeatActions:
    def _heat_setup(self, icon_fn) -> None:
        self.heat_alert_window = None
        self._heat_icon = icon_fn
        # pid -> (create_time, nice, affinity) from before Slow down. The create
        # time ties the record to that one process, never to a reused PID.
        self._heat_slowed = {}

    # ── events from the watch ───────────────────────────────────────────
    def handle_heat_event(self, event) -> None:
        kind = event[0]
        if kind == 'start':
            _, state, rows = event
            self.guardian.log_event('warn', f"Throttled: {'; '.join(state.reasons)}")
            if self.thermal.config.alerts:
                self.show_heat_alert(state, rows)
        elif kind == 'end':
            self.guardian.log_event('info', f"Throttling ended after {fmt_duration(event[1].seconds)}")
        elif kind == 'auto':
            message = self._heat_auto(event[1], event[2])
            window = self.heat_alert_window
            if message and window is not None and window.winfo_exists():
                window.in_use = True
                window.result.config(text=message)

    def show_heat_alert(self, state, rows) -> None:
        existing = self.heat_alert_window
        if existing is not None and existing.winfo_exists():
            return
        self.heat_alert_window = build_heat_toast(self, state, rows, self._heat_icon)

    def open_heat_tab(self) -> None:
        if self.view_mode.get() != 'detailed':
            self.set_view_mode('detailed')
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        tab = getattr(self, 'heat_tab_frame', None)
        if tab is not None and hasattr(self, 'detailed_notebook'):
            self.detailed_notebook.select(tab)

    # ── actions on one heat source ──────────────────────────────────────
    def heat_pause(self, row, parent=None) -> str:
        ok = self.suspend_pid(row.pid, row.name, parent)
        if ok is None:
            return "Cancelled"
        if not ok:
            return f"Could not pause {row.name} (it may have closed, or need ByteDog run as administrator)"
        return f"Paused {row.name} (PID {row.pid}). Nothing is lost; Resume carries on."

    def heat_resume(self, row, parent=None) -> str:
        restored = self._restore_speed(row.pid)
        if row.pid in self.guardian.suspended:
            if not self.resume_pid(row.pid, row.name, parent):
                return f"Could not resume {row.name}"
            return f"Resumed {row.name} (PID {row.pid})"
        if restored is None:
            return f"{row.name} was not paused or slowed"
        return f"{row.name} is back to full speed" if restored else f"Could not restore {row.name}; try Resume again"

    def heat_slow(self, row, parent=None) -> str:
        if self._is_protected(row.name, row.pid):
            return f"{row.name} is protected, so it was left alone"
        if not self._same_process(row.pid, row.name):
            return f"{row.name} has already closed"
        return self._slow(row.pid, row.name)

    def heat_close(self, row, parent=None) -> str:
        ok = self.kill_pid(row.pid, row.name, parent)
        if ok is None:
            return "Cancelled"
        return f"Closed {row.name} (PID {row.pid})" if ok else f"Could not close {row.name}"

    def _slow(self, pid: int, name: str) -> str:
        try:
            proc = psutil.Process(pid)
            started = proc.create_time()
            saved = self._heat_slowed.get(pid)
            if saved is None or saved[0] != started:
                saved = (started, proc.nice(), proc.cpu_affinity())
            cores = slow_cores(saved[2])
        except (psutil.Error, OSError, ValueError) as e:
            return f"Could not slow {name}: {e}"
        try:
            proc.cpu_affinity(cores)
            proc.nice(PRIORITIES[SLOW_PRIORITY])
        except (psutil.Error, OSError, ValueError) as e:
            try:                              # do not leave it half-slowed
                proc.cpu_affinity(saved[2])
                proc.nice(saved[1])
            except (psutil.Error, OSError, ValueError):
                self._heat_slowed[pid] = saved    # keep the record so Resume can retry
            return f"Could not slow {name}: {e}"
        self._heat_slowed[pid] = saved
        self.guardian.log_event('action', f"Slowed {name} (PID {pid}) to {len(cores)} cores")
        return f"Slowed {name}: {len(cores)} cores and lower priority. Resume undoes it."

    def _restore_speed(self, pid: int) -> bool | None:
        """True restored, False could not (record kept for a retry), None nothing to restore."""
        saved = self._heat_slowed.get(pid)
        if saved is None:
            return None
        try:
            proc = psutil.Process(pid)
            if proc.create_time() != saved[0]:    # the PID now belongs to another process
                del self._heat_slowed[pid]
                return None
            proc.cpu_affinity(saved[2])
            proc.nice(saved[1])
        except psutil.NoSuchProcess:
            del self._heat_slowed[pid]
            return None
        except (psutil.Error, OSError, ValueError):
            return False
        del self._heat_slowed[pid]
        return True

    # ── optional automatic relief ───────────────────────────────────────
    def _heat_auto(self, action: str, rows) -> str:
        """Act on the biggest heat source that is safe to touch. Never asks, so it
        never overrides protection; everything it does is undone by Resume."""
        skip = set(self.guardian.suspended) | {os.getpid()}
        if action == AUTO_SLOW:
            skip |= set(self._heat_slowed)
        for row in rows:
            if row.pid in skip or self._is_protected(row.name, row.pid):
                continue
            if not self._same_process(row.pid, row.name):
                continue
            if action == AUTO_PAUSE:
                if not self.process_manager.suspend_process(row.pid):
                    continue
                self.guardian.suspended[row.pid] = row.name
                message = f"Paused {row.name} (PID {row.pid}) automatically to cool down"
            else:
                message = "Automatic: " + self._slow(row.pid, row.name)
            self.guardian.log_event('action', message)
            return message
        message = "Automatic relief found nothing safe to act on"
        self.guardian.log_event('warn', message)
        return message
