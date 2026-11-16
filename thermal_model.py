"""Heat and throttling logic: is the machine being slowed down, what is making
the heat, and a log of throttle episodes. Pure logic, no Windows calls and no
tkinter, so it can be unit tested.

Throttled means the CPU is clocked well below its normal speed WHILE it is
busy, held for a few seconds, or the GPU reports a thermal slowdown. A slow
clock on an idle CPU is ordinary power saving and is never flagged.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable

APP_DIR = Path(os.environ.get('APPDATA', str(Path.home()))) / 'ByteDog'
CONFIG_PATH = APP_DIR / 'thermal.json'
LOG_PATH = APP_DIR / 'thermal_log.jsonl'

OK, WARM, THROTTLED = 'ok', 'warm', 'throttled'
AUTO_OFF, AUTO_SLOW, AUTO_PAUSE = 'off', 'slow', 'pause'

# NVML clock throttle reason bits
GPU_POWER_CAP = 0x4
GPU_HW_SLOWDOWN = 0x8
GPU_SW_THERMAL = 0x20
GPU_HW_THERMAL = 0x40
GPU_POWER_BRAKE = 0x80
GPU_THERMAL_BITS = GPU_HW_SLOWDOWN | GPU_SW_THERMAL | GPU_HW_THERMAL
GPU_REASON_TEXT = (
    (GPU_HW_THERMAL, 'GPU too hot (hardware slowdown)'),
    (GPU_SW_THERMAL, 'GPU too hot (driver slowdown)'),
    (GPU_HW_SLOWDOWN, 'GPU hardware slowdown'),
    (GPU_POWER_BRAKE, 'GPU power brake'),
    (GPU_POWER_CAP, 'GPU power capped'),
)
#: A process below this heat score is not worth listing.
MIN_HEAT_SCORE = 1.0
DAY_S = 86400
#: Samples further apart than this mean the PC slept or the watch stalled: timers start afresh.
GAP_S = 30.0
#: "Hot" starts this far below the temperature at which the GPU slows itself down.
GPU_WARM_MARGIN_C = 5.0
#: A cooling change is only compared once this much time has passed since it.
MIN_COMPARE_S = DAY_S
_LIMITS = {    # setting -> (lowest, highest) accepted from the settings file
    'cpu_speed_throttled_pct': (10.0, 95.0), 'cpu_busy_min_pct': (5.0, 95.0), 'hold_s': (2.0, 600.0),
    'recover_s': (2.0, 600.0), 'gpu_warm_c': (50.0, 110.0), 'auto_after_s': (10.0, 3600.0),
}


@dataclass
class ThermalConfig:
    alerts: bool = True
    cpu_speed_throttled_pct: float = 60.0   # at or below this % of normal speed...
    cpu_busy_min_pct: float = 30.0          # ...while at least this busy
    hold_s: float = 10.0                    # for this long, before it counts
    recover_s: float = 20.0                 # and this long clear, before it ends
    gpu_warm_c: float = 82.0
    auto_action: str = AUTO_OFF             # off | slow | pause
    auto_after_s: float = 60.0

    @classmethod
    def load(cls, path: Path | None = None) -> 'ThermalConfig':
        try:
            raw = json.loads(Path(path or CONFIG_PATH).read_text(encoding='utf-8'))
            known = {f.name for f in fields(cls)}
            config = cls(**{k: v for k, v in raw.items() if k in known})
        except (OSError, ValueError, TypeError):
            return cls()
        return config.checked()

    def checked(self) -> 'ThermalConfig':
        """Out-of-range or non-numeric settings fall back to the default, in place."""
        default = type(self)()
        for name, (low, high) in _LIMITS.items():
            value = getattr(self, name)
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            setattr(self, name, min(max(float(value), low), high) if ok else getattr(default, name))
        if self.auto_action not in (AUTO_OFF, AUTO_SLOW, AUTO_PAUSE):
            self.auto_action = AUTO_OFF
        self.alerts = bool(self.alerts)
        return self

    def save(self, path: Path | None = None) -> None:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding='utf-8')
        tmp.replace(path)


@dataclass(frozen=True)
class Sample:
    t: float
    cpu_speed: float | None = None    # % of normal clock speed (100 = nominal, can exceed with turbo)
    cpu_busy: float | None = None     # % of time the CPU is working
    gpu_temp: float | None = None
    gpu_reasons: int = 0              # NVML throttle reason bits
    gpu_power_w: float | None = None
    gpu_slowdown_c: float | None = None   # temperature at which the GPU slows itself down
    on_battery: bool = False          # on battery a slow clock is power saving, not heat


@dataclass(frozen=True)
class ThermalState:
    level: str = OK
    reasons: tuple = ()
    cpu_speed: float | None = None
    gpu_temp: float | None = None
    since: float | None = None        # when the current throttle began


@dataclass(frozen=True)
class Episode:
    start: float
    end: float
    min_cpu_speed: float | None
    max_gpu_temp: float | None
    cause: str = ''

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


def gpu_reason_names(mask: int) -> list:
    return [text for bit, text in GPU_REASON_TEXT if mask & bit]


def cpu_limited(sample: Sample, config: ThermalConfig) -> bool:
    """Slow while busy. Not on battery, where Windows holds the clock down to save power.
    A power plan that caps the processor on mains power looks the same as heat here."""
    if sample.cpu_speed is None or sample.cpu_busy is None or sample.on_battery:
        return False
    return sample.cpu_speed <= config.cpu_speed_throttled_pct and sample.cpu_busy >= config.cpu_busy_min_pct


def gpu_limited(sample: Sample) -> bool:
    return bool(sample.gpu_reasons & GPU_THERMAL_BITS)


def gpu_warm(sample: Sample, config: ThermalConfig) -> bool:
    if sample.gpu_temp is None:
        return False
    limit = config.gpu_warm_c
    if sample.gpu_slowdown_c:
        limit = sample.gpu_slowdown_c - GPU_WARM_MARGIN_C
    return sample.gpu_temp >= limit


def _reasons(sample: Sample, config: ThermalConfig) -> tuple:
    found = []
    if cpu_limited(sample, config):
        found.append(f"CPU slowed to {sample.cpu_speed:.0f}% of normal speed while {sample.cpu_busy:.0f}% busy")
    found += [text for bit, text in GPU_REASON_TEXT if sample.gpu_reasons & bit & GPU_THERMAL_BITS]
    return tuple(found)


class ThermalEngine:
    """Feed it samples; it answers with the current state and, when a throttle
    starts or ends, an event: ('start', state) or ('end', Episode)."""

    def __init__(self, config: ThermalConfig | None = None):
        self.config = config or ThermalConfig()
        self.state = ThermalState()
        self._limited_since: float | None = None
        self._clear_since: float | None = None
        self._episode_start: float | None = None
        self._min_speed: float | None = None
        self._max_temp: float | None = None
        self._last_t: float | None = None

    def flush(self) -> Episode | None:
        """End the episode in progress at the last sample (app closing, or a long gap)."""
        if self._episode_start is None:
            return None
        end = self._clear_since or self._last_t or self._episode_start
        episode = Episode(self._episode_start, end, self._min_speed, self._max_temp)
        self._episode_start = self._clear_since = self._limited_since = None
        self.state = ThermalState()
        return episode

    def update(self, sample: Sample) -> tuple:
        cfg = self.config
        events = []
        if self._last_t is not None and sample.t - self._last_t > GAP_S:
            # the PC slept or the watch stalled: do not count the gap as throttled time
            ended = self.flush()
            if ended is not None:
                events.append(ended)
            self._limited_since = None
        cpu_unknown = sample.cpu_speed is None or sample.cpu_busy is None
        if cpu_unknown and not gpu_limited(sample) and (
                self._limited_since is not None or self._episode_start is not None):
            # a sensor dropping out is not the machine recovering: hold the state
            self._last_t = sample.t
            return self.state, [('end', e) for e in events]
        self._last_t = sample.t
        limited = cpu_limited(sample, cfg) or gpu_limited(sample)
        if limited:
            self._clear_since = None
            if self._limited_since is None:
                self._limited_since = sample.t
            held = sample.t - self._limited_since >= cfg.hold_s
            if self._episode_start is None and held:
                self._episode_start = self._limited_since
                self._min_speed, self._max_temp = None, None
                events.append('start')
        else:
            self._limited_since = None
            if self._episode_start is not None:
                if self._clear_since is None:
                    self._clear_since = sample.t
                if sample.t - self._clear_since >= cfg.recover_s:
                    events.append(Episode(self._episode_start, self._clear_since,
                                          self._min_speed, self._max_temp))
                    self._episode_start = None
                    self._clear_since = None
        if self._episode_start is not None:
            if cpu_limited(sample, cfg):
                self._min_speed = sample.cpu_speed if self._min_speed is None else min(self._min_speed, sample.cpu_speed)
            if sample.gpu_temp is not None:
                self._max_temp = sample.gpu_temp if self._max_temp is None else max(self._max_temp, sample.gpu_temp)

        if self._episode_start is not None:
            level = THROTTLED
            reasons = _reasons(sample, cfg) or self.state.reasons
        elif limited or gpu_warm(sample, cfg):
            level = WARM
            reasons = _reasons(sample, cfg) or (f"GPU at {sample.gpu_temp:.0f} C",)
        else:
            level, reasons = OK, ()
        self.state = ThermalState(level, reasons, sample.cpu_speed, sample.gpu_temp, self._episode_start)
        out = []
        for event in events:
            out.append(('start', self.state) if event == 'start' else ('end', event))
        return self.state, out

    def throttled_for(self, now: float) -> float:
        return 0.0 if self._episode_start is None else max(0.0, now - self._episode_start)


@dataclass(frozen=True)
class HeatRow:
    pid: int
    name: str
    cpu: float      # % of the whole machine's CPU
    gpu: float      # % of the GPU
    score: float


def rank_heat(names: dict, cpu: dict, gpu: dict, limit: int = 12) -> list:
    """Processes ordered by how much heat they are likely making.
    ``names``: pid -> name. ``cpu``: pid -> % of the whole CPU. ``gpu``: pid -> % of the GPU.
    A watt on the GPU and a watt on the CPU heat the same chassis, so the score is the sum."""
    rows = []
    for pid in set(cpu) | set(gpu):
        if pid not in names or pid == 0:
            continue
        c, g = max(cpu.get(pid, 0.0), 0.0), min(max(gpu.get(pid, 0.0), 0.0), 100.0)
        if c + g >= MIN_HEAT_SCORE:
            rows.append(HeatRow(pid, names[pid], round(c, 1), round(g, 1), round(c + g, 1)))
    rows.sort(key=lambda r: (-r.score, r.pid))
    return rows[:limit]


def describe_cause(rows: Iterable[HeatRow]) -> str:
    top = next(iter(rows), None)
    if top is None:
        return ''
    part = 'GPU' if top.gpu > top.cpu else 'CPU'
    return f"{top.name} ({part})"


class ThermalLog:
    """Append-only record of throttle episodes and cooling-change markers."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or LOG_PATH)

    def _append(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(record) + '\n')
        except OSError:
            pass     # a log that cannot be written must never stop the monitor

    def add_episode(self, episode: Episode) -> None:
        self._append({'type': 'episode', **asdict(episode)})

    def add_marker(self, note: str, t: float | None = None) -> None:
        self._append({'type': 'marker', 't': t if t is not None else time.time(), 'note': note})

    def read(self) -> tuple:
        """``(episodes, markers)``; unreadable lines are skipped."""
        episodes, markers = [], []
        try:
            lines = self.path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return episodes, markers
        for line in lines:
            try:
                raw = json.loads(line)
                if raw.get('type') == 'episode':
                    episodes.append(Episode(float(raw['start']), float(raw['end']), raw.get('min_cpu_speed'),
                                            raw.get('max_gpu_temp'), raw.get('cause') or ''))
                elif raw.get('type') == 'marker':
                    markers.append((float(raw['t']), str(raw.get('note') or '')))
            except (ValueError, KeyError, TypeError):
                continue
        return episodes, markers


