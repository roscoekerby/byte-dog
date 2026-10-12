"""One look for the whole app: light and dark palettes, fonts, Tk defaults,
button hover, and a live theme switch that re-colours every existing widget.

Widgets take colours from the shared COLORS dict when they are built. A switch
maps each old palette value to the new one (palettes are checked so that this
mapping is unambiguous), re-colours the widget tree, then tells listeners
(charts) to redraw."""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from tkinter import ttk
from types import MappingProxyType
from typing import Callable

UI_FONT = 'Segoe UI'
UI_FONT_SEMIBOLD = 'Segoe UI Semibold'
MONO_FONT = 'Cascadia Mono'
THEMES = ('system', 'light', 'dark')

DARK = MappingProxyType({
    'bg': '#1b1d23', 'surface': '#22252d', 'button': '#2b2f38', 'hover': '#363b46',
    'border': '#353a45', 'select': '#2d4a7a', 'fg': '#e6e8eb', 'muted': '#9ba3af',
    'faint': '#6b7280', 'accent': '#4c8dff', 'accent_hover': '#6aa0ff',
    'on_accent': '#ffffff', 'success': '#3fb950', 'warning': '#e3a008',
    'error': '#f85149', 'danger_bg': '#5b2326', 'danger_hover': '#6e2a2e',
    'warn_bg': '#4a3511', 'info_bg': '#1e3a5f', 'grid': '#2a2e36',
    'chart_cpu': '#4c8dff', 'chart_ram': '#a371f7', 'chart_gpu': '#3fb950',
    'chart_down': '#39c5cf', 'chart_up': '#f0883e',
})

LIGHT = MappingProxyType({
    'bg': '#f4f5f7', 'surface': '#ffffff', 'button': '#e9ebef', 'hover': '#dcdfe5',
    'border': '#d5d9e0', 'select': '#cfe1ff', 'fg': '#1f2328', 'muted': '#5b6470',
    'faint': '#8b939e', 'accent': '#0a66d8', 'accent_hover': '#0857b8',
    'on_accent': '#fffffe', 'success': '#1a7f37', 'warning': '#b36b00',
    'error': '#cf222e', 'danger_bg': '#ffe0de', 'danger_hover': '#ffcdc9',
    'warn_bg': '#fff3cd', 'info_bg': '#e3efff', 'grid': '#eceef2',
    'chart_cpu': '#0a66d8', 'chart_ram': '#8250df', 'chart_gpu': '#1a7f37',
    'chart_down': '#1b7c83', 'chart_up': '#c4510a',
})

# The live palette every widget builder reads. Mutated in place on a switch,
# so modules holding a reference always see the current theme.
COLORS: dict = dict(DARK)

# Background -> hover background for buttons.
HOVER = {'button': 'hover', 'accent': 'accent_hover', 'danger_bg': 'danger_hover'}

_COLOR_OPTIONS = ('background', 'foreground', 'activebackground', 'activeforeground',
                  'selectcolor', 'troughcolor', 'insertbackground', 'highlightbackground',
                  'highlightcolor', 'selectbackground', 'selectforeground',
                  'disabledforeground', 'readonlybackground', 'disabledbackground')
_TAG_OPTIONS = ('foreground', 'background')
_listeners: list = []
_PERSONALIZE_KEY = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'


def font(size: int = 9, weight: str = 'normal') -> tuple:
    return (UI_FONT, size, weight) if weight != 'normal' else (UI_FONT, size)


def heading_font(size: int = 11) -> tuple:
    return (UI_FONT_SEMIBOLD, size)


def mono(size: int = 9, weight: str = 'normal') -> tuple:
    return (MONO_FONT, size, weight) if weight != 'normal' else (MONO_FONT, size)


def _read_apps_use_light_theme() -> int | None:
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            return int(winreg.QueryValueEx(key, 'AppsUseLightTheme')[0])
    except OSError:
        return None


def windows_prefers_dark(reader: Callable[[], int | None] = _read_apps_use_light_theme) -> bool:
    """Windows' 'app mode' setting; dark when it cannot be read."""
    return reader() != 1


def resolve(choice: str, prefers_dark: Callable[[], bool] = windows_prefers_dark):
    """The palette for a View > Theme choice ('system', 'light' or 'dark')."""
    if choice not in THEMES:
        raise ValueError(f"Unknown theme {choice!r}; expected one of {THEMES}")
    if choice == 'system':
        return DARK if prefers_dark() else LIGHT
    return DARK if choice == 'dark' else LIGHT


def is_dark(palette=None) -> bool:
    return (palette or COLORS)['bg'] == DARK['bg']


def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a towards b by t (0 = a, 1 = b). Tk has no alpha, so
    chart fills use a colour mixed into the background instead."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def level_color(pct: float, colors=None) -> str:
    c = colors or COLORS
    return c['success'] if pct < 50 else c['warning'] if pct < 80 else c['error']


