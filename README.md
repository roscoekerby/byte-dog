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
- Real-time CPU, RAM, GPU monitoring with history graphs
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
- **Auto-start:** Tools menu installs a Task Scheduler logon task (elevated)

## Run

```
python bytedog.py
```

or `run.bat`. Run as administrator for full suspend/kill coverage and
auto-start installation. Requires `psutil` (and optionally `gputil`).

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
