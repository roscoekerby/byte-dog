# ByteDog

Lightweight Windows system monitor and RAM watchdog with proactive thrash
prevention. Part of the Dog family of utilities.

## Why

On a machine with heavy Chrome usage, RAM can fill until Windows starts
swapping hard (thrashing); at that point even the mouse stops responding and
only a hard reboot helps. ByteDog watches memory pressure and intervenes
before that point.

## Features

- Minimal / compact / detailed views (NetDog-style toggling), always-on-top overlay
- Real-time CPU, RAM, GPU monitoring with history graphs (GPU shown in the
  minimal overlay too)
- **GPU via NVML (nvidia-ml-py):** load, VRAM, temperature, all in-process
  (no nvidia-smi subprocess, no console flashes under pythonw)
- **Per-process VRAM column** in the process list; sourced from Windows GPU
  performance counters (Task Manager's data source) since NVML hides
  per-process memory under WDDM, with NVML process lists as fallback
- Process list with kill / suspend / resume
- **RAM Guardian with escalating thrash prevention:**
  - **Warn (default 75% RAM):** topmost alert with the biggest memory hogs
    (grouped per app) and one-click Kill / Suspend / Resume buttons
  - **Suspend (default 85%):** automatically freezes the top memory hog
    (reversible via Resume All)
  - **Kill (default 92%):** automatically kills the top hog, max 3 kills/min
  - Early-warning trigger when the pagefile starts growing fast (thrash onset)
    even below the warn threshold
  - Chrome/Edge/Brave aware: only individual tab (renderer) processes are ever
    auto-targeted, never the browser itself
  - Protected system-process list plus a user-editable "never touch" list
  - Alert-only mode if you want no automatic actions
- **Norton/AV-safe monitoring:** hog scans use a single kernel snapshot call
  (no per-process handle opens), ~5ms for 340 processes even with AV
  interception that makes psutil scans take 15+ seconds
- **Self-hardening:** runs at HIGH priority with a pinned working set so the
  rescue UI stays responsive during the very thrash it is fighting
- **Self-elevating:** on launch, if not already admin, ByteDog relaunches
  itself with a UAC prompt so kill/suspend and working-set pinning work at
  full strength without you having to fight a frozen Explorer to manually
  "Run as administrator" mid-thrash. Declining the prompt is fine — it
  keeps running non-elevated, just with reduced kill/suspend coverage
- **Auto-start:** installed once on first launch as a per-user Run-key entry
  (Tools menu can remove or reinstall it). The entry follows whatever launched
  ByteDog last, so switching from the script to `ByteDog.exe` updates it

## Run

```
python bytedog.py
```

or `run.bat`. Launching either way triggers one UAC elevation prompt (unless
already admin); accept it for full suspend/kill coverage and auto-start
installation. Pass `--no-elevate` to skip the prompt during development.
Requires `psutil` (and optionally `nvidia-ml-py` for GPU monitoring on
NVIDIA cards).

## Build the EXE

`run.bat` and the Run-key auto-start launch `pythonw.exe`, so the UAC prompt,
Task Manager's Startup tab and Settings > Startup all show Python's name and
icon. The packaged build fixes that identity:

```
build.bat
```

(or `pyinstaller ByteDog.spec`). Output: `dist\ByteDog.exe`, a single windowed
exe with the ByteDog icon and a version resource (ROSCODE TECH / ByteDog), no
Python install needed on the target machine. Inputs: `ByteDog.spec`,
`version_info.txt`, `ByteDog_256.ico`. The first run of the exe rewrites an
existing auto-start entry to point at the exe. Windows Defender may flag a fresh
PyInstaller exe; that is the bootloader false positive, add an exclusion for
`dist\`.

## Configuration

Guardian settings (thresholds, mode, extra protected processes) are edited in
the Guardian tab of the detailed view and persisted to
`%APPDATA%\ByteDog\config.json`.

## Testing

```
python -m pytest tests/ -q          # unit tests (escalation engine, targeting, config)
python tests/balloon.py --target 76 # live demo: inflate RAM until the WARN alert fires
python tests/balloon.py --target 86 # live demo: watch the balloon get auto-suspended
python tests/balloon.py --target 93 # live demo: watch the balloon get auto-killed
```

The balloon script makes itself the top memory hog, so the guardian acts on
it rather than on real apps. See its docstring for suspend-test caveats.

## Design

See `docs/superpowers/specs/2026-07-04-thrash-prevention-design.md` for the
full design (escalation engine, Norton-safe snapshot, Chromium renderer
targeting, self-hardening).
