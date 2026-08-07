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

# Fix: Processes tab looked broken vs. the Guardian alert screen

Follow-up bug report: the RAM Guardian alert screen shows a clean,
memory-sorted hog list, but Detailed view -> Processes tab looked "more
difficult / visually obscure" by comparison. Root-caused to three
independent, additive issues (see `_raw` wiki capture for the full
diagnosis); user said "yes fix all".

## Plan
- [x] Auto-scan on tab open: `<<NotebookTabChanged>>` bound on the detailed
      notebook (`bytedog.py` `create_detailed_view`) plus a check on
      entering detailed view, both routed through
      `_maybe_refresh_active_process_tab()` -> `refresh_processes()`, so the
      tab opens already sorted by memory instead of sitting at all-zero
      columns until a manual Refresh click
- [x] Kept it fresh while left open: `_process_tab_autorefresh_tick()`
      re-scans every 15s only while the Processes tab is the active tab
      (well above the scan's own ~4-17s worst case on AV-intercepted
      machines, so it never overlaps itself)
- [x] Real per-process CPU%: `SystemMonitor._proc_handles` keeps one
      `psutil.Process` per pid alive across `scan_process_memory()` calls
      (previously every call used fresh, single-use instances from
      `process_iter()`, so `cpu_percent()` could never produce a delta and
      the column was hardcoded to 0.0). Handles a first-sighting "prime"
      call, PID-reuse (NoSuchProcess on a stale handle triggers a
      re-prime), and pruning dead pids so the cache doesn't grow unbounded
