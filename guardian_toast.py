"""The Guardian's pop-up: a small topmost notice in ByteDog's style. The full
controls (hog panel, thresholds, log) live in the main window's Guardian tab;
the notice shows RAM, the next target and the action taken, with quick
rescue buttons and "Open ByteDog" to jump to that tab.
"""
from __future__ import annotations

import tkinter as tk

from ui_theme import font

HEADERS = {     # tier -> (theme colour key, title)
    'warn': ('warning', 'Memory pressure'),
    'suspend': ('error', 'Suspending top hog'),
    'kill': ('error', 'Killing top hog'),
}
WIDTH, HEIGHT, MARGIN = 380, 196, 20
WARN_AUTOCLOSE_S = 15


def build_toast(app, event, icon_fn) -> tk.Toplevel:
    """Create the notice for a guardian event. Sets app.guardian_alert_action
    and app.guardian_alert_next (labels the app updates later)."""
    c = app.colors
    tier = event['type']
    toast = tk.Toplevel(app.root)
    toast.tier = tier
    toast.in_use = False
    toast.title("ByteDog Guardian")
    icon_fn(toast)
    toast.attributes('-topmost', True)
    toast.configure(bg=c['bg'])
    toast.resizable(False, False)
    screen_w = toast.winfo_screenwidth()
    toast.geometry(f"{WIDTH}x{HEIGHT}+{screen_w - WIDTH - MARGIN}+{MARGIN * 4}")

    color_key, title = HEADERS.get(tier, HEADERS['warn'])
    color = c[color_key]
    tk.Frame(toast, bg=color, height=4).pack(fill='x')
    body = tk.Frame(toast, bg=c['bg'])
    body.pack(fill='both', expand=True, padx=12, pady=(8, 4))
    ram_pct = event['ram_pct']
    tk.Label(body, text=f"\U0001F415 {title}", bg=c['bg'], fg=color,
             font=font(10, 'bold'), anchor='w').pack(fill='x')
    tk.Label(body, text=f"RAM {ram_pct:.1f}%  ({event.get('used_gb', 0):.1f} / "
                        f"{event.get('total_gb', 0):.0f} GB)",
             bg=c['bg'], fg=c['fg'], font=font(9), anchor='w').pack(fill='x')
    bar_w = WIDTH - 24     # fixed: winfo_width() is 1 until the window is mapped
    bar = tk.Canvas(body, width=bar_w, height=6, bg=c['button'], highlightthickness=0)
    bar.pack(anchor='w', pady=(3, 4))
    bar.create_rectangle(0, 0, int(bar_w * min(ram_pct, 100) / 100), 6, fill=color, outline='')
    toast.ram_bar = bar
    app.guardian_alert_next = tk.Label(body, text="Finding the top hog...", bg=c['bg'],
                                       fg=c['muted'], font=font(9), anchor='w',
                                       justify='left', wraplength=WIDTH - 24)
    app.guardian_alert_next.pack(fill='x')
    app.guardian_alert_action = tk.Label(body, text="", bg=c['bg'], fg=c['success'],
                                         font=font(9, 'bold'), anchor='w',
                                         justify='left', wraplength=WIDTH - 24)
    app.guardian_alert_action.pack(fill='x')

    btns = tk.Frame(toast, bg=c['bg'])
    btns.pack(fill='x', padx=12, pady=(0, 8))

    def use(cmd):
        return lambda: (setattr(toast, 'in_use', True), cmd())

    tk.Button(btns, text="Open ByteDog", bg=c['accent'], fg=c['on_accent'],
              font=font(9, 'bold'),
              command=lambda: (app.open_guardian_tab(), toast.destroy())).pack(side='left')
    for label, cmd in (("Suspend Top", app._suspend_top_hog_now),
                       ("Resume All", app._resume_all_suspended)):
        tk.Button(btns, text=label, bg=c['button'], fg=c['fg'],
                  font=font(9), command=use(cmd)).pack(side='left', padx=(4, 0))
    tk.Button(btns, text="Dismiss", bg=c['button'], fg=c['fg'],
              font=font(9), command=toast.destroy).pack(side='right')
    countdown = tk.Label(btns, text="", bg=c['bg'], fg=c['faint'], font=font(8))
    countdown.pack(side='right', padx=4)

    if tier == 'warn':
        _start_countdown(toast, countdown, WARN_AUTOCLOSE_S)
    return toast


def _start_countdown(toast, label, seconds):
    def tick(n):
        if not toast.winfo_exists():
            return
        if toast.in_use:
            label.config(text="Kept open while you use it")
        elif n > 0:
            label.config(text=f"{n}s")
            toast.after(1000, lambda: tick(n - 1))
        else:
            toast.destroy()
    toast.after(1000, lambda: tick(seconds - 1))
