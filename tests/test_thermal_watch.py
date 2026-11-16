"""The heat watch (sensors faked), the counter parsing, and the Heat tab, notice
and actions driven on the one shared ByteDogApp. No real process is touched."""
import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gpu
import thermal
from heat_actions import slow_cores
from heat_tab import HeatTab, history_lines, speed_note, status_text, summary_line
from heat_toast import build_heat_toast, culprit_line, headline
from thermal_model import (
    AUTO_PAUSE, GPU_POWER_CAP, OK, THROTTLED, Episode, HeatRow, Sample, ThermalConfig, ThermalLog, ThermalState,
    summarise,
)
from thermal_watch import ThermalWatch, badge

NAMES = {10: 'python', 11: 'ffmpeg', 12: 'chrome'}
CPU = {10: 24.0, 11: 1.4, 12: 5.6}
GPU = {10: 78.0}


class FakeCpu:
    def __init__(self, speed=23.0, busy=54.0):
        self.speed, self.busy = speed, busy

    def read(self):
        return self.speed, self.busy


class FakeSources:
    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        return NAMES, CPU, GPU


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_watch(tmp_path, config=None, cpu=None):
    clock = Clock()
    watch = ThermalWatch(
        config=config or ThermalConfig(), log=ThermalLog(tmp_path / 'log.jsonl'), cpu_reader=cpu or FakeCpu(),
        sources=FakeSources(), clock=clock, resolve_name=lambda pid, name: f"{name}.exe",
        gpu_thermal=lambda: {'temperature': 84.0, 'reasons': GPU_POWER_CAP, 'power_w': 54.0})
    return watch, clock


def step(watch, clock, times, dt=2.0):
    events = []
    for _ in range(times):
        events += watch.sample()
        clock.t += dt
    return events


def test_throttle_start_names_the_culprit_with_its_real_name(tmp_path):
    watch, clock = make_watch(tmp_path)
    events = step(watch, clock, 7)
    assert [e[0] for e in events] == ['start']
    _, state, rows = events[0]
    assert state.level == THROTTLED
    assert rows[0] == HeatRow(10, 'python.exe', 24.0, 78.0, 102.0)
    assert watch.events.get_nowait()[0] == 'start'


def test_episode_is_logged_with_its_cause_when_it_ends(tmp_path):
    cpu = FakeCpu()
    watch, clock = make_watch(tmp_path, cpu=cpu)
    step(watch, clock, 10)
    cpu.speed, cpu.busy = 98.0, 12.0
    watch._gpu_thermal = lambda: {'temperature': 60.0, 'reasons': 0, 'power_w': 5.0}
    events = step(watch, clock, 14)
    assert [e[0] for e in events] == ['end']
    episodes, _ = watch.log.read()
    assert len(episodes) == 1
    assert episodes[0].cause == 'python.exe (GPU)' and episodes[0].min_cpu_speed == 23.0
    assert watch.state.level == OK and watch.heat_rows() == []


def test_per_process_counters_are_left_alone_while_all_is_well(tmp_path):
    watch, clock = make_watch(tmp_path, cpu=FakeCpu(100.0, 20.0))
    watch._gpu_thermal = lambda: {'temperature': 50.0, 'reasons': 0, 'power_w': 5.0}
    step(watch, clock, 5)
    assert watch._sources.reads == 0
    watch.want_rows = True
    step(watch, clock, 3)
    assert watch._sources.reads == 3 and watch.heat_rows()[0].name == 'python.exe'


def test_automatic_relief_fires_once_and_only_when_switched_on(tmp_path):
    watch, clock = make_watch(tmp_path)
    assert 'auto' not in [e[0] for e in step(watch, clock, 60)]
    watch, clock = make_watch(tmp_path, ThermalConfig(auto_action=AUTO_PAUSE, auto_after_s=30.0))
    autos = [e for e in step(watch, clock, 60) if e[0] == 'auto']
    assert len(autos) == 1 and autos[0][1] == AUTO_PAUSE and autos[0][2][0].pid == 10


def test_a_failing_sensor_reads_as_unknown_not_throttled(tmp_path):
    watch, clock = make_watch(tmp_path, cpu=FakeCpu(None, None))
    watch._gpu_thermal = lambda: None
    assert step(watch, clock, 20) == []
    assert watch.state.level == OK and watch.last_sample.cpu_speed is None


def test_badge_text():
    assert badge(ThermalState()) == ('', 'success')
    assert badge(ThermalState('warm')) == ('HOT', 'warning')
    assert badge(ThermalState(THROTTLED, (), 23.4)) == ('THROTTLED 23%', 'error')