- [x] Column overflow: explicit width for all 6 Treeview columns
      (previously `Name` fell through the width `if/elif` unmatched and
      kept ttk's ~200px default); detailed-view window widened 450px ->
      620px to fit them; added a horizontal scrollbar as a fallback for
      if the window gets resized narrower
- [x] `python -m py_compile` + `pytest -q`: 40/40 pass
- [x] Live smoke test: launched `python bytedog.py --no-elevate` in the
      background (unbuffered), confirmed it reaches the Tk mainloop with no
      traceback — `create_detailed_view()` (and therefore the new Treeview
      column config) runs unconditionally at startup regardless of the
      default view mode ("compact"), so this exercises the changed code
      even though the window isn't visibly the active view. Killed only
      the test process afterward, left the user's pre-existing running
      ByteDog instances untouched
- [ ] Visual confirmation — native Tk window, can't screenshot it from here;
      user to eyeball Detailed -> Processes after this ships

## Review

**What changed and why:** all three causes from the diagnosis were fixed
together since they're additive — fixing only one would still leave the
tab looking broken relative to the alert screen. The CPU% fix is the one
worth flagging: it's not a UI tweak, it required a persistent per-pid
`psutil.Process` cache because `cpu_percent()` is stateful (needs the same
object queried twice to produce a real delta), which the existing
fire-and-forget `process_iter()` usage couldn't support.

Not verified by me: actual visual layout in a running window (no
screenshot tooling for a native Tk app available here) — confirmed via
compile + tests + a clean background launch instead.

# Feature: block manual kill/suspend of protected processes without an explicit override

Follow-up request: the self-elevation fix (above) means ByteDog can now
actually succeed at killing/suspending system-critical processes it
previously failed against with AccessDenied — the Processes tab's manual
Kill/Suspend had *no* protected-process check at all (unlike the guardian's
automatic escalation, which already excludes DEFAULT_PROTECTED via
`select_targets`). User: grey out critical processes in the list, and gate
any kill/suspend attempt on them behind an override that explains the
specific consequence (their example: the process controlling display
output, i.e. dwm.exe).

## Plan
- [x] `guardian.py`: `PROTECTED_INFO` dict — one human-readable consequence
      description per `DEFAULT_PROTECTED` name (csrss.exe/smss.exe/wininit.exe/
      winlogon.exe/services.exe -> "typically crashes/reboots Windows",
      lsass.exe -> "Windows deliberately reboots if this dies", dwm.exe ->
      "can blank/flash the screen and force back to the lock screen" (the
      user's named example), svchost.exe -> "which service depends on which
      instance" caveat, etc. `protected_reason(name)` does the case-insensitive
      lookup with a generic fallback for user-added protected names with no
      description on file
- [x] Coverage-checked: every `DEFAULT_PROTECTED` name has a `PROTECTED_INFO`
      entry (scripted check, zero missing)
- [x] Processes tab: protected rows greyed (`#777777`) via a Treeview tag,
      applied in `update_process_list` through a new `_is_protected()` helper
      (checks `self.guardian.config.protected_names()` — default list plus
      whatever the user added)
- [x] Kill/Suspend on a protected process (button or right-click context
      menu — both route through the same `kill_selected_process`/
      `suspend_selected_process` handlers) no longer uses the plain Yes/No
      confirm; it opens `_show_protected_override_dialog`: names the
      process, states the specific consequence, and requires an explicit
      "Override & Kill/Suspend Anyway" click rather than a casual Yes.
      Non-protected processes keep the existing (kill: confirm, suspend: no
      confirm, reversible) behavior unchanged
- [x] Context menu labels get a 🔒 prefix on Kill/Suspend when the
      right-clicked process is protected, so the risk is visible before the
      dialog even opens
- [x] Design choice made without asking: didn't disable the toolbar
      Kill/Suspend buttons via selection-change binding (an alternative,
      more literal reading of "grey them out"); the always-visible greyed
      row plus a mandatory override dialog on click was judged simpler and
      equally effective — easy to revisit if the button-disabling version is
      preferred instead
- [x] `python -m py_compile` + `pytest -q`: 40/40 pass
- [x] `guardian.protected_reason()` spot-checked directly (dwm.exe, case
      insensitivity, unknown-name fallback) and full DEFAULT_PROTECTED
      coverage scripted-verified
- [x] Live smoke test: background launch reaches Tk mainloop with no
      traceback, same method as the prior two fixes
- [ ] Visual confirmation — user to eyeball the grey rows + override dialog
      (try selecting explorer.exe or dwm.exe and hitting Kill)

## Review

**What changed and why:** this closes a real safety gap the elevation fix
(above) opened up — before elevation, a manual kill on e.g. lsass.exe would
just silently fail with AccessDenied; now that ByteDog runs elevated by
default, that same click could actually succeed and crash the system. The
guardian's *automatic* escalation was already safe (`select_targets`
excludes `DEFAULT_PROTECTED`); this brings the *manual* kill/suspend path
up to the same standard, plus adds the specific-consequence explanation the
user asked for rather than a generic "are you sure?".

Not done: didn't extend protection to the alert-screen's "Kill Top
Hog"/"Suspend Top" manual buttons — they were already safe, since they only
ever pick from `select_targets()`'s already-filtered candidate list, so a
protected process can never appear as a "top hog" target through that path.

# Feature: Task Manager parity (Services/Startup/Users tabs, process tree,
# graceful kill) via 5 parallel subagents

User: "add all of those using subagents", referring to the gaps named in
the earlier Task-Manager-comparison answer. Scoped down two items before
dispatching: dropped "App History" entirely (no reliable Windows API
without diagnostic telemetry — would just be fake data) and per-process
network I/O (Windows only exposes that via ETW tracing, too heavy) in
favor of per-process Disk I/O, which is straightforward via
`psutil.Process.io_counters()`.

## Plan
- [x] Scaffolded the shared, conflict-prone wiring myself first (three new
      `notebook.add(...)` calls + matching stub tab methods in
      `create_detailed_view`), compiled/tested/committed that as a stable
      base — so no two parallel agents would independently edit the same
      shared method and collide at merge time.
- [x] 5 parallel subagents, each in an isolated `git worktree`:
      1. Graceful WM_CLOSE-before-hard-kill on manual kills only (Guardian's
         automatic emergency kill stays instant — `graceful=False` at the
         `_auto_act` call site)
      2. Process tree grouping by ppid (`show='tree headings'`, Name moved
         to the `'#0'` column) + a new Disk I/O column
      3. Services tab: list/sort/search + Start/Stop via `sc.exe`
         (confirmation-gated, background thread, `CREATE_NO_WINDOW`)
      4. Startup tab: Run keys + Startup folder, enable/disable via the
         same `StartupApproved` registry flag real Task Manager uses
         (non-destructive — never touches the actual Run value)
      5. Users tab: read-only `psutil.users()` listing, no logoff action
- [x] All 5 branches merged into `main` with `--no-ff` — **zero textual
      conflicts**, confirming the pre-scaffolding approach worked as
      intended
- [x] Post-merge semantic review (not just "did git merge cleanly"):
      AST-level duplicate-method-name check across every class (would
      silently shadow, not error), confirmed the process-tree agent
      correctly propagated its `item['values'][1]` -> `item['text']`
      Treeview change into the protected-process override dialog code
      from an earlier session (not something it was explicitly told about
      — it read the surrounding code and updated it correctly), verified
      the three separate `kill_process()` call sites (manual: default
      `graceful=True`; alert-screen "Kill Top Hog": default; Guardian
      auto-escalation: explicit `graceful=False`) are each correct
- [x] **Real bug caught by live smoke-testing, not by pytest/compile**:
      the Services tab starts its background scan thread during
      `ByteDogApp.__init__` (tabs are built eagerly at startup), before
      `run()` calls `root.mainloop()`. If the scan finished fast enough to
      hit `root.after(0, ...)` from its thread before the mainloop was
      actually pumping, Tkinter raised `RuntimeError: main thread is not
      in main loop` — reproduced live (not every run — timing-dependent
      race), fixed by deferring the scan's *start* via
      `self.root.after(1500, self.refresh_services)` instead of calling it
      directly at tab-build time, matching the existing codebase
      convention (`root.after(2000, self.update_guardian_tab)`) for
      exactly this kind of "defer until mainloop is running" need
- [x] **Second real bug, found independently while reviewing the Services
      agent's own report**: it hit `ttk.Label.config(fg=...)` ->
      `TclError: unknown option "-fg"` in its own new code (fixed there),
      and flagged that `self.status_label` (a `ttk.Label`) had the exact
      same bug pre-existing in 8 other call sites across the app —
      including Kill/Suspend/Resume/Refresh, the core interactive actions
      this whole session has been building on. Confirmed live in isolation
      first, then grepped/fixed all 8 (`fg=` -> `foreground=`), and
      separately confirmed every OTHER `fg=` site in the file targets a
      classic `tk.Label`/`tk.Button` (safe) via construction-site grep, so
      nothing else needed touching
- [x] `python -m py_compile` + `pytest -q` (40/40) + three separate live
      smoke tests (6s / 8s / 20s background launches) after the merge and
      after each of the two bug fixes above
- [x] Cleaned up all 5 worktrees and merged branches after confirming the
      merge

## Review

**What changed and why:** five genuinely independent features landed via
parallel subagents with zero merge conflicts, because the shared insertion
point (registering a new tab) was done once, upfront, by the orchestrator
rather than delegated. The two real bugs found post-merge (`ttk` `fg=`
crash, Services-tab startup race) are the clearest evidence for why "tests
pass + it compiles" isn't sufficient sign-off for Tkinter UI work — both
were only reachable by actually launching the app and either exercising a
button (fg=) or hitting an unlucky timing window (the race). Neither would
ever show up in `pytest` (which only covers `guardian.py`'s pure logic) or
`py_compile`.

Not done / explicitly scoped out: App History tab (no reliable API, would
be fake data) and per-process network I/O (needs ETW, too heavy) — told to
the user before dispatching, not silently dropped.

Known residual risk: the Services/Startup tabs' destructive-ish actions
(service Start/Stop, startup item enable/disable) were deliberately NOT
live-tested against real services/registry entries by their respective
subagents (per explicit instruction — too risky to have an agent
autonomously stop a real Windows service or toggle a real startup entry
during its own verification pass). Their correctness rests on code review
+ the documented Win32/registry mechanisms, not an end-to-end live test.
Worth the user trying Start/Stop on a low-stakes service (e.g. Print
Spooler) and a startup-item toggle themselves before trusting it on
anything that matters.
