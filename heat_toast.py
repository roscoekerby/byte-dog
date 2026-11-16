"""The heat notice: a small topmost window when the machine starts being
throttled. It says how slow the machine is running and what is making the
heat, with Pause / Slow down for that process and "Open ByteDog" for the Heat tab.
"""
from __future__ import annotations

import tkinter as tk

from ui_theme import font

WIDTH, MARGIN = 420, 20
AUTOCLOSE_S = 30


def headline(state) -> str:
    if state.cpu_speed is not None and state.cpu_speed < 100:
        return f"Your PC is being slowed down: CPU at {state.cpu_speed:.0f}% of normal speed"
    return "Your PC is being slowed down by heat"


def culprit_line(rows) -> str:
    if not rows:
        return "Finding what is making the heat..."
    top = rows[0]
    return f"Biggest heat source: {top.name} (PID {top.pid})   CPU {top.cpu:.0f}%   GPU {top.gpu:.0f}%"


def build_heat_toast(app, state, rows, icon_fn) -> tk.Toplevel:
    """`app` provides colors, heat_pause(row), heat_slow(row) and open_heat_tab()."""
    c = app.colors
    toast = tk.Toplevel(app.root)
    toast.in_use = False
    toast.title("ByteDog Heat")
    icon_fn(toast)
    toast.attributes('-topmost', True)
    toast.configure(bg=c['bg'])
    toast.resizable(False, False)

    tk.Frame(toast, bg=c['error'], height=4).pack(fill='x')
    body = tk.Frame(toast, bg=c['bg'])
    body.pack(fill='both', expand=True, padx=12, pady=(8, 4))
    tk.Label(body, text="\U0001F415 Overheating: throttled", bg=c['bg'], fg=c['error'],
             font=font(10, 'bold'), anchor='w').pack(fill='x')
    tk.Label(body, text=headline(state), bg=c['bg'], fg=c['fg'], font=font(9), anchor='w',
             justify='left', wraplength=WIDTH - 24).pack(fill='x')
    tk.Label(body, text="This is why the mouse and typing lag.", bg=c['bg'], fg=c['muted'],
             font=font(9), anchor='w').pack(fill='x')
    toast.culprit = tk.Label(body, text=culprit_line(rows), bg=c['bg'], fg=c['fg'], font=font(9),
                             anchor='w', justify='left', wraplength=WIDTH - 24)
    toast.culprit.pack(fill='x', pady=(4, 0))
    toast.result = tk.Label(body, text="", bg=c['bg'], fg=c['success'], font=font(9, 'bold'),
                            anchor='w', justify='left', wraplength=WIDTH - 24)
    toast.result.pack(fill='x')

    btns = tk.Frame(toast, bg=c['bg'])
    btns.pack(fill='x', padx=12, pady=(0, 8))

    def act(fn):
        def run():
            toast.in_use = True
            top = rows[0] if rows else None
            if top is not None:
                toast.result.config(text=fn(top) or "")
        return run

    tk.Button(btns, text="Open ByteDog", bg=c['accent'], fg=c['on_accent'], font=font(9, 'bold'),
              command=lambda: (app.open_heat_tab(), toast.destroy())).pack(side='left')
    if rows:
        tk.Button(btns, text="Pause it", bg=c['button'], fg=c['fg'], font=font(9),
                  command=act(app.heat_pause)).pack(side='left', padx=(4, 0))
        tk.Button(btns, text="Slow it down", bg=c['button'], fg=c['fg'], font=font(9),
                  command=act(app.heat_slow)).pack(side='left', padx=(4, 0))
    tk.Button(btns, text="Dismiss", bg=c['button'], fg=c['fg'], font=font(9),
              command=toast.destroy).pack(side='right')
    countdown = tk.Label(btns, text="", bg=c['bg'], fg=c['faint'], font=font(8))
    countdown.pack(side='right', padx=4)

    def tick(n):
        if not toast.winfo_exists():
            return
        if toast.in_use:
            countdown.config(text="")
        elif n > 0:
            countdown.config(text=f"{n}s")
            toast.after(1000, lambda: tick(n - 1))
        else:
            toast.destroy()

    toast.update_idletasks()
    toast.geometry(f"{WIDTH}x{toast.winfo_reqheight()}+{toast.winfo_screenwidth() - WIDTH - MARGIN}+{MARGIN * 4}")
    toast.after(1000, lambda: tick(AUTOCLOSE_S - 1))
    return toast