def test_counter_instances_parse_to_pids():
    names, cpu = thermal.parse_process_cpu({'chrome:1234': 40.0, 'Idle:0': 1900.0, '_Total': 2000.0, 'x:y': 1.0}, 20)
    assert names == {1234: 'chrome'} and cpu == {1234: 2.0}
    engines = {'pid_55_luid_0x0_0x1_phys_0_eng_0_engtype_3D': 60.0, 'pid_55_luid_0x0_0x1_phys_0_eng_5_engtype_Cuda': 70.0,
               'pid_77_luid_0x0_0x2_phys_0_eng_0_engtype_3D': 3.0, 'junk': 9.0}
    assert thermal.parse_gpu_engines(engines) == {55: 100.0, 77: 3.0}


def test_readers_degrade_without_counters():
    class Dead:
        def collect(self):
            return False

    assert thermal.CpuSpeedReader(Dead()).read() == (None, None)
    assert thermal.HeatSourceReader(Dead(), 8).read() == ({}, {}, {})


def test_gpu_thermal_reads_reasons_and_power(monkeypatch):
    from types import SimpleNamespace

    def no_threshold(handle, kind):
        raise RuntimeError('not supported')

    fake = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0, nvmlInit=lambda: None, nvmlDeviceGetHandleByIndex=lambda i: object(),
        nvmlDeviceGetTemperature=lambda h, s: 84, nvmlDeviceGetPowerUsage=lambda h: 54050,
        nvmlDeviceGetCurrentClocksThrottleReasons=lambda h: 4, nvmlDeviceGetTemperatureThreshold=no_threshold)
    monkeypatch.setattr(gpu, 'pynvml', fake)
    monkeypatch.setattr(gpu, '_initialized', False)
    monkeypatch.setattr(gpu, '_init_failed', False)
    assert gpu.get_gpu_thermal() == {'temperature': 84.0, 'power_w': 54.05, 'reasons': 4, 'slowdown_c': None}
    monkeypatch.setattr(gpu, 'pynvml', None)
    assert gpu.get_gpu_thermal() is None


def test_slow_down_keeps_the_later_half_of_the_cores():
    assert slow_cores(list(range(20))) == list(range(10, 20))
    assert slow_cores([3]) == [3]


def test_wording():
    throttled = ThermalState(THROTTLED, ('CPU slowed to 23% of normal speed while 54% busy',), 23.0, 84.0, 0.0)
    assert status_text(throttled) == ('THROTTLED', 'CPU slowed to 23% of normal speed while 54% busy', 'error')
    assert status_text(ThermalState()) == ('Normal', 'No throttling', 'success')
    assert speed_note(Sample(0, 72.0, 10.0), ThermalConfig()) == 'of normal speed   10% busy, so it is saving power'
    assert speed_note(Sample(0, 23.0, 54.0), ThermalConfig()) == 'of normal speed   54% busy'
    assert headline(throttled) == 'Your PC is being slowed down: CPU at 23% of normal speed'
    assert 'python.exe (PID 10)' in culprit_line([HeatRow(10, 'python.exe', 24.0, 78.0, 102.0)])
    assert culprit_line([]) == 'Finding what is making the heat...'
    episodes = [Episode(0, 7200, 23.0, 84.0, 'python.exe (GPU)')]
    assert summary_line('Last 24 hours', summarise(episodes, 0, 86400)) == \
        'Last 24 hours: throttled 2 h 00 min over 1 spell(s), slowest 23% of normal, mostly python.exe (GPU)'
    assert summary_line('Last 7 days', summarise([], 0, 86400)) == 'Last 7 days: no throttling'
    lines = history_lines(episodes, [(86400.0, 'cooling pad')], 2 * 86400.0)
    assert lines[-1] == 'Since "cooling pad": 0 s a day throttled, against 2 h 00 min a day before'


# ── on the real app ─────────────────────────────────────────────────────
@pytest.fixture
def heat(dog, tmp_path, monkeypatch):
    watch, clock = make_watch(tmp_path)
    monkeypatch.setattr(dog, 'thermal', watch)
    dog.guardian.suspended.clear()
    dog._heat_slowed.clear()
    return dog, watch, clock


def test_heat_tab_shows_the_throttle_and_its_sources(heat):
    dog, watch, clock = heat
    step(watch, clock, 8)
    tab = HeatTab(dog.root, dog)
    try:
        tab.refresh()
        assert tab.status.value.cget('text') == 'THROTTLED'
        assert tab.speed.value.cget('text') == '23%'
        assert tab.gpu.value.cget('text') == '84 °C'
        assert 'GPU power capped' in tab.gpu.sub.cget('text')
        first = tab.tree.get_children()[0]
        assert tab.tree.item(first, 'text') == 'python.exe'
        assert tab.tree.item(first, 'values')[1:3] == ('24.0%', '78%')
    finally:
        tab.destroy()


