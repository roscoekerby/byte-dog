"""Unit tests for gpu.py — NVML backend, per-process VRAM, PDH fallback."""
import platform
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gpu

MB = 1024 * 1024


def make_fake_nvml(*, util=42, used=2048 * MB, total=8192 * MB, temp=55,
                   name='Fake GPU', gfx_procs=(), compute_procs=(),
                   init_error=False):
    def nvml_init():
        if init_error:
            raise RuntimeError('driver not loaded')

    return SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        nvmlInit=nvml_init,
        nvmlDeviceGetHandleByIndex=lambda i: object(),
        nvmlDeviceGetName=lambda h: name,
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=util, memory=0),
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(used=used, total=total),
        nvmlDeviceGetTemperature=lambda h, s: temp,
        nvmlDeviceGetGraphicsRunningProcesses=lambda h: list(gfx_procs),
        nvmlDeviceGetComputeRunningProcesses=lambda h: list(compute_procs),
    )


@pytest.fixture(autouse=True)
def reset_gpu_state(monkeypatch):
    monkeypatch.setattr(gpu, '_initialized', False)
    monkeypatch.setattr(gpu, '_init_failed', False)


# ── get_gpu_info ─────────────────────────────────────────────────────────

def test_gpu_info_shape_and_values(monkeypatch):
    monkeypatch.setattr(gpu, 'pynvml', make_fake_nvml())
    info = gpu.get_gpu_info()
    assert info['name'] == 'Fake GPU'
    assert info['load'] == 42.0
    assert info['memory_used'] == pytest.approx(2048.0)
    assert info['memory_total'] == pytest.approx(8192.0)
    assert info['memory_percent'] == pytest.approx(25.0)
    assert info['temperature'] == 55


def test_gpu_info_decodes_bytes_name(monkeypatch):
    monkeypatch.setattr(gpu, 'pynvml', make_fake_nvml(name=b'Fake GPU'))
    assert gpu.get_gpu_info()['name'] == 'Fake GPU'


def test_unavailable_without_pynvml(monkeypatch):
    monkeypatch.setattr(gpu, 'pynvml', None)
    assert gpu.gpu_available() is False
    assert gpu.get_gpu_info() is None
    assert gpu._nvml_process_vram() == {}


def test_init_failure_is_remembered(monkeypatch):
    fake = make_fake_nvml(init_error=True)
    calls = []
    real_init = fake.nvmlInit

    def counting_init():
        calls.append(1)
        real_init()

    fake.nvmlInit = counting_init
    monkeypatch.setattr(gpu, 'pynvml', fake)
    assert gpu.get_gpu_info() is None
    assert gpu.get_gpu_info() is None
    assert len(calls) == 1  # no retry storm after a failed init


def test_gpu_info_survives_query_error(monkeypatch):
    fake = make_fake_nvml()
    def boom(h):
        raise RuntimeError('lost device')
    fake.nvmlDeviceGetUtilizationRates = boom
    monkeypatch.setattr(gpu, 'pynvml', fake)
    assert gpu.get_gpu_info() is None


# ── NVML per-process VRAM ────────────────────────────────────────────────

def proc(pid, mem):
    return SimpleNamespace(pid=pid, usedGpuMemory=mem)


def test_nvml_process_vram_merges_and_skips_none(monkeypatch):
    monkeypatch.setattr(gpu, 'pynvml', make_fake_nvml(
        gfx_procs=[proc(1, 100 * MB), proc(2, None)],
        compute_procs=[proc(1, 150 * MB), proc(3, 50 * MB)],
    ))
    vram = gpu._nvml_process_vram()
    assert vram == {1: pytest.approx(150.0), 3: pytest.approx(50.0)}


# ── PDH instance parsing ─────────────────────────────────────────────────

def test_parse_pid_from_pdh_instance():
    assert gpu._parse_pid('pid_21664_luid_0x00000000_0x0000E7B7_phys_0') == 21664
    assert gpu._parse_pid('pid_8_luid_0x0_0x0_phys_0') == 8
    assert gpu._parse_pid('total') is None
    assert gpu._parse_pid('') is None


# ── get_process_vram routing ─────────────────────────────────────────────

def test_process_vram_prefers_pdh_on_windows(monkeypatch):
    monkeypatch.setattr(gpu.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(gpu, '_pdh_process_vram', lambda: {10: 300.0})
    monkeypatch.setattr(gpu, '_nvml_process_vram', lambda: {99: 1.0})
    assert gpu.get_process_vram() == {10: 300.0}


def test_process_vram_falls_back_to_nvml(monkeypatch):
    monkeypatch.setattr(gpu.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(gpu, '_pdh_process_vram', lambda: {})
    monkeypatch.setattr(gpu, '_nvml_process_vram', lambda: {99: 1.0})
    assert gpu.get_process_vram() == {99: 1.0}


def test_process_vram_skips_pdh_off_windows(monkeypatch):
    monkeypatch.setattr(gpu.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(
        gpu, '_pdh_process_vram',
        lambda: (_ for _ in ()).throw(AssertionError('PDH called off-Windows')))
    monkeypatch.setattr(gpu, '_nvml_process_vram', lambda: {99: 1.0})
    assert gpu.get_process_vram() == {99: 1.0}


# ── live integration (Windows + NVIDIA only) ─────────────────────────────

@pytest.mark.skipif(platform.system() != 'Windows', reason='PDH is Windows-only')
def test_pdh_live_returns_pid_keyed_mb():
    vram = gpu._pdh_process_vram()
    # dwm.exe always holds dedicated VRAM on a desktop session
    assert isinstance(vram, dict)
    for pid, mb in vram.items():
        assert isinstance(pid, int)
        assert mb >= 0.0
