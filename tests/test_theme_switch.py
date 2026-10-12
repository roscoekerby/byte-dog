"""Switching theme at runtime re-colours what is already on screen, the
choice is saved, and the redesigned tabs render real data."""
import socket
from types import SimpleNamespace as NS

import ui_prefs
import ui_theme
from conftest import InlineThread
from net_model import NetSample
from ui_theme import DARK, LIGHT


def _choose(dog, theme):
    dog.theme_choice.set(theme)
    dog.apply_theme_choice()
    dog.root.update_idletasks()


def test_switch_recolours_live_widgets_and_saves(dog):
    try:
        _choose(dog, 'dark')
        assert dog.minimal_frame.cget('bg') == DARK['surface']
        _choose(dog, 'light')
        assert ui_theme.COLORS == dict(LIGHT)
        assert dog.minimal_frame.cget('bg') == LIGHT['surface']
        assert dog.network_tab.cget('bg') == LIGHT['bg']
        assert str(dog.guardian_panel.tree.tag_configure('next', 'background')) == LIGHT['warn_bg']
        assert dog.overview_tab.bars.cget('bg') == LIGHT['surface']
        assert ui_prefs.load().theme == 'light'
        _choose(dog, 'dark')
        assert dog.minimal_frame.cget('bg') == DARK['surface']
        assert str(dog.guardian_panel.tree.tag_configure('next', 'background')) == DARK['warn_bg']
    finally:
        _choose(dog, 'dark')


def test_status_text_colour_follows_a_switch(dog):
    try:
        _choose(dog, 'dark')
        dog.report_status("Found 212 processes")
        _choose(dog, 'light')
        assert str(dog.status_label.cget('foreground')) == LIGHT['success']
    finally:
        _choose(dog, 'dark')


def test_network_tab_shows_rates_and_hides_dead_adapters(dog, monkeypatch):
    monkeypatch.setattr('network_tab.threading.Thread', InlineThread)
    tab = dog.network_tab
    samples = iter([NetSample(0.0, 0, 0, {'Wi-Fi': (0, 0)}),
                    NetSample(2.0, 2048, 4096, {'Wi-Fi': (2048, 4096)})])
    details = ({'Wi-Fi': [NS(family=socket.AF_INET, address='192.168.0.2')],
                'Ethernet': [NS(family=socket.AF_INET, address='169.254.1.1')]},
               {'Wi-Fi': NS(isup=True, speed=1000), 'Ethernet': NS(isup=False, speed=0)})
    tab.collector = lambda need: (next(samples), details)
    tab.prev, tab.addr_stats = None, None
    tab.down_hist.clear(), tab.up_hist.clear()
    monkeypatch.setattr(tab, 'after', lambda ms, fn=None: fn() if ms == 0 else None)
    tab._work(True)
    tab._work(True)
    assert tab.down_stat.value.cget('text') == '2.0 KB/s'
    assert tab.up_stat.value.cget('text') == '1.0 KB/s'
    assert list(tab.tree.get_children()) == ['Wi-Fi']
    assert tab.toggle.cget('text') == 'Show disconnected (1)'
    tab.show_all.set(True)
    tab._render_adapters()
    assert list(tab.tree.get_children()) == ['Wi-Fi', 'Ethernet']
    assert tab.tree.set('Ethernet', 'status') == 'Disconnected'
    tab.show_all.set(False)


def test_performance_and_overview_render_history(dog):
    dog.monitor.cpu_history.extend([10, 20, 30])
    dog.monitor.ram_history.extend([40, 45, 50])
    dog.performance_tab.refresh()
    assert dog.performance_tab.cpu.value.cget('text') == '30%'
    assert 'Peak 30%' in dog.performance_tab.cpu.sub.cget('text')
    dog.overview_tab.refresh()
    dog.overview_tab.update_cores([5, 90, 40])
    assert 'core 1 at 90%' in dog.overview_tab.core_note.cget('text')
    assert dog.overview_tab.memory.value.cget('text').endswith('GB')