def test_pause_resume_and_close_route_through_the_app_safeguards(heat, monkeypatch):
    dog, watch, clock = heat
    step(watch, clock, 8)
    calls = []
    monkeypatch.setattr(dog, 'suspend_pid', lambda pid, name, parent=None: calls.append(('suspend', pid, name)) or True)
    monkeypatch.setattr(dog, 'kill_pid', lambda pid, name, parent=None: calls.append(('kill', pid, name)) or None)
    tab = HeatTab(dog.root, dog)
    try:
        tab.refresh()
        tab._act('pause')
        assert calls == [('suspend', 10, 'python.exe')]
        assert tab.result.cget('text').startswith('Paused python.exe (PID 10)')
        tab._act('close')
        assert calls[-1] == ('kill', 10, 'python.exe') and tab.result.cget('text') == 'Cancelled'
        assert dog.heat_resume(HeatRow(999999, 'gone.exe', 0, 0, 0)) == 'gone.exe was not paused or slowed'
    finally:
        tab.destroy()


def test_paused_processes_stay_listed_so_they_can_be_resumed(heat):
    dog, watch, clock = heat
    dog.guardian.suspended[4242] = 'render.exe'
    tab = HeatTab(dog.root, dog)
    try:
        tab.refresh()
        assert tab.tree.item('4242', 'values')[3] == 'paused'
        tab.tree.selection_set('4242')
        assert tab._selected().pid == 4242
    finally:
        tab.destroy()
        dog.guardian.suspended.clear()


def test_automatic_relief_skips_protected_and_pauses_the_next(heat, monkeypatch):
    dog, watch, clock = heat
    rows = [HeatRow(10, 'csrss.exe', 50.0, 0.0, 50.0), HeatRow(11, 'render.exe', 30.0, 60.0, 90.0)]
    paused = []
    monkeypatch.setattr(dog, '_is_protected', lambda name, pid: name == 'csrss.exe')
    monkeypatch.setattr(dog, '_same_process', lambda pid, name: True)
    monkeypatch.setattr(dog.process_manager, 'suspend_process', lambda pid: paused.append(pid) or True)
    message = dog._heat_auto(AUTO_PAUSE, rows)
    assert paused == [11] and dog.guardian.suspended == {11: 'render.exe'}
    assert message == 'Paused render.exe (PID 11) automatically to cool down'
    dog.guardian.suspended.clear()
    monkeypatch.setattr(dog, '_is_protected', lambda name, pid: True)
    assert dog._heat_auto(AUTO_PAUSE, rows) == 'Automatic relief found nothing safe to act on'


def test_slow_down_refuses_protected_processes(heat, monkeypatch):
    dog, _, _ = heat
    monkeypatch.setattr(dog, '_is_protected', lambda name, pid: True)
    assert dog.heat_slow(HeatRow(4, 'System', 9.0, 0.0, 9.0)) == 'System is protected, so it was left alone'


def test_throttle_event_opens_one_notice_and_respects_the_alert_switch(heat):
    dog, watch, clock = heat
    events = step(watch, clock, 8)
    try:
        watch.config.alerts = False
        dog.handle_heat_event(events[0])
        assert dog.heat_alert_window is None
        watch.config.alerts = True
        dog.handle_heat_event(events[0])
        first = dog.heat_alert_window
        assert first.winfo_exists() and 'python.exe' in first.culprit.cget('text')
        dog.handle_heat_event(events[0])
        assert dog.heat_alert_window is first
        dog.handle_heat_event(('end', Episode(0.0, 125.0, 23.0, 84.0, 'python.exe (GPU)')))
        assert 'Throttling ended after 2 min' in dog.guardian.event_log[0]['message']
    finally:
        if dog.heat_alert_window is not None and dog.heat_alert_window.winfo_exists():
            dog.heat_alert_window.destroy()
        dog.heat_alert_window = None


def test_notice_builds_without_a_known_culprit(heat):
    dog, _, _ = heat
    toast = build_heat_toast(dog, ThermalState(THROTTLED, (), None, 90.0, 0.0), [], lambda w: None)
    try:
        assert isinstance(toast, tk.Toplevel)
        assert toast.culprit.cget('text') == 'Finding what is making the heat...'
    finally:
        toast.destroy()


def test_badge_appears_in_the_small_views(heat):
    dog, watch, clock = heat
    step(watch, clock, 8)
    before = dog.view_mode.get()
    try:
        dog.set_view_mode('minimal')
        dog.update_metrics()
        assert 'THROTTLED 23%' in dog.minimal_metrics_label.cget('text')
        dog.set_view_mode('compact')
        dog.update_metrics()
        assert dog.heat_compact_label.cget('text') == 'THROTTLED 23%'
        assert dog.status_label.cget('text') == 'Status: Overheating'
    finally:
        dog.set_view_mode(before)