def value_map(old, new) -> dict:
    """old palette value -> new palette value, lower-cased keys.
    Raises if one old value would need two different new values."""
    mapping: dict = {}
    for key, value in old.items():
        target = new[key]
        prev = mapping.setdefault(value.lower(), target)
        if prev != target:
            raise ValueError(f"Palette value {value} is ambiguous ({key})")
    return mapping


def on_change(callback: Callable[[], None]) -> None:
    """Call back after every theme switch (charts redraw from COLORS)."""
    _listeners.append(callback)


def off_change(callback: Callable[[], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


# ── Tk wiring ──

def install(root: tk.Tk) -> None:
    """Defaults for widgets built from now on, plus hover for every tk.Button.
    Call before building any widget."""
    _set_option_defaults(root)
    root.bind_class('Button', '<Enter>', _hover_on, add='+')
    root.bind_class('Button', '<Leave>', _hover_off, add='+')
    root.bind_class('Toplevel', '<Map>', _style_new_toplevel, add='+')
    apply_ttk(root)
    set_titlebar(root)


def _set_option_defaults(root: tk.Misc) -> None:
    c = COLORS
    for pattern, value in (
            ('*Font', f'{{{UI_FONT}}} 9'),
            ('*Background', c['bg']), ('*Foreground', c['fg']),
            ('*Button.Background', c['button']), ('*Button.relief', 'flat'),
            ('*Button.borderWidth', 0), ('*Button.padX', 10), ('*Button.padY', 3),
            ('*Button.cursor', 'hand2'), ('*Button.activeBackground', c['hover']),
            ('*Button.activeForeground', c['fg']), ('*Button.highlightThickness', 0),
            ('*Entry.relief', 'flat'), ('*Entry.Background', c['surface']),
            ('*Entry.highlightThickness', 1), ('*Entry.highlightBackground', c['border']),
            ('*Entry.highlightColor', c['accent']), ('*Entry.insertBackground', c['fg']),
            ('*Text.relief', 'flat'), ('*Text.Background', c['surface']),
            ('*Text.highlightThickness', 0), ('*Text.padX', 8), ('*Text.padY', 6),
            ('*Text.insertBackground', c['fg']), ('*Text.selectBackground', c['select']),
            ('*Text.selectForeground', c['fg']), ('*Listbox.selectBackground', c['select']),
            ('*Listbox.selectForeground', c['fg']),
            ('*Listbox.relief', 'flat'), ('*Listbox.Background', c['surface']),
            ('*Listbox.highlightThickness', 0),
            ('*Checkbutton.selectColor', c['surface']), ('*Radiobutton.selectColor', c['surface']),
            ('*Checkbutton.activeBackground', c['bg']), ('*Radiobutton.activeBackground', c['bg']),
            ('*Checkbutton.highlightThickness', 0), ('*Radiobutton.highlightThickness', 0),
            ('*Scale.troughColor', c['button']), ('*Scale.highlightThickness', 0),
            ('*Scale.sliderRelief', 'flat'), ('*Scale.borderWidth', 0),
            ('*Canvas.highlightThickness', 0),
            ('*Menu.Background', c['surface']), ('*Menu.activeBackground', c['select']),
            ('*Menu.activeForeground', c['fg']), ('*Menu.relief', 'flat'),
    ):
        root.option_add(pattern, value)


def _hover_on(event) -> None:
    w = event.widget
    try:
        bg = w.cget('background')
        for base, hover in HOVER.items():
            if bg == COLORS[base] and str(w.cget('state')) != 'disabled':
                w._rest_bg = bg
                w.configure(background=COLORS[hover])
                return
    except tk.TclError:
        pass


def _hover_off(event) -> None:
    w = event.widget
    rest = getattr(w, '_rest_bg', None)
    if rest is None:
        return
    try:
        # A theme switch mid-hover leaves an old value; snap it to the new palette.
        back = {COLORS[h]: COLORS[b] for b, h in HOVER.items()}
        w.configure(background=back.get(w.cget('background'), rest))
    except tk.TclError:
        pass
    w._rest_bg = None


def _style_new_toplevel(event) -> None:
    if isinstance(event.widget, tk.Toplevel) and not getattr(event.widget, '_titled', False):
        event.widget._titled = True
        set_titlebar(event.widget)


def apply_ttk(root: tk.Misc) -> None:
    c = COLORS
    style = ttk.Style(root)
    style.theme_use('clam')
    base = dict(background=c['bg'], foreground=c['fg'], font=font(9))
    style.configure('.', bordercolor=c['border'], lightcolor=c['bg'], darkcolor=c['bg'],
                    troughcolor=c['bg'], focuscolor=c['accent'], **base)
    style.configure('TFrame', background=c['bg'])
    style.configure('Card.TFrame', background=c['surface'])
    style.configure('TLabel', **base)
    style.configure('TButton', background=c['button'], foreground=c['fg'], relief='flat',
                    padding=(10, 3), borderwidth=0)
    style.map('TButton', background=[('active', c['hover'])])
    style.configure('TNotebook', background=c['bg'], borderwidth=0, tabmargins=(0, 4, 0, 0))
    style.configure('TNotebook.Tab', background=c['bg'], foreground=c['muted'],
                    padding=(12, 6), borderwidth=0, font=font(9))
    style.map('TNotebook.Tab',
              background=[('selected', c['surface']), ('active', c['button'])],
              foreground=[('selected', c['fg']), ('active', c['fg'])],
              lightcolor=[('selected', c['accent'])])
    style.configure('Treeview', background=c['surface'], fieldbackground=c['surface'],
                    foreground=c['fg'], rowheight=24, borderwidth=0, font=font(9))
    style.map('Treeview', background=[('selected', c['select'])],
              foreground=[('selected', c['fg'])])
    style.configure('Treeview.Heading', background=c['bg'], foreground=c['muted'],
                    relief='flat', borderwidth=0, padding=(6, 4), font=font(9, 'bold'))
    style.map('Treeview.Heading', background=[('active', c['button'])])
    for orient in ('Vertical', 'Horizontal'):
        style.configure(f'{orient}.TScrollbar', background=c['button'], troughcolor=c['bg'],
                        bordercolor=c['bg'], arrowcolor=c['muted'], relief='flat',
                        lightcolor=c['button'], darkcolor=c['button'], arrowsize=12)
        style.map(f'{orient}.TScrollbar', background=[('active', c['hover'])])
    style.configure('TCheckbutton', **base, indicatorbackground=c['surface'])
    style.configure('TEntry', fieldbackground=c['surface'], foreground=c['fg'],
                    insertcolor=c['fg'])
    style.configure('TCombobox', fieldbackground=c['surface'], foreground=c['fg'],
                    background=c['button'], arrowcolor=c['muted'])
    style.configure('TProgressbar', background=c['accent'], troughcolor=c['button'],
                    bordercolor=c['button'], lightcolor=c['accent'], darkcolor=c['accent'])
    style.configure('Horizontal.TScale', background=c['accent'], troughcolor=c['button'],
                    bordercolor=c['bg'], lightcolor=c['accent'], darkcolor=c['accent'],
                    sliderthickness=14, troughrelief='flat')
    style.map('Horizontal.TScale', background=[('active', c['accent_hover'])])
    style.configure('TPanedwindow', background=c['bg'])
    style.configure('Sash', sashthickness=6, gripcount=0, background=c['bg'])


def set_titlebar(win: tk.Misc) -> None:
    """Dark or light native title bar to match the palette (Windows 10 2004+)."""
    if sys.platform != 'win32':
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if is_dark() else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), 20, ctypes.byref(value), ctypes.sizeof(value))
    except (OSError, AttributeError, tk.TclError):
        pass


