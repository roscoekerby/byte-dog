# ByteDog GPU Monitoring v2 (NVML backend) - Implementation Plan

Goal: swap GPUtil for nvidia-ml-py (official NVIDIA NVML bindings), keep the
get_gpu_info() interface, add per-process VRAM, add GPU to the minimal view.

## Phase 0: dependency
- [x] Install nvidia-ml-py (approved); vetted: official NVIDIA package, 0 Popen/subprocess/os.system hits in pynvml.py (pythonw console-flash rule satisfied)
- [x] Live probe: NVML init/util/mem/temp all work; WDDM limitation CONFIRMED (usedGpuMemory=None per process) -> decision: PDH counter '\GPU Process Memory(*)\Dedicated Usage' as the Windows per-process source, NVML process lists as fallback

## Phase 1: TDD gpu.py
- [x] tests/test_gpu.py written first (RED: ModuleNotFoundError confirmed): info shape, bytes-name decode, unavailable paths, init-failure memoization, query-error resilience, NVML vram merge + None skip, PDH pid parsing, routing (PDH preferred on Windows, NVML fallback, no PDH off-Windows), live PDH smoke test
- [x] gpu.py implemented (GREEN, 11 passed): lazy nvmlInit, get_gpu_info() same dict keys as GPUtil version, get_process_vram() -> {pid: MB}, PDH reader via ctypes/pdh.dll (in-process, no subprocess)

## Phase 2: wire into bytedog.py
- [x] GPUtil import + nvidia-smi monkey-patch block replaced with gpu module; GPU_AVAILABLE = gpu_backend.gpu_available(); unused subprocess import removed
- [x] SystemMonitor.get_gpu_info delegates to gpu.get_gpu_info()
- [x] scan_process_memory attaches gpu_mb per pid (one PDH query per scan, background thread)
- [x] Process tab: "GPU MB" column added (heading, width, sort map, row values)
- [x] MinimalView: GPU label with same green/yellow/red thresholds as CPU/RAM; window grows to 200x105 only when GPU present

## Phase 3: verification
- [x] pytest green: 40 passed (29 guardian + 11 gpu)
- [x] Smoke run under pythonw: alive after 10s, 0 nvidia-smi processes spawned (console-flash class eliminated, monkey-patch no longer needed)
- [x] Live data verified: RTX 4060 detected (load/VRAM 232/8188 MB/temp 39C); per-process VRAM matches Get-Counter exactly (System pid 4 = 4.0 MB)
- [x] README update
- [x] Commit + push

## Review

**What changed and why:**

- New `gpu.py` (~170 lines): GPU backend module. NVML (nvidia-ml-py, official
  NVIDIA bindings) for aggregate load/VRAM/temperature; replaces GPUtil, which
  shelled out to nvidia-smi on every poll (the source of the earlier console-
  flash bug and a Norton-scan risk). NVML is pure in-process ctypes.
- Per-process VRAM: NVML returns usedGpuMemory=None for all processes under
  Windows WDDM driver mode (confirmed live), so the Windows path reads the
  PDH counter '\GPU Process Memory(*)\Dedicated Usage' via pdh.dll ctypes,
  the same source Task Manager uses. Values cross-checked against Get-Counter.
  NVML process lists remain the fallback (TCC mode / non-Windows).
- `bytedog.py`: get_gpu_info() delegates to the new backend (same dict shape,
  zero UI changes needed); process list gained a sortable "GPU MB" column
  filled during the background memory scan; minimal overlay now shows GPU %
  with the same color thresholds as CPU/RAM.
- Dependency: nvidia-ml-py 13.610.43 (first-party NVIDIA, vetted: no
  subprocess/Popen/os.system in the artifact).
- Note: on this Optimus laptop most desktop apps render on the iGPU, so
  dedicated-VRAM values are near zero at idle; the column becomes meaningful
  when ollama/games/ML workloads load the discrete card.

# Fix: Guardian couldn't actually kill/suspend hogs during a real thrash

Bug report: RAM Guardian correctly warned at 80%/90%, but manual "Kill Top
Hog" failed with "may need admin rights" — right when the system was
thrashing hard enough that fighting Explorer to relaunch "as administrator"
wasn't a realistic option anymore.

## Plan
- [x] Root-cause: `psutil.Process.terminate/suspend` need the same or higher
      privilege as the target; ByteDog wasn't running elevated, and wasn't
      offering to become elevated
- [x] `guardian.py`: `is_admin()`, `relaunch_elevated()` (UAC self-relaunch
      via `ShellExecuteW(..., "runas", ...)`), `enable_debug_privilege()`
      (SeDebugPrivilege on the token, widens reach without touching the
      DEFAULT_PROTECTED denylist)
- [x] `bytedog.py` `main()`: attempt elevated self-relaunch before starting
      the UI; `--no-elevate` flag to skip during dev; falls back to running
      non-elevated (with a printed warning) if the UAC prompt is declined,
      rather than blocking
- [x] Bug caught during manual verification: `enable_debug_privilege()`
      first cut omitted `restype`/`argtypes` on the Win32 calls, so
      `GetCurrentProcess()`'s -1 pseudo-handle got truncated to 32-bit and
      `OpenProcessToken` failed with error 6 (invalid handle) even though
      the logic was otherwise correct — fixed by declaring signatures
      exactly like the existing `harden_self()` pattern does
- [x] `pytest -q`: 40/40 still pass (bytedog.main() isn't exercised by
      tests, so this needed a manual `python -c` smoke check against the
      real Win32 API, not just import)
- [x] README updated: elevation-on-launch documented, old "run as admin
      manually" guidance replaced

## Review

**What changed and why:** ByteDog now self-elevates via a UAC prompt on
startup instead of silently degrading and telling the user after the fact
that an action needed admin rights. This closes the gap between "the
warning fired" and "the kill button actually worked" — the exact failure
the user hit. `enable_debug_privilege()` is a secondary hardening step for
the remaining edge case (killing a process outside the normal same-user
ACL) once elevated. Declining the UAC prompt still works — non-elevated
mode is unchanged, just with the same reduced coverage as before.

Not done: no CPU-thrash tier (Guardian is RAM/swap-pressure only, per
existing design) and no REALTIME priority bump — HIGH priority plus the
now-fully-working working-set pin was judged sufficient; REALTIME risks
starving the rest of the system, which cuts against the goal.
