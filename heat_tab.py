"""Heat tab: is the machine being throttled, how slow is it running, what is
making the heat (with Pause / Resume / Slow down / Close per process), and how
much throttling there has been lately, including before and after a cooling
change such as a new cooling pad.

The app supplies `thermal` (a ThermalWatch), `guardian.suspended` and the
heat_pause / heat_resume / heat_slow / heat_close actions.
"""
from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Callable

from thermal_model import (
    AUTO_OFF, AUTO_PAUSE, AUTO_SLOW, DAY_S, OK, THROTTLED, WARM, cooling_comparison, fmt_duration,
    gpu_reason_names, summarise,
)
from ui_theme import COLORS, font
from ui_widgets import Card, LineChart, Stat, button

TICK_MS = 2000
SUB_WRAP = 185
HINT_WRAP = 640
HISTORY_EVERY_TICKS = 15
COLUMNS = ('pid', 'cpu', 'gpu', 'state')
HEADINGS = {'pid': 'PID', 'cpu': 'CPU', 'gpu': 'GPU', 'state': 'State'}
WIDTHS = {'#0': 240, 'pid': 70, 'cpu': 70, 'gpu': 70, 'state': 110}
STATUS = {OK: ('Normal', 'success'), WARM: ('Warm', 'warning'), THROTTLED: ('THROTTLED', 'error')}
AUTO_LABELS = {AUTO_OFF: 'Do nothing', AUTO_SLOW: 'Lower its priority', AUTO_PAUSE: 'Pause it'}


def status_text(state) -> tuple:
    """``(headline, detail, colour key)`` for the status card."""
    label, colour = STATUS.get(state.level, STATUS[OK])
    if state.level == OK:
        return label, 'No throttling', colour
    if state.level == THROTTLED and not state.limited_now:
        return 'Recovering', 'Back to normal speed; checking it stays there', 'warning'
    return label, '; '.join(state.reasons) or 'Heat is building up', colour


def speed_note(sample, config) -> str:
    """Why the clock is where it is: an idle CPU runs slow on purpose."""
    if sample.cpu_busy is None:
        return 'of normal speed'
    if sample.cpu_speed < 90 and sample.cpu_busy < config.cpu_busy_min_pct:
        return f"of normal speed   {sample.cpu_busy:.0f}% busy, so it is saving power"
    return f"of normal speed   {sample.cpu_busy:.0f}% busy"


def summary_line(label: str, s) -> str:
    if not s.count:
        return f"{label}: no throttling"
    worst = f", slowest {s.worst_speed:.0f}% of normal" if s.worst_speed is not None else ''
    cause = f", mostly {s.usual_cause}" if s.usual_cause else ''
    return f"{label}: throttled {fmt_duration(s.seconds)} over {s.count} spell(s){worst}{cause}"


def history_lines(episodes: list, markers: list, now: float) -> list:
    lines = [summary_line('Last 24 hours', summarise(episodes, now - DAY_S, now)),
             summary_line('Last 7 days', summarise(episodes, now - 7 * DAY_S, now))]
    compared = cooling_comparison(episodes, markers, now)
    if compared:
        note, before, after = compared
        if after is None:
            lines.append(f"Since \"{note}\": the before and after comparison appears after a day")
            return lines
        lines.append(f"Since \"{note}\": {fmt_duration(after.per_day_s)} a day throttled, "
                     f"against {fmt_duration(before.per_day_s)} a day before")
    return lines


