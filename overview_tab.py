"""Overview tab: what this machine is (cards) and how busy each core is (bars)."""
from __future__ import annotations

import platform
import sys
import time
import tkinter as tk
from typing import Callable

import psutil

from ui_theme import COLORS, font, level_color
from ui_widgets import Card, CoreBars, Stat

TICK_MS = 2000
GB = 1024 ** 3
_CPU_KEY = r'HARDWARE\DESCRIPTION\System\CentralProcessor\0'


def processor_name() -> str:
    """The marketing name ('Intel Core i7-...'), not platform's 'Intel64 Family 6'."""
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY) as key:
                return str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
        except OSError:
            pass
    return platform.processor() or 'Unknown processor'


def fmt_uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


class OverviewTab(tk.Frame):
    def __init__(self, parent: tk.Misc, monitor, gpu_available: bool,
                 is_active: Callable[[], bool] = lambda: True):
        super().__init__(parent, bg=COLORS['bg'])
        self.monitor = monitor
        self.is_active = is_active
        self.gpu_available = gpu_available
        grid = tk.Frame(self, bg=COLORS['bg'])
        grid.pack(fill='x', padx=12, pady=(12, 8))
        self.system = Stat(grid, 'System')
        self.cpu = Stat(grid, 'Processor')
        self.memory = Stat(grid, 'Memory')
        self.gpu = Stat(grid, 'Graphics')
        for i, card in enumerate((self.system, self.cpu, self.memory, self.gpu)):
            card.grid(row=i // 2, column=i % 2, sticky='nsew',
                      padx=(0 if i % 2 == 0 else 8, 0), pady=(0 if i < 2 else 8, 0))
        for col in (0, 1):
            grid.columnconfigure(col, weight=1, uniform='ov')

        physical = psutil.cpu_count(logical=False)
        counts = f"{psutil.cpu_count()} logical" + (f", {physical} physical" if physical else "")
        cores = Card(self, title=f"CPU cores ({counts})")
        cores.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.bars = CoreBars(cores.body, height=140)
        self.bars.pack(fill='both', expand=True)
        self.core_note = tk.Label(cores.body, text='', bg=COLORS['surface'], fg=COLORS['faint'],
                                  font=font(8), anchor='w')
        self.core_note.pack(fill='x', pady=(6, 0))
        self._fill_static()
        self.after(200, self._tick)

    def _fill_static(self) -> None:
        name = processor_name()
        self.cpu.set(_short_cpu(name), name)
        if not self.gpu_available:
            self.gpu.set('None detected', 'No NVIDIA GPU found')

    def update_cores(self, values) -> None:
        values = list(values)
        self.bars.set(values)
        if values:
            busiest = max(range(len(values)), key=values.__getitem__)
            self.core_note.config(text=f"Average {sum(values) / len(values):.0f}%   "
                                       f"Busiest: core {busiest} at {values[busiest]:.0f}%")

    def refresh(self) -> None:
        release = platform.release()
        self.system.set(f"{platform.system()} {release}",
                        f"Up {fmt_uptime(time.time() - psutil.boot_time())}   {platform.node()}")
        mem = self.monitor.get_memory_info()
        self.memory.set(f"{mem['total'] / GB:.0f} GB",
                        f"{mem['used'] / GB:.1f} GB in use ({mem['percent']:.0f}%)   "
                        f"Page file {mem['swap_percent']:.0f}%",
                        color=level_color(mem['percent']))
        if self.gpu_available:
            info = self.monitor.get_gpu_info() or {}
            if info:
                self.gpu.set(f"{info.get('memory_total', 0) / 1024:.0f} GB VRAM",
                             f"{info.get('name', 'GPU')}   {info.get('load', 0):.0f}% busy")

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.is_active():
            try:
                self.refresh()
            except Exception as e:
                self.system.set('--', f"Unavailable: {e}")
        self.after(TICK_MS, self._tick)


def _short_cpu(name: str) -> str:
    """'13th Gen Intel(R) Core(TM) i7-13700H' -> 'Core i7-13700H'."""
    cleaned = name.replace('(R)', '').replace('(TM)', '').replace(' CPU', '')
    for marker in ('Core', 'Ryzen', 'Xeon', 'Celeron', 'Pentium', 'Snapdragon', 'Apple'):
        if marker in cleaned:
            return cleaned[cleaned.index(marker):].split('@')[0].strip()
    return cleaned.split('@')[0].strip()[:28]
