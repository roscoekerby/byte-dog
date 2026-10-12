"""Performance tab: CPU, memory and GPU history as line charts, each with its
current value, average and peak (replaces the old ASCII graphs)."""
from __future__ import annotations

import tkinter as tk
from typing import Callable

from ui_theme import COLORS, font, heading_font, level_color
from ui_widgets import Card, LineChart

TICK_MS = 1000
GB = 1024 ** 3


class _Panel(Card):
    def __init__(self, parent: tk.Misc, title: str, series_key: str, height: int):
        super().__init__(parent)
        head = tk.Frame(self.body, bg=COLORS['surface'])
        head.pack(fill='x')
        tk.Label(head, text=title, bg=COLORS['surface'], fg=COLORS['fg'],
                 font=heading_font(11)).pack(side='left')
        self.value = tk.Label(head, text='--', bg=COLORS['surface'], fg=COLORS[series_key],
                              font=heading_font(15))
        self.value.pack(side='right')
        self.sub = tk.Label(self.body, text='', bg=COLORS['surface'], fg=COLORS['faint'],
                            font=font(8), anchor='w')
        self.sub.pack(fill='x')
        self.chart = LineChart(self.body, series=(series_key,), height=height)
        self.chart.pack(fill='both', expand=True, pady=(6, 0))

    def show(self, history, detail: str = '', interval_s: float = 2.0) -> None:
        values = [v for v in history if v is not None]
        self.chart.set_data(values)
        if not values:
            self.value.config(text='--')
            self.sub.config(text='Collecting...')
            return
        now = values[-1]
        self.value.config(text=f"{now:.0f}%", fg=level_color(now))
        span = len(values) * interval_s
        window = f"{span / 60:.0f} min" if span >= 60 else f"{span:.0f} s"
        stats = f"Avg {sum(values) / len(values):.0f}%   Peak {max(values):.0f}%   over the last {window}"
        self.sub.config(text=f"{detail}   {stats}" if detail else stats)


class PerformanceTab(tk.Frame):
    def __init__(self, parent: tk.Misc, monitor, gpu_available: bool,
                 is_active: Callable[[], bool] = lambda: True, chart_height: int = 100,
                 sample_s: float = 2.0):
        super().__init__(parent, bg=COLORS['bg'])
        self.monitor = monitor
        self.sample_s = sample_s
        self.is_active = is_active
        self.cpu = _Panel(self, 'CPU', 'chart_cpu', chart_height)
        self.ram = _Panel(self, 'Memory', 'chart_ram', chart_height)
        self.panels = [self.cpu, self.ram]
        self.gpu = None
        if gpu_available:
            self.gpu = _Panel(self, 'GPU', 'chart_gpu', chart_height)
            self.panels.append(self.gpu)
        for i, panel in enumerate(self.panels):
            panel.pack(fill='both', expand=True, padx=12, pady=(12 if i == 0 else 0, 8))
        self.after(200, self._tick)

    def refresh(self) -> None:
        m = self.monitor
        every = self.sample_s
        mem = m.get_memory_info()
        self.cpu.show(m.cpu_history, interval_s=every)
        self.ram.show(m.ram_history, f"{mem['used'] / GB:.1f} of {mem['total'] / GB:.0f} GB in use",
                      interval_s=every)
        if self.gpu is not None:
            info = m.get_gpu_info() or {}
            detail = ''
            if info:
                detail = f"{info.get('name', 'GPU')}   VRAM {info.get('memory_used', 0) / 1024:.1f} " \
                         f"of {info.get('memory_total', 0) / 1024:.0f} GB"
            self.gpu.show(m.gpu_history or [], detail, interval_s=every)

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.is_active():
            try:
                self.refresh()
            except Exception as e:     # a sensor read failing must not stop the loop
                self.cpu.sub.config(text=f"Unavailable: {e}")
        self.after(TICK_MS, self._tick)
