"""Phase 4: the Guardian lives in the main window; the pop-up is a small
notice that names the next target and jumps to the Guardian tab."""
import time

import pytest

from conftest import InlineThread
from hog_panel import HogPanel


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr('bytedog.threading.Thread', InlineThread)


def _pump(dog, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        dog.root.update()
        time.sleep(0.02)


def test_notice_names_the_same_target_as_the_buttons(dog, inline):
    dog.show_guardian_alert({'type': 'suspend', 'ram_pct': 86.0, 'used_gb': 55, 'total_gb': 64})
    try:
        _pump(dog)
        top = dog._select_top_target()
        text = dog.guardian_alert_next.cget('text')
        assert text.startswith('Next target:') and f"PID {top['pid']}" in text
        assert dog.guardian_alert_window.winfo_width() <= 420   # a notice, not a panel
    finally:
        dog.guardian_alert_window.destroy()


def test_open_bytedog_lands_on_guardian_tab(dog, inline):
    dog.set_view_mode('compact')
    dog.show_guardian_alert({'type': 'warn', 'ram_pct': 77.0, 'used_gb': 49, 'total_gb': 64})
    toast = dog.guardian_alert_window
    button = next(w for f in toast.winfo_children() for w in f.winfo_children()
                  if w.winfo_class() == 'Button' and w.cget('text') == 'Open ByteDog')
    button.invoke()
    _pump(dog)
    assert dog.view_mode.get() == 'detailed'
    assert dog._active_detailed_tab_text() == 'Guardian'
    assert not toast.winfo_exists()
    dog.set_view_mode('compact')


def test_guardian_tab_hosts_the_full_hog_panel(dog):
    assert isinstance(dog.guardian_panel, HogPanel)
    assert not dog.guardian_panel.compact
    assert dog.guardian_panel.winfo_toplevel() is dog.root


def test_hidden_panel_does_not_scan(dog, monkeypatch):
    scans = []
    monkeypatch.setattr(dog.hog_source, 'rows', lambda *a, **k: scans.append(1) or [])
    dog.set_view_mode('compact')
    _pump(dog, 0.1)
    dog.guardian_panel._busy = False
    dog.guardian_panel.refresh()
    assert scans == []


def test_notice_ram_bar_is_drawn(dog, inline):
    dog.show_guardian_alert({'type': 'suspend', 'ram_pct': 80.0, 'used_gb': 51, 'total_gb': 64})
    try:
        bar = dog.guardian_alert_window.ram_bar
        x0, y0, x1, y1 = bar.coords(bar.find_all()[0])
        assert x1 == int((380 - 24) * 0.8)
    finally:
        dog.guardian_alert_window.destroy()
