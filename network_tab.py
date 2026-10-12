"""Network tab: live download/upload, a 60-second chart, and the adapters,
connected ones first with the dead 169.254 / disconnected ones tucked away."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk
from typing import Callable

import psutil

import net_model
from net_model import fmt_bytes, fmt_link, fmt_rate
from ui_theme import COLORS, font
from ui_widgets import Card, LineChart, Stat, section

TICK_MS = 1000
ADAPTER_REFRESH_S = 10        # adapter list/addresses change rarely
HISTORY = 60
COLUMNS = (('status', 'Status', 90), ('ipv4', 'IPv4', 120), ('link', 'Link', 80),
           ('down', 'Down now', 90), ('up', 'Up now', 90), ('recv', 'Received', 90),
           ('sent', 'Sent', 90))


def collect(need_adapters: bool) -> tuple:
    """Runs on a worker: one counters read, plus adapter details when due."""
    now = time.monotonic()
    snap = net_model.sample(now, psutil.net_io_counters(), psutil.net_io_counters(pernic=True))
    details = (psutil.net_if_addrs(), psutil.net_if_stats()) if need_adapters else None
    return snap, details


class NetworkTab(tk.Frame):
    def __init__(self, parent: tk.Misc, collector: Callable = collect,
                 is_active: Callable[[], bool] = lambda: True):
        super().__init__(parent, bg=COLORS['bg'])
        self.collector = collector
        self.is_active = is_active
        self.prev: net_model.NetSample | None = None
        self.down_hist: deque = deque(maxlen=HISTORY)
        self.up_hist: deque = deque(maxlen=HISTORY)
        self.addr_stats: tuple | None = None
        self.addr_time = 0.0
        self.busy = False
        self.show_all = tk.BooleanVar(master=self, value=False)
        self._build()
        self.after(200, self._tick)

    # ── layout ──

    def _build(self) -> None:
        stats = tk.Frame(self, bg=COLORS['bg'])
        stats.pack(fill='x', padx=12, pady=(12, 8))
        self.down_stat = Stat(stats, 'Download', color_key='chart_down')
        self.up_stat = Stat(stats, 'Upload', color_key='chart_up')
        self.recv_stat = Stat(stats, 'Received since boot')
        self.sent_stat = Stat(stats, 'Sent since boot')
        for i, card in enumerate((self.down_stat, self.up_stat, self.recv_stat, self.sent_stat)):
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 8, 0))
            stats.columnconfigure(i, weight=1, uniform='stat')

        chart_card = Card(self, title='Throughput, last 60 seconds')
        chart_card.pack(fill='x', padx=12, pady=(0, 8))
        self.chart = LineChart(chart_card.body, series=('chart_down', 'chart_up'),
                               ceiling=None, label=lambda v: fmt_bytes(v), height=130)
        self.chart.pack(fill='x')
        legend = tk.Frame(chart_card.body, bg=COLORS['surface'])
        legend.pack(fill='x', pady=(6, 0))
        for key, text in (('chart_down', 'Download'), ('chart_up', 'Upload')):
            tk.Label(legend, text='●', bg=COLORS['surface'], fg=COLORS[key],
                     font=font(10)).pack(side='left')
            tk.Label(legend, text=text, bg=COLORS['surface'], fg=COLORS['muted'],
                     font=font(8)).pack(side='left', padx=(2, 12))

        head = tk.Frame(self, bg=COLORS['bg'])
        head.pack(fill='x', padx=12, pady=(4, 4))
        section(head, 'Adapters').pack(side='left')
        self.toggle = tk.Checkbutton(head, text='Show disconnected', variable=self.show_all,
                                     command=self._render_adapters, bg=COLORS['bg'],
                                     fg=COLORS['muted'], activeforeground=COLORS['fg'],
                                     font=font(9))
        self.toggle.pack(side='right')

        frame = tk.Frame(self, bg=COLORS['bg'])
        frame.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show='tree headings',
                                 height=6, selectmode='browse')
        self.tree.heading('#0', text='Adapter', anchor='w')
        self.tree.column('#0', width=200, minwidth=120, stretch=True)
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, anchor='w')
            self.tree.column(key, width=width, minwidth=60, stretch=False, anchor='w')
        self.tree.tag_configure('off', foreground=COLORS['faint'])
        self.tree.tag_configure('on', foreground=COLORS['fg'])
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.rows: list = []
        self.rates = net_model.Rates()

    # ── data ──

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        active = self.is_active()
        if not active:
            self.prev = None      # the next rate must span one tick, not the hidden spell
        if not self.busy and active:
            self.busy = True
            due = time.monotonic() - self.addr_time > ADAPTER_REFRESH_S or self.addr_stats is None
            threading.Thread(target=self._work, args=(due,), daemon=True,
                             name='ByteDogNet').start()
        self.after(TICK_MS, self._tick)

    def _work(self, need_adapters: bool) -> None:
        try:
            result = self.collector(need_adapters)
        except Exception as e:       # psutil/OS failure: show it, keep ticking
            result = e
        try:
            self.after(0, lambda: self._apply(result))
        except (RuntimeError, tk.TclError):
            pass                     # app closing

    def _apply(self, result) -> None:
        self.busy = False
        if isinstance(result, Exception):
            self.down_stat.set('--', f"Unavailable: {result}")
            return
        snap, details = result
        self.rates = net_model.rates(self.prev, snap)
        if self.prev is not None:
            self.down_hist.append(self.rates.down)
            self.up_hist.append(self.rates.up)
        self.prev = snap
        if details is not None:
            self.addr_stats = details
            self.addr_time = time.monotonic()
        self.down_stat.set(fmt_rate(self.rates.down), f"Peak {fmt_rate(max(self.down_hist, default=0))}")
        self.up_stat.set(fmt_rate(self.rates.up), f"Peak {fmt_rate(max(self.up_hist, default=0))}")
        self.recv_stat.set(fmt_bytes(snap.recv))
        self.sent_stat.set(fmt_bytes(snap.sent))
        self.chart.set_data(self.down_hist, self.up_hist)
        if self.addr_stats:
            io = {n: _Io(*v) for n, v in snap.per_nic.items()}
            self.rows = net_model.adapters(*self.addr_stats, io)
        self._render_adapters()

    def _render_adapters(self) -> None:
        connected = [a for a in self.rows if a.connected]
        hidden = len(self.rows) - len(connected)
        self.toggle.config(text=f"Show disconnected ({hidden})")
        shown = self.rows if self.show_all.get() else connected
        wanted = [a.name for a in shown]
        current = list(self.tree.get_children())
        if current != wanted:
            self.tree.delete(*current)
            for name in wanted:
                self.tree.insert('', 'end', iid=name, text=name)
        for a in shown:
            up, down = self.rates.per_nic.get(a.name, (0.0, 0.0))
            status = 'Connected' if a.connected else 'Loopback' if a.loopback \
                else 'No network' if a.up else 'Disconnected'
            self.tree.item(a.name, text=a.name, tags=('on' if a.connected else 'off',), values=(
                status, a.ipv4, fmt_link(a.speed_mbps),
                fmt_rate(down) if a.connected else '', fmt_rate(up) if a.connected else '',
                fmt_bytes(a.recv) if a.recv else '', fmt_bytes(a.sent) if a.sent else ''))


class _Io:
    """net_model.adapters reads psutil's per-NIC counter shape."""
    __slots__ = ('bytes_sent', 'bytes_recv')

    def __init__(self, sent: int, recv: int):
        self.bytes_sent, self.bytes_recv = sent, recv
