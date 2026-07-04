# ByteDog Thrash Prevention - Implementation Plan

Spec: docs/superpowers/specs/2026-07-04-thrash-prevention-design.md

## Phase 0: housekeeping
- [x] Commit pre-existing uncommitted Guardian v1 work (bytedog.py + run.bat) as its own commit

## Phase 1: TDD core logic (guardian.py)
- [x] Write tests/test_guardian.py (RED): engine tiers, cooldowns, kill cap, swap trigger, alert_only mode, target selection, chromium renderer rule, grouping, config round-trip
- [x] Implement guardian.py: GuardianConfig, EscalationEngine, select_targets, group_by_name, classify helpers (GREEN, 29 tests)
- [x] Implement fast_memory_snapshot (NtQuerySystemInformation) + psutil fallback; verified live: 341 processes in 3.8ms
- [x] Implement harden_self (priority + pinned working set); verified live (HIGH + 60MB pin)
- [x] Implement install_autostart / uninstall_autostart (schtasks)

## Phase 2: wire into bytedog.py
- [x] RAMGuardian: config-backed thresholds, engine-driven check_ram (ram + swap_used)
- [x] handle_guardian_event: warn alert with grouped hogs + Kill/Suspend/Resume buttons; auto suspend/kill worker for act/crit tiers
- [x] Guardian tab: 3 threshold sliders, mode radio, extra-protected editor, config persistence
- [x] Tools menu: install/remove auto-start, resume suspended
- [x] main(): harden_self at startup

## Phase 3: verification
- [x] pytest green (29 passed)
- [x] Smoke run app: alive after 10s, OS-verified PriorityClass = High
- [x] Live integration: inflated real RAM to 76.2% (30 GB balloon); engine fired warn on real psutil data; balloon selected as top target; self-pid exclusion confirmed
- [x] UI E2E: synthetic event through real mainloop; alert window appeared and populated with grouped hogs via the fast snapshot
- [x] README update
- [x] Commit + push

## Review

**What changed and why:**

- New `guardian.py` (~350 lines): all thrash-prevention logic, pure and unit
  tested. Escalation engine (warn 75 / suspend 85 / kill 92, cooldowns, 3
  kills/min cap, pagefile-growth early warning since psutil swap sin/sout are
  always 0 on Windows), RSS-ranked target selection with the Chromium
  renderer-only rule, JSON config at %APPDATA%/ByteDog, Norton-safe process
  snapshot via one NtQuerySystemInformation syscall (3.8ms vs 4-17s psutil
  under Norton), self-hardening (HIGH priority + hard-min pinned working set),
  schtasks auto-start management.
- `bytedog.py`: RAMGuardian now delegates decisions to the engine; alerts are
  tier-aware with live-filled grouped hog lists and rescue buttons
  (Kill/Suspend/Resume); auto suspend/kill runs on a worker thread walking
  down the target list on failure; Guardian tab gained 3 threshold sliders,
  mode radio, and a user-protected list editor; Tools menu gained auto-start
  install/remove.
- Design decision: python.exe is NOT default-protected (ByteDog excludes its
  own pid; a runaway Python script is a likely hog on this machine).
- Known limits: system-wide process-creation blocking needs a kernel driver
  (out of scope); working-set trimming (EmptyWorkingSet) deferred.
