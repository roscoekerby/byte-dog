# ByteDog Thrash Prevention - Design Spec

Date: 2026-07-04
Status: Approved (chat), implementing

## Problem

The PC (64GB RAM, Windows 11, Norton AV) occasionally thrashes: RAM fills, Windows
swaps to pagefile, and the machine becomes so unresponsive the mouse cannot move,
forcing a hard reboot. Chrome (many tabs) is the usual driver. ByteDog must detect
memory pressure early, surface the hogs, and act automatically before the freeze.

Key facts driving the design:

- Thrashing is a RAM/pagefile problem, not CPU. The trigger signal is RAM percent
  plus pagefile-usage growth rate, never CPU percent.
- Blocking process creation system-wide requires a kernel driver; out of scope.
  The equivalent outcome comes from early escalating action.
- Norton intercepts every OpenProcess call (~30ms each, ~16s for a full psutil
  scan of 550 processes). Emergency scans must avoid per-process handles.
- During a real thrash, a normal-priority GUI freezes too. ByteDog must pin its
  own memory and raise its priority so its rescue UI keeps working.

## Decisions (user-approved)

1. Escalating auto-action: WARN 75% (alert + hog list), ACT 85% (auto-suspend top
   hog), CRITICAL 92% (auto-kill top hog, max 3 kills/min).
2. Chrome handling: only individual renderer processes (`--type=renderer`) are
   auto-action targets, biggest first. Browser/GPU/network/utility processes are
   never touched. Applies to all Chromium browsers (chrome, msedge, brave).
3. Auto-start at login via Task Scheduler (highest privileges, minimized), with
   install/uninstall from the Tools menu.
4. python.exe/pythonw.exe are NOT default-protected: ByteDog excludes its own
   pid explicitly, and a runaway Python script is a plausible thrash cause for
   this user. Add them to the user-protected list if ever needed.

## Architecture

New module `guardian.py` holds all new logic, pure and unit-testable. `bytedog.py`
keeps the UI and wires events. No new dependencies (ctypes + psutil + stdlib).

### guardian.py components

- **GuardianConfig** (dataclass): warn_pct=75, act_pct=85, crit_pct=92,
  kill_rate_max=3 per 60s, mode ('escalate' | 'alert_only'), user_protected
  (list of extra protected process names). JSON persistence at
  `%APPDATA%/ByteDog/config.json`. Invalid/missing file loads defaults.

- **EscalationEngine** (pure state machine): `evaluate(ram_pct, swap_used, now)`
  returns a Decision (tier 'warn' | 'suspend' | 'kill' + reason) or None.
  - Tier by RAM percent against the three thresholds.
  - Early-warn trigger: pagefile usage growing faster than 20 MB/s while RAM is
    above (warn - 5) promotes to WARN even below the warn threshold. (psutil's
    swap sin/sout are always 0 on Windows, so growth of swap used is the signal.)
  - Cooldowns: warn max 1/60s, suspend max 1/30s, kills min 10s apart and capped
    by kill_rate_max per rolling 60s (`record_kill(now)` tracks them).
  - In alert_only mode every tier downgrades to a warn decision (with the real
    tier attached for display).

- **fast_memory_snapshot()**: single `NtQuerySystemInformation(SystemProcessInformation)`
  syscall returning (pid, name, rss) for every process with zero per-process
  handles, so Norton adds no overhead. Falls back to the existing psutil scan on
  any failure.

- **Target selection**: `select_targets(procs, protected, suspended_pids, n)`.
  Non-protected processes ranked by RSS. Chromium processes are only candidates
  when classified as renderers; classification (`classify_chromium`) reads
  cmdline via psutil for Chromium pids only (a handful of handles, bounded cost).
  `group_by_name(procs)` sums RSS per process name for display.

- **harden_self()**: SetPriorityClass(HIGH_PRIORITY_CLASS) and
  SetProcessWorkingSetSizeEx with hard-minimum flags (pin ~60-200MB) so ByteDog
  survives the thrash it is fighting. Logs and continues on failure (needs admin).

- **install_autostart() / uninstall_autostart()**: `schtasks /Create` ONLOGON,
  RL HIGHEST, running pythonw.exe with bytedog.py. Reports errors verbatim.

### bytedog.py wiring

- RAMGuardian keeps its name, protected set, event log, and leak detection, but
  thresholds/decisions move to GuardianConfig + EscalationEngine.
- Fast loop (2s tick) stays RAM-only: `check_ram` passes ram_pct + swap_used to
  the engine. Decisions go through the existing guardian_queue.
- On WARN: non-blocking alert (existing style) + async fast snapshot fills the
  hog list with per-name grouped totals and Kill/Suspend/Resume buttons.
- On SUSPEND: worker thread takes a snapshot, selects the top target, suspends
  it, logs, and the alert shows what was suspended with a Resume button.
- On KILL: same flow with kill; engine's rate cap consulted via record_kill.
  Repeats on subsequent ticks until RAM falls below act_pct.
- Guardian tab: three threshold sliders (Warn/Suspend/Kill), mode radio
  (Escalating auto-action / Alert only), editable extra-protected list, all
  persisted to config on change.
- Tools menu: Install auto-start / Remove auto-start.
- main() calls harden_self() at startup.

## Error handling

- Suspend/kill AccessDenied or vanished process: skip to next target, log it.
- No eligible targets above threshold: fall back to alert only.
- Snapshot failure: psutil fallback; if that fails, alert without hog list.
- Engine never selects protected names or ByteDog's own pid.

## Testing

- Unit (pytest, tests/test_guardian.py): threshold tiers, cooldowns, kill rate
  cap, swap-growth early warn, alert_only downgrade, target selection (protected
  excluded, Chromium renderer-only rule, RSS ordering, suspended-pid exclusion),
  name grouping, config round-trip and corrupt-file default.
- Real-user simulation (tests/balloon.py): allocates RAM in chunks to a target
  percent and holds, so the live app walks through WARN (and optionally ACT/
  CRITICAL, where the balloon itself becomes the hog that gets suspended then
  killed). Headed by nature; release on Ctrl+C or kill.

## Out of scope

- Kernel-level process creation blocking.
- CPU-based triggers.
- Working-set trimming (EmptyWorkingSet) - possible later addition.
