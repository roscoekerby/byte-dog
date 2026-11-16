"""Runs the heat watch: samples the sensors, feeds the engine, keeps the throttle
log and hands events to the UI. No tkinter here; the app polls `events`.

Per-process readings (who is making the heat) are only taken while the machine
is warm or throttled, or while the Heat tab is open, because that counter set
is the costly one.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from typing import Callable

import gpu as gpu_backend
import thermal
from thermal_model import (
    AUTO_OFF, OK, AutoRelief, Episode, Sample, ThermalConfig, ThermalEngine, ThermalLog, ThermalState,
    describe_cause, rank_heat,
)

SAMPLE_S = 2.0
HISTORY_POINTS = 60
#: Per-process rates older than this are stale: the next read only primes the counters.
ROWS_STALE_S = 3 * SAMPLE_S


def _on_battery() -> bool:
    try:
        import psutil
        battery = psutil.sensors_battery()
        return battery is not None and battery.power_plugged is False
    except Exception:
        return False


def _real_name(pid: int, fallback: str) -> str:
    """Process V2 counters drop the extension ('python'); the safety checks need 'python.exe'."""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return fallback


class ThermalWatch:
    def __init__(self, config: ThermalConfig | None = None, log: ThermalLog | None = None,
                 cpu_reader=None, sources=None, gpu_thermal: Callable | None = None,
                 clock: Callable[[], float] = time.time, resolve_name: Callable = _real_name,
                 on_battery: Callable[[], bool] = _on_battery, self_pid: int | None = None):
        self.config = config or ThermalConfig.load()
        self.engine = ThermalEngine(self.config)
        self.log = log or ThermalLog()
        self.relief = AutoRelief()
        self.events: queue.Queue = queue.Queue()
        self.speed_history: deque = deque(maxlen=HISTORY_POINTS)
        self.temp_history: deque = deque(maxlen=HISTORY_POINTS)
        self.last_sample = Sample(0.0)
        self.rows: list = []
        self.want_rows = False           # set by the Heat tab while it is visible
        self._cpu = cpu_reader
        self._sources = sources
        self._gpu_thermal = gpu_thermal or gpu_backend.get_gpu_thermal
        self._clock = clock
        self._resolve = resolve_name
        self._on_battery = on_battery
        self._self_pid = os.getpid() if self_pid is None else self_pid
        self._names: dict = {}
        self._rows_read_at = 0.0
        self._cause = ''
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def state(self) -> ThermalState:
        return self.engine.state

    def save_config(self) -> None:
        try:
            self.config.save()
        except OSError:
            pass

    def _read_sample(self, now: float) -> Sample:
        if self._cpu is None:
            self._cpu = thermal.CpuSpeedReader()
        speed, busy = self._cpu.read()
        g = self._gpu_thermal() or {}
        return Sample(now, speed, busy, g.get('temperature'), g.get('reasons') or 0, g.get('power_w'),
                      g.get('slowdown_c'), self._on_battery())

    def _read_rows(self, now: float) -> None:
        if self._sources is None:
            self._sources = thermal.HeatSourceReader()
        names, cpu, gpu = self._sources.read()
        fresh = now - self._rows_read_at <= ROWS_STALE_S
        self._rows_read_at = now
        if not fresh:
            return                       # first read after a gap: rates are not meaningful yet
        # ByteDog must never offer to pause or close itself
        names = {pid: name for pid, name in names.items() if pid != self._self_pid}
        ranked = rank_heat(names, cpu, gpu)
        live = {row.pid for row in ranked}
        self._names = {pid: name for pid, name in self._names.items() if pid in live}
        rows = []
        for row in ranked:
            if row.pid not in self._names:
                self._names[row.pid] = self._resolve(row.pid, row.name)
            rows.append(type(row)(row.pid, self._names[row.pid], row.cpu, row.gpu, row.score))
        with self._lock:
            self.rows = rows

    def heat_rows(self) -> list:
        with self._lock:
            return list(self.rows)

    def speed_points(self) -> list:
        """A copy of the speed history that is safe to read from the UI thread."""
        return list(self.speed_history)

    def sample(self) -> list:
        """One step. Returns the events it queued (also handy for tests)."""
        now = self._clock()
        sample = self._read_sample(now)
        self.last_sample = sample
        state, changes = self.engine.update(sample)
        self.speed_history.append(sample.cpu_speed)
        self.temp_history.append(sample.gpu_temp)
        if state.level != OK or self.want_rows:
            try:
                self._read_rows(now)
            except Exception:
                pass                     # a counter failing must not stop the watch
        elif self.rows:
            with self._lock:
                self.rows = []
        out = []
        if state.level != OK and self.rows and not self._cause:
            self._cause = describe_cause(self.rows)     # the culprit when the heat began, not after it was paused
        for kind, payload in changes:
            if kind == 'start':
                out.append(('start', state, self.heat_rows()))
            else:
                episode = Episode(payload.start, payload.end, payload.min_cpu_speed,
                                  payload.max_gpu_temp, self._cause)
                self.log.add_episode(episode)
                self._cause = ''
                out.append(('end', episode))
        if state.level == OK:
            self._cause = ''
        action = self.relief.due(self.config, state, now)
        if action != AUTO_OFF and self.rows:
            self.relief.done(state)
            out.append(('auto', action, self.heat_rows()))
        for event in out:
            self.events.put(event)
        return out

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.sample()
                except Exception as e:
                    print(f"Heat watch error: {e}")
                self._stop.wait(SAMPLE_S)

        threading.Thread(target=loop, daemon=True, name='ByteDogHeat').start()

    def stop(self) -> None:
        """Stop sampling, record an episode still in progress, and release the counters."""
        self._stop.set()
        episode = self.engine.flush()
        if episode is not None:
            self.log.add_episode(Episode(episode.start, episode.end, episode.min_cpu_speed,
                                         episode.max_gpu_temp, self._cause))
        for reader in (self._cpu, self._sources):
            close = getattr(reader, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


def badge(state: ThermalState) -> tuple:
    """``(text, theme colour key)`` for the small views. Empty text while all is well."""
    if state.level == 'throttled':
        speed = f" {state.cpu_speed:.0f}%" if state.cpu_speed is not None else ''
        return f"THROTTLED{speed}", 'error'
    if state.level == 'warm':
        return 'WARM', 'warning'
    return '', 'success'