@dataclass(frozen=True)
class Summary:
    seconds: float = 0.0
    count: int = 0
    worst_speed: float | None = None
    usual_cause: str = ''
    days: float = 0.0

    @property
    def per_day_s(self) -> float:
        return self.seconds / self.days if self.days > 0 else 0.0


def summarise(episodes: Iterable[Episode], start: float, end: float) -> Summary:
    """Throttling between two times. Episodes are clipped to the window."""
    seconds, count, worst, causes = 0.0, 0, None, {}
    for e in episodes:
        overlap = min(e.end, end) - max(e.start, start)
        if overlap <= 0:
            continue
        seconds += overlap
        count += 1
        if e.min_cpu_speed is not None:
            worst = e.min_cpu_speed if worst is None else min(worst, e.min_cpu_speed)
        if e.cause:
            causes[e.cause] = causes.get(e.cause, 0.0) + overlap
    usual = max(causes, key=causes.get) if causes else ''
    return Summary(seconds, count, worst, usual, max(end - start, 0.0) / DAY_S)


def cooling_comparison(episodes: list, markers: list, now: float) -> tuple | None:
    """``(note, before, after)`` around the latest cooling-change marker, each a Summary
    over an equal span (at most 7 days). None without a marker. ``before`` and ``after``
    are None until a day has passed, because a few minutes scaled up to "per day" mislead."""
    if not markers:
        return None
    t, note = max(markers)
    span = min(now - t, 7 * DAY_S)
    if span < MIN_COMPARE_S:
        return note, None, None
    return note, summarise(episodes, t - span, t), summarise(episodes, t, t + span)


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


@dataclass
class AutoRelief:
    """Decides when the optional automatic action is due: once per throttle episode."""
    acted_for: float | None = field(default=None)

    def due(self, config: ThermalConfig, state: ThermalState, now: float) -> str:
        if config.auto_action == AUTO_OFF or state.level != THROTTLED or state.since is None:
            return AUTO_OFF
        if self.acted_for == state.since or now - state.since < config.auto_after_s:
            return AUTO_OFF
        return config.auto_action

    def done(self, state: ThermalState) -> None:
        self.acted_for = state.since
