"""Heat and throttling logic. The headline case is the real one from 2026-11-16:
a GPU render heated the laptop until the CPU ran at 23% of normal speed while 54% busy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from thermal_model import (
    AUTO_OFF, AUTO_PAUSE, GPU_HW_SLOWDOWN, GPU_HW_THERMAL, GPU_POWER_CAP, OK, THROTTLED, WARM, AutoRelief, Episode,
    HeatRow, Sample, ThermalConfig, ThermalEngine, ThermalLog, ThermalState, cooling_comparison,
    describe_cause, fmt_duration, gpu_reason_names, rank_heat, summarise,
)


def run(engine, samples):
    events = []
    for s in samples:
        _, new = engine.update(s)
        events += new
    return events


def render_day(seconds, step=2.0, start=0.0):
    return [Sample(start + i * step, cpu_speed=23.0, cpu_busy=54.0, gpu_temp=84.0, gpu_reasons=GPU_POWER_CAP)
            for i in range(int(seconds / step) + 1)]


def test_the_render_day_is_flagged_as_throttled_after_the_hold_time():
    engine = ThermalEngine()
    events = run(engine, render_day(8))
    assert events == [] and engine.state.level == WARM
    events = run(engine, render_day(4, start=10.0))
    assert [kind for kind, _ in events] == ['start']
    assert engine.state.level == THROTTLED
    assert engine.state.since == 0.0
    assert 'CPU slowed to 23% of normal speed while 54% busy' in engine.state.reasons


def test_a_slow_clock_on_an_idle_cpu_is_power_saving_not_throttling():
    engine = ThermalEngine()
    idle = [Sample(i * 2.0, cpu_speed=20.0, cpu_busy=4.0, gpu_temp=45.0) for i in range(60)]
    assert run(engine, idle) == []
    assert engine.state.level == OK


def test_a_busy_cpu_at_full_speed_is_fine():
    engine = ThermalEngine()
    assert run(engine, [Sample(i * 2.0, cpu_speed=112.0, cpu_busy=95.0, gpu_temp=70.0) for i in range(30)]) == []
    assert engine.state.level == OK


def test_a_brief_dip_does_not_alert():
    engine = ThermalEngine()
    samples = [Sample(0, 23, 54), Sample(2, 23, 54), Sample(4, 100, 54), Sample(6, 23, 54), Sample(8, 100, 54)]
    assert run(engine, samples) == []


def test_power_cap_alone_is_not_throttling_but_thermal_slowdown_is():
    engine = ThermalEngine()
    capped = [Sample(i * 2.0, 100, 50, 70, GPU_POWER_CAP) for i in range(20)]
    assert run(engine, capped) == [] and engine.state.level == OK
    hot = [Sample(100 + i * 2.0, 100, 50, 88, GPU_HW_THERMAL) for i in range(8)]
    assert [k for k, _ in run(engine, hot)] == ['start']
    assert 'GPU too hot (hardware slowdown)' in engine.state.reasons


def test_an_idle_gpu_flagging_slowdown_while_the_cpu_is_busy_is_not_throttling():
    """The 16 Nov false alarms: GPU idle at 54 C raising "hardware slowdown" for up to 93 minutes
    while antivirus and svchost kept the CPU busy at full speed."""
    engine = ThermalEngine()
    samples = [Sample(i * 2.0, 100.0, 60.0, 54.0, GPU_HW_SLOWDOWN | 0x1, gpu_slowdown_c=91.0, gpu_load=0.0)
               for i in range(1800)]
    assert run(engine, samples) == [] and engine.state.level == OK


def test_hardware_slowdown_on_a_busy_gpu_counts_only_near_its_slowdown_temperature():
    engine = ThermalEngine()
    cool = [Sample(i * 2.0, 100.0, 30.0, 70.0, GPU_HW_SLOWDOWN, gpu_slowdown_c=91.0, gpu_load=95.0)
            for i in range(20)]
    assert run(engine, cool) == []
    hot = [Sample(100 + i * 2.0, 100.0, 30.0, 85.0, GPU_HW_SLOWDOWN, gpu_slowdown_c=91.0, gpu_load=95.0)
           for i in range(8)]
    assert [k for k, _ in run(engine, hot)] == ['start']
    assert engine.state.reasons == ('GPU hardware slowdown',)


def test_a_heat_flag_on_an_idle_gpu_is_ignored_but_counts_when_busy():
    engine = ThermalEngine()
    idle = [Sample(i * 2.0, 100.0, 30.0, 88.0, GPU_HW_THERMAL, gpu_load=3.0) for i in range(20)]
    assert run(engine, idle) == []
    busy = [Sample(100 + i * 2.0, 100.0, 30.0, 88.0, GPU_HW_THERMAL, gpu_load=80.0) for i in range(8)]
    assert [k for k, _ in run(engine, busy)] == ['start']


def test_the_state_says_when_an_episode_is_only_winding_down():
    engine = ThermalEngine()
    run(engine, render_day(30))
    assert engine.state.limited_now is True
    run(engine, [Sample(32.0, 86.0, 20.0, 60.0)])
    assert engine.state.level == THROTTLED and engine.state.limited_now is False


def test_a_hot_gpu_without_slowdown_is_only_warm():
    engine = ThermalEngine()
    run(engine, [Sample(i * 2.0, 100, 50, 85) for i in range(20)])
    assert engine.state.level == WARM
    assert engine.state.reasons == ('GPU at 85 C',)


def test_episode_ends_after_recovery_and_records_the_worst_slowdown():
    engine = ThermalEngine()
    run(engine, render_day(60))
    slower = [Sample(62.0, 18.0, 60.0, 86.0)]
    cool = [Sample(64.0 + i * 2.0, 98.0, 20.0, 60.0) for i in range(12)]
    events = run(engine, slower + cool)
    assert [kind for kind, _ in events] == ['end']
    episode = events[0][1]
    assert episode.start == 0.0 and episode.end == 64.0
    assert episode.min_cpu_speed == 18.0 and episode.max_gpu_temp == 86.0
    assert engine.state.level == OK


def test_a_short_recovery_does_not_end_the_episode():
    engine = ThermalEngine()
    run(engine, render_day(30))
    blip = [Sample(32.0, 95.0, 50.0), Sample(34.0, 95.0, 50.0)]
    assert run(engine, blip + render_day(10, start=36.0)) == []
    assert engine.state.level == THROTTLED and engine.state.since == 0.0


def test_a_sensor_dropout_mid_throttle_is_not_a_recovery():
    engine = ThermalEngine()
    run(engine, render_day(30))
    dropout = [Sample(32.0 + i * 2.0, gpu_temp=84.0) for i in range(15)]
    assert run(engine, dropout) == []
    assert engine.state.level == THROTTLED and engine.state.since == 0.0


def test_sleep_ends_the_episode_at_the_last_sample_instead_of_counting_the_gap():
    engine = ThermalEngine()
    run(engine, render_day(60))
    events = run(engine, [Sample(60.0 + 8 * 3600, 98.0, 10.0, 40.0)])
    assert [kind for kind, _ in events] == ['end']
    assert events[0][1].end == 60.0 and events[0][1].seconds == 60.0
    assert engine.state.level == OK
    again = run(engine, [Sample(60.0 + 8 * 3600 + 2, 23.0, 54.0)])
    assert again == []                       # the hold time starts afresh after the gap


def test_flush_closes_an_open_episode_once():
    engine = ThermalEngine()
    run(engine, render_day(30))
    episode = engine.flush()
    assert episode.start == 0.0 and episode.end == 30.0
    assert engine.flush() is None and ThermalEngine().flush() is None


def test_on_battery_a_slow_clock_is_not_throttling():
    engine = ThermalEngine()
    samples = [Sample(i * 2.0, 23.0, 54.0, 50.0, on_battery=True) for i in range(30)]
    assert run(engine, samples) == [] and engine.state.level == OK


def test_a_gpu_only_episode_does_not_report_an_idle_cpu_as_the_slowdown():
    engine = ThermalEngine()
    hot = [Sample(i * 2.0, 8.0, 3.0, 90.0, GPU_HW_THERMAL) for i in range(10)]
    cool = [Sample(20.0 + i * 2.0, 8.0, 3.0, 60.0) for i in range(12)]
    episode = run(engine, hot + cool)[-1][1]
    assert episode.min_cpu_speed is None and episode.max_gpu_temp == 90.0


def test_warm_is_judged_against_the_gpus_own_slowdown_temperature():
    engine = ThermalEngine()
    run(engine, [Sample(i * 2.0, 100, 50, 84.0, gpu_slowdown_c=97.0) for i in range(5)])
    assert engine.state.level == OK          # 84 C is routine for a GPU that slows at 97 C
    run(engine, [Sample(20.0, 100, 50, 93.0, gpu_slowdown_c=97.0)])
    assert engine.state.level == WARM


def test_missing_readings_never_alert():
    engine = ThermalEngine()
    assert run(engine, [Sample(i * 2.0) for i in range(30)]) == []
    assert engine.state.level == OK


def test_heat_sources_rank_by_cpu_plus_gpu():
    names = {10: 'python.exe', 11: 'ffmpeg.exe', 12: 'chrome.exe', 13: 'idle.exe'}
    rows = rank_heat(names, {10: 24.0, 11: 1.4, 12: 5.6, 13: 0.1}, {10: 78.0, 12: 2.0, 99: 50.0})
    assert [r.name for r in rows] == ['python.exe', 'chrome.exe', 'ffmpeg.exe']
    assert rows[0] == HeatRow(10, 'python.exe', 24.0, 78.0, 102.0)
    assert describe_cause(rows) == 'python.exe (GPU)'
    assert describe_cause([HeatRow(1, 'handbrake.exe', 80.0, 0.0, 80.0)]) == 'handbrake.exe (CPU)'
    assert describe_cause([]) == ''


def test_gpu_reason_names():
    assert gpu_reason_names(GPU_POWER_CAP | GPU_HW_THERMAL) == ['GPU too hot (hardware slowdown)', 'GPU power capped']
    assert gpu_reason_names(0) == []


def test_log_round_trip_and_bad_lines(tmp_path):
    log = ThermalLog(tmp_path / 'heat' / 'log.jsonl')
    log.add_episode(Episode(100.0, 400.0, 23.0, 84.0, 'python.exe (GPU)'))
    log.add_marker('cooling pad', t=500.0)
    with log.path.open('a', encoding='utf-8') as out:
        out.write('not json\n{"type": "episode"}\n')
    episodes, markers = log.read()
    assert episodes == [Episode(100.0, 400.0, 23.0, 84.0, 'python.exe (GPU)')]
    assert markers == [(500.0, 'cooling pad')]
    assert ThermalLog(tmp_path / 'missing.jsonl').read() == ([], [])


def test_summary_clips_to_the_window_and_names_the_usual_cause():
    episodes = [Episode(0, 100, 30.0, 80.0, 'a.exe (CPU)'), Episode(900, 1100, 23.0, 84.0, 'python.exe (GPU)'),
                Episode(5000, 6000, 10.0, 90.0, 'later.exe (CPU)')]
    s = summarise(episodes, 50, 1000)
    assert s.seconds == 150 and s.count == 2
    assert s.worst_speed == 23.0 and s.usual_cause == 'python.exe (GPU)'
    assert summarise([], 0, 100) .count == 0


def test_cooling_comparison_uses_equal_spans_around_the_marker():
    day = 86400
    episodes = [Episode(1 * day, 1 * day + 7200, 23.0, 84.0, 'python.exe (GPU)'),
                Episode(3 * day, 3 * day + 600, 55.0, 80.0, 'python.exe (GPU)')]
    note, before, after = cooling_comparison(episodes, [(2 * day, 'cooling pad')], 4 * day)
    assert note == 'cooling pad'
    assert before.seconds == 7200 and after.seconds == 600
    assert before.days == after.days == 2
    assert cooling_comparison(episodes, [], 4 * day) is None
    assert cooling_comparison(episodes, [(2 * day, 'cooling pad')], 2 * day + 300) == ('cooling pad', None, None)


def test_durations_read_naturally():
    assert fmt_duration(42) == '42 s'
    assert fmt_duration(125) == '2 min'
    assert fmt_duration(3 * 3600 + 12 * 60) == '3 h 12 min'


def test_config_round_trip_and_bad_values(tmp_path):
    path = tmp_path / 'thermal.json'
    ThermalConfig(auto_action=AUTO_PAUSE, auto_after_s=30.0, alerts=False).save(path)
    loaded = ThermalConfig.load(path)
    assert loaded.auto_action == AUTO_PAUSE and loaded.auto_after_s == 30.0 and loaded.alerts is False
    path.write_text('{"auto_action": "explode", "unknown": 1}', encoding='utf-8')
    assert ThermalConfig.load(path).auto_action == AUTO_OFF
    path.write_text('nonsense', encoding='utf-8')
    assert ThermalConfig.load(path) == ThermalConfig()
    path.write_text('{"hold_s": -5, "auto_after_s": NaN, "recover_s": "soon", "cpu_busy_min_pct": 50}',
                    encoding='utf-8')
    fixed = ThermalConfig.load(path)
    assert fixed.hold_s == 2.0 and fixed.auto_after_s == 60.0 and fixed.recover_s == 20.0
    assert fixed.cpu_busy_min_pct == 50.0


def test_automatic_relief_is_off_by_default_and_acts_once_per_episode():
    throttled = ThermalState(THROTTLED, (), 23.0, 84.0, since=100.0)
    relief = AutoRelief()
    assert relief.due(ThermalConfig(), throttled, 500.0) == AUTO_OFF
    config = ThermalConfig(auto_action=AUTO_PAUSE, auto_after_s=60.0)
    assert relief.due(config, throttled, 130.0) == AUTO_OFF
    assert relief.due(config, throttled, 161.0) == AUTO_PAUSE
    relief.done(throttled)
    assert relief.due(config, throttled, 300.0) == AUTO_OFF
    assert relief.due(config, ThermalState(THROTTLED, (), 23.0, 84.0, since=900.0), 1000.0) == AUTO_PAUSE
    assert relief.due(config, ThermalState(WARM), 1000.0) == AUTO_OFF