def switch(root: tk.Tk, palette) -> bool:
    """Re-colour the running app to `palette`. Returns False if already there."""
    if dict(palette) == COLORS:
        return False
    mapping = value_map(COLORS, palette)
    COLORS.clear()
    COLORS.update(palette)
    _set_option_defaults(root)
    apply_ttk(root)
    _recolor_tree(root, mapping)
    for callback in list(_listeners):
        try:
            callback()
        except tk.TclError:
            pass
    return True


def _recolor_tree(widget: tk.Misc, mapping: dict) -> None:
    _recolor_widget(widget, mapping)
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        set_titlebar(widget)
    for child in widget.winfo_children():
        _recolor_tree(child, mapping)


def _recolor_widget(w: tk.Misc, mapping: dict) -> None:
    options = ('foreground', 'background') if isinstance(w, ttk.Widget) else _COLOR_OPTIONS
    for option in options:
        _swap(lambda o=option: w.cget(o), lambda v, o=option: w.configure(**{o: v}), mapping)
    if isinstance(w, tk.Text):
        for tag in w.tag_names():
            for option in _TAG_OPTIONS:
                _swap(lambda: w.tag_cget(tag, option),
                      lambda v: w.tag_configure(tag, **{option: v}), mapping)
    elif isinstance(w, ttk.Treeview):
        for tag in w.tk.splitlist(w.tk.call(w, 'tag', 'names')):
            for option in _TAG_OPTIONS:
                _swap(lambda: w.tag_configure(tag, option),
                      lambda v: w.tag_configure(tag, **{option: v}), mapping)
    elif isinstance(w, tk.Canvas):
        for item in w.find_all():
            for option in ('fill', 'outline'):
                _swap(lambda: w.itemcget(item, option),
                      lambda v: w.itemconfigure(item, **{option: v}), mapping)


def _swap(get, put, mapping: dict) -> None:
    try:
        value = get()
    except tk.TclError:
        return
    new = mapping.get(str(value).lower()) if value else None
    if new and new != value:
        try:
            put(new)
        except tk.TclError:
            pass
