"""Building blocks for the redesigned tabs: cards, stat tiles, line charts and
per-core bars. Colours come from ui_theme.COLORS by key, and canvases redraw
themselves when the theme changes."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Sequence

import ui_theme
from ui_theme import COLORS, font, heading_font, mix, mono

CHART_GRID_STEPS = 4
FILL_STRENGTH = 0.78     # how far a chart's area fill fades into the card colour


class Card(tk.Frame):
    """A raised panel with an optional title; put content in `.body`."""

    def __init__(self, parent: tk.Misc, title: str | None = None, pad: int = 12, **kw):
        super().__init__(parent, bg=COLORS['surface'], highlightthickness=1,
                         highlightbackground=COLORS['border'], **kw)
        if title:
            self.title = tk.Label(self, text=title, bg=COLORS['surface'], fg=COLORS['muted'],
                                  font=font(9), anchor='w')
            self.title.pack(fill='x', padx=pad, pady=(pad - 2, 0))
        self.body = tk.Frame(self, bg=COLORS['surface'])
        self.body.pack(fill='both', expand=True, padx=pad, pady=(4 if title else pad, pad))


class Stat(Card):
    """A headline number with a caption: 'Download  1.2 MB/s'."""

    def __init__(self, parent: tk.Misc, title: str, color_key: str = 'fg', **kw):
        super().__init__(parent, title=title, **kw)
        self._color_key = color_key
        self.value = tk.Label(self.body, text='--', bg=COLORS['surface'],
                              fg=COLORS[color_key], font=heading_font(16), anchor='w')
        self.value.pack(fill='x')
        self.sub = tk.Label(self.body, text='', bg=COLORS['surface'], fg=COLORS['faint'],
                            font=font(8), anchor='w')
        self.sub.pack(fill='x')

    def set(self, value: str, sub: str | None = None, color: str | None = None) -> None:
        self.value.config(text=value, fg=color or COLORS[self._color_key])
        if sub is not None:
            self.sub.config(text=sub)


def section(parent: tk.Misc, text: str, bg_key: str = 'bg') -> tk.Label:
    """A small heading above a group of widgets."""
    return tk.Label(parent, text=text, bg=COLORS[bg_key], fg=COLORS['fg'],
                    font=heading_font(10), anchor='w')


def button(parent: tk.Misc, text: str, command: Callable, role: str = 'neutral',
           **kw) -> tk.Button:
    """Flat button in one of three roles: neutral, primary (accent) or danger."""
    bg, fg = {'neutral': ('button', 'fg'), 'primary': ('accent', 'on_accent'),
              'danger': ('danger_bg', 'error')}[role]
    return tk.Button(parent, text=text, command=command, bg=COLORS[bg], fg=COLORS[fg],
                     activebackground=COLORS[ui_theme.HOVER[bg]], activeforeground=COLORS[fg],
                     font=font(9), **kw)


class _ThemedCanvas(tk.Canvas):
    def __init__(self, parent: tk.Misc, bg_key: str = 'surface', **kw):
        super().__init__(parent, bg=COLORS[bg_key], highlightthickness=0, **kw)
        self._bg_key = bg_key
        self.bind('<Configure>', lambda e: self.redraw())
        ui_theme.on_change(self._themed)
        self.bind('<Destroy>', lambda e: ui_theme.off_change(self._themed)
                  if e.widget is self else None, add='+')

    def _themed(self) -> None:
        if self.winfo_exists():
            self.config(bg=COLORS[self._bg_key])
            self.redraw()

    def redraw(self) -> None:
        raise NotImplementedError


class LineChart(_ThemedCanvas):
    """Filled line chart for a few series sharing one axis.
    `ceiling` fixes the top (100 for percentages); None scales to the data."""

    def __init__(self, parent: tk.Misc, series: Sequence[str], ceiling: float | None = 100,
                 label: Callable[[float], str] = lambda v: f"{v:.0f}%", points: int = 60,
                 height: int = 120, **kw):
        super().__init__(parent, height=height, **kw)
        self.series = list(series)            # COLORS keys, e.g. 'chart_cpu'
        self.ceiling = ceiling
        self.label = label
        self.points = points
        self.data: list = [[] for _ in self.series]

    def set_data(self, *columns: Sequence[float]) -> None:
        self.data = [[v for v in col if v is not None][-self.points:] for col in columns]
        self.redraw()

    def _top(self) -> float:
        if self.ceiling is not None:
            return self.ceiling
        peak = max((max(col) for col in self.data if col), default=0)
        return nice_ceiling(peak)

    def redraw(self) -> None:
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 30:
            return
        left, top, bottom = 44, 6, h - 6
        top_val = self._top()
        for i in range(CHART_GRID_STEPS + 1):
            y = top + (bottom - top) * i / CHART_GRID_STEPS
            self.create_line(left, y, w, y, fill=COLORS['grid'])
            value = top_val * (1 - i / CHART_GRID_STEPS)
            self.create_text(left - 6, y, text=self.label(value), anchor='e',
                             fill=COLORS['faint'], font=font(7))
        step = (w - left) / max(self.points - 1, 1)
        for key, col in zip(self.series, self.data):
            if len(col) < 2:
                continue
            x0 = w - step * (len(col) - 1)
            pts = []
            for i, v in enumerate(col):
                pts += [x0 + i * step, bottom - (bottom - top) * min(v / top_val, 1)]
            colour = COLORS[key]
            self.create_polygon([pts[0], bottom, *pts, pts[-2], bottom],
                                fill=mix(colour, COLORS['surface'], FILL_STRENGTH), outline='')
            self.create_line(*pts, fill=colour, width=2, smooth=False)


def nice_ceiling(value: float) -> float:
    """Round a peak up to 1, 2 or 5 x 10^n so axis labels stay readable."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1) if value >= 1 else 1
    for m in (1, 2, 5, 10):
        if value <= m * magnitude:
            return float(m * magnitude)
    return float(10 * magnitude)


class CoreBars(_ThemedCanvas):
    """One vertical bar per logical core, coloured by load."""

    def __init__(self, parent: tk.Misc, height: int = 110, **kw):
        super().__init__(parent, height=height, **kw)
        self.values: list = []

    def set(self, values: Sequence[float]) -> None:
        self.values = list(values)
        self.redraw()

    def redraw(self) -> None:
        self.delete('all')
        n = len(self.values)
        w, h = self.winfo_width(), self.winfo_height()
        if not n or w < 40:
            return
        gap = 4
        bar_w = max((w - gap * (n - 1)) / n, 2)
        bottom = h - 16
        for i, v in enumerate(self.values):
            x = i * (bar_w + gap)
            self.create_rectangle(x, 2, x + bar_w, bottom, fill=COLORS['button'], outline='')
            y = bottom - (bottom - 2) * min(max(v, 0), 100) / 100
            self.create_rectangle(x, y, x + bar_w, bottom,
                                  fill=ui_theme.level_color(v), outline='')
            self.create_text(x + bar_w / 2, h - 7, text=str(i), fill=COLORS['faint'],
                             font=mono(7))
