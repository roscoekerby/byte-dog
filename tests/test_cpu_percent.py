"""Per-process CPU % must match Task Manager: share of the whole machine
(0-100), not psutil's per-core figure (0 to 100 x logical cores)."""
import subprocess
import sys
import time

import psutil

import bytedog


def test_normalise_divides_by_logical_cores():
    assert bytedog.machine_cpu_percent(1600.0, 16) == 100.0
    assert bytedog.machine_cpu_percent(94.7, 20) == 4.7
    assert bytedog.machine_cpu_percent(0.0, 20) == 0.0
    assert bytedog.machine_cpu_percent(None, 20) == 0.0
    assert bytedog.machine_cpu_percent(50.0, 0) == 50.0     # unknown core count: as-is


def test_busy_process_reads_like_task_manager():
    spin = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        mon = bytedog.SystemMonitor()
        mon.scan_process_memory()
        time.sleep(1.5)
        rows = {p['pid']: p for p in mon.scan_process_memory()}
        one_core = 100.0 / psutil.cpu_count()
        assert 0.5 * one_core <= rows[spin.pid]['cpu_percent'] <= one_core + 1
        assert all(p['cpu_percent'] <= 100.0 for p in rows.values())
    finally:
        spin.kill()