class HeatTab(tk.Frame):
    def __init__(self, parent: tk.Misc, app, is_active: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.time):
        super().__init__(parent, bg=COLORS['bg'])
        self.app = app
        self.watch = app.thermal
        self.is_active = is_active
        self._clock = clock
        self._ticks = 0
        self._rows = []
        self._build_status()
        self._build_chart()
        self._build_sources()
        self._build_history()
        self._build_settings()
        self.bind('<Destroy>', lambda e: setattr(self.watch, 'want_rows', False) if e.widget is self else None)
        self.after(200, self._tick)

    # ── layout ──────────────────────────────────────────────────────────
    def _build_status(self) -> None:
        row = tk.Frame(self, bg=COLORS['bg'])
        row.pack(fill='x', padx=12, pady=(12, 8))
        self.status = Stat(row, 'Status', 'success')
        self.speed = Stat(row, 'CPU speed', 'chart_cpu')
        self.gpu = Stat(row, 'GPU', 'chart_gpu')
        for i, card in enumerate((self.status, self.speed, self.gpu)):
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 8, 0))
            row.columnconfigure(i, weight=1, uniform='heat')
        for card in (self.status, self.speed, self.gpu):
            card.sub.config(wraplength=SUB_WRAP, justify='left')

    def _build_chart(self) -> None:
        card = Card(self, title='CPU speed, % of normal (a busy CPU far below 100 is being throttled)')
        card.pack(fill='x', padx=12, pady=(0, 8))
        self.chart = LineChart(card.body, series=('chart_cpu',), height=56)
        self.chart.pack(fill='x')

    def _build_sources(self) -> None:
        card = Card(self, title='What is making the heat')
        card.pack(fill='x', padx=12, pady=(0, 8))
        bar = tk.Frame(card.body, bg=COLORS['surface'])
        bar.pack(fill='x', pady=(0, 4))
        for text, action, role in (('Pause', 'pause', 'primary'), ('Resume', 'resume', 'neutral'),
                                   ('Slow down', 'slow', 'neutral'), ('Close', 'close', 'danger')):
            button(bar, text, lambda a=action: self._act(a), role).pack(side='left', padx=(0, 4))
        self.result = tk.Label(bar, text='', bg=COLORS['surface'], fg=COLORS['muted'], font=font(9), anchor='w')
        self.result.pack(side='left', padx=(8, 0))
        self.tree = ttk.Treeview(card.body, columns=COLUMNS, show='tree headings', height=4,
                                 selectmode='browse')
        self.tree.heading('#0', text='Process', anchor='w')
        self.tree.column('#0', width=WIDTHS['#0'], anchor='w')
        for col in COLUMNS:
            self.tree.heading(col, text=HEADINGS[col], anchor='w')
            self.tree.column(col, width=WIDTHS[col], anchor='w', stretch=False)
        self.tree.tag_configure('paused', foreground=COLORS['accent'])
        self.tree.pack(fill='x')
        self.hint = tk.Label(card.body, text='Pause freezes a process in place (nothing is lost) so the PC can '
                                             'cool; Resume carries on. Slow down gives it half the cores and a lower priority.',
                             bg=COLORS['surface'], fg=COLORS['faint'], font=font(8), anchor='w', justify='left',
                             wraplength=HINT_WRAP)
        self.hint.pack(fill='x', pady=(4, 0))

    def _build_history(self) -> None:
        card = Card(self, title='Throttling history')
        card.pack(fill='x', padx=12, pady=(0, 8))
        self.history = tk.Label(card.body, text='', bg=COLORS['surface'], fg=COLORS['fg'], font=font(9),
                                anchor='w', justify='left', wraplength=HINT_WRAP - 160)
        self.history.pack(side='left', fill='x', expand=True)
        button(card.body, 'Mark cooling change', self._mark_cooling).pack(side='right')

    def _build_settings(self) -> None:
        row = tk.Frame(self, bg=COLORS['bg'])
        row.pack(fill='x', padx=12, pady=(0, 10))
        cfg = self.watch.config
        self.alerts_var = tk.BooleanVar(value=cfg.alerts)
        tk.Checkbutton(row, text='Alert me when throttling starts', variable=self.alerts_var,
                       bg=COLORS['bg'], fg=COLORS['fg'], activebackground=COLORS['bg'],
                       activeforeground=COLORS['fg'], selectcolor=COLORS['button'], font=font(9),
                       command=self._save_settings).pack(side='left')
        tk.Label(row, text='If still throttled after', bg=COLORS['bg'], fg=COLORS['fg'],
                 font=font(9)).pack(side='left', padx=(16, 4))
        self.after_var = tk.StringVar(value=f"{cfg.auto_after_s:.0f}")
        entry = ttk.Entry(row, textvariable=self.after_var, width=5)
        entry.pack(side='left')
        entry.bind('<FocusOut>', lambda e: self._save_settings())
        entry.bind('<Return>', lambda e: self._save_settings())
        tk.Label(row, text='s, for the top heat source:', bg=COLORS['bg'], fg=COLORS['fg'],
                 font=font(9)).pack(side='left', padx=(4, 4))
        self.auto_var = tk.StringVar(value=AUTO_LABELS.get(cfg.auto_action, AUTO_LABELS[AUTO_OFF]))
        box = ttk.Combobox(row, textvariable=self.auto_var, values=list(AUTO_LABELS.values()),
                           state='readonly', width=18)
        box.pack(side='left')
        box.bind('<<ComboboxSelected>>', lambda e: self._save_settings())

    # ── behaviour ───────────────────────────────────────────────────────
    def _save_settings(self) -> None:
        cfg = self.watch.config
        cfg.alerts = bool(self.alerts_var.get())
        cfg.auto_action = next((k for k, v in AUTO_LABELS.items() if v == self.auto_var.get()), AUTO_OFF)
        try:
            seconds = float(self.after_var.get())
            if math.isfinite(seconds):
                cfg.auto_after_s = min(max(seconds, 10.0), 3600.0)
        except ValueError:
            pass
        self.after_var.set(f"{cfg.auto_after_s:.0f}")
        self.watch.save_config()

    def _selected(self):
        picked = self.tree.selection()
        if not picked:
            return self._rows[0] if self._rows else None
        return next((r for r in self._rows if str(r.pid) == picked[0]), None)

    def _act(self, action: str) -> None:
        row = self._selected()
        if row is None:
            self.result.config(text='Nothing to act on yet', fg=COLORS['muted'])
            return
        run = {'pause': self.app.heat_pause, 'resume': self.app.heat_resume,
               'slow': self.app.heat_slow, 'close': self.app.heat_close}[action]
        message = run(row) or ''
        self.result.config(text=message, fg=COLORS['fg'])
        self.refresh()

    def _mark_cooling(self) -> None:
        note = simpledialog.askstring(
            'Mark cooling change', 'What changed? (for example: cooling pad, cleaned fans, repaste)',
            parent=self.winfo_toplevel())
        if note and note.strip():
            self.watch.log.add_marker(note.strip(), self._clock())
            self._show_history()

    def _show_history(self) -> None:
        episodes, markers = self.watch.log.read()
        self.history.config(text='\n'.join(history_lines(episodes, markers, self._clock())))

    def _show_rows(self) -> None:
        rows = self.watch.heat_rows()
        suspended = dict(self.app.guardian.suspended)     # a copy: other threads change it
        paused = set(suspended)
        keep = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            is_paused = r.pid in paused
            self.tree.insert('', 'end', iid=str(r.pid), text=r.name,
                             values=(r.pid, f"{r.cpu:.1f}%", f"{r.gpu:.0f}%", 'paused' if is_paused else ''),
                             tags=('paused',) if is_paused else ())
        for pid, name in suspended.items():
            if not self.tree.exists(str(pid)):
                self.tree.insert('', 'end', iid=str(pid), text=name, values=(pid, '', '', 'paused'),
                                 tags=('paused',))
                rows = rows + [type('Paused', (), {'pid': pid, 'name': name, 'cpu': 0.0, 'gpu': 0.0})()]
        self._rows = rows
        if keep and self.tree.exists(keep[0]):
            self.tree.selection_set(keep[0])
            self.tree.see(keep[0])

    def refresh(self) -> None:
        state, sample = self.watch.state, self.watch.last_sample
        headline, detail, colour = status_text(state)
        self.status.set(headline, detail, COLORS[colour])
        if sample.cpu_speed is None:
            self.speed.set('--', 'Clock speed is not reported on this PC')
        else:
            slow = state.level == THROTTLED
            self.speed.set(f"{sample.cpu_speed:.0f}%", speed_note(sample, self.watch.config),
                           COLORS['error'] if slow else None)
        if sample.gpu_temp is None:
            self.gpu.set('--', 'No GPU sensor')
        else:
            power = f"{sample.gpu_power_w:.0f} W   " if sample.gpu_power_w is not None else ''
            limits = ', '.join(gpu_reason_names(sample.gpu_reasons)) or 'no limits active'
            self.gpu.set(f"{sample.gpu_temp:.0f} °C", f"{power}{limits}")
        self.chart.set_data([min(v, 100.0) for v in self.watch.speed_points() if v is not None])
        self._show_rows()
        if self._ticks % HISTORY_EVERY_TICKS == 0:
            self._show_history()
        self._ticks += 1

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        active = self.is_active()
        self.watch.want_rows = active
        if active:
            try:
                self.refresh()
            except Exception as e:      # a sensor read failing must not stop the loop
                self.result.config(text=f"Unavailable: {e}")
        self.after(TICK_MS, self._tick)
