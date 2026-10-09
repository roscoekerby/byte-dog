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

# Feature: Window column (labels a process with its open window titles)

User asked whether Chrome processes could be labeled by tab (like Task
Manager). Live-tested `EnumWindows` against a real 47-process Chrome
session first rather than assuming: only 1 PID (the browser-UI process)
owns any titled window — the ~46 sandboxed renderer/GPU/utility children
own none, since real per-renderer tab attribution needs a private
Chromium<->Task-Manager IPC channel with no public equivalent. Told the
user the honest scope before building; they confirmed the partial version.

## Plan
- [x] `guardian.py`: `collect_window_titles()` — one system-wide
      `EnumWindows` sweep -> `{pid: [titles]}`, not one sweep per process.
      Turned out more broadly useful than just Chrome: correctly labels
      Explorer windows, Word's open document, Windows Terminal's open tabs,
      any multi-window app, not Chrome-specific
- [x] `SystemMonitor.scan_process_memory()`: one `collect_window_titles()`
      call per scan, attach `pinfo['window_title']` (semicolon-joined if a
      process owns multiple windows); `get_process_list()`'s fast path
      defaults to `''`
- [x] New "Window" column: `create_process_list` (260px, left-anchored,
      relies on the existing horizontal-scrollbar fallback like Disk I/O
      did), `sort_processes`'s `col_map`, `_insert_process_row` (title
      truncated to 80 chars in the cell)
- [x] **Bug caught before it shipped**, not after: the generic sort key
      (`x.get(self.sort_column, 0) or 0`) coerced an empty `window_title`
      string to `int 0` via the `or 0` fallback — sorting by the Window
      column would crash with `TypeError: '<' not supported between
      instances of 'str' and 'int'` the instant one blank and one titled
      row were compared (guaranteed, since almost every process has no
      window). Reproduced in isolation first, then fixed by dropping the
      `or 0` (every pinfo field already has a real, always-populated
      default, so it was a no-op for every numeric column anyway)
- [x] `python -m py_compile` + `pytest -q` (40/40)
- [x] End-to-end data verification (not just "doesn't crash"): instantiated
      `SystemMonitor` directly and called `scan_process_memory()` against
      the real live desktop — confirmed correct titles attributed to the
      correct PIDs (Chrome's 4 open tabs, Explorer's 5 windows, Word's open
      doc, Windows Terminal's 4 tabs, ByteDog's own already-running window)
- [x] Live smoke test: 20s background launch, no traceback (covers initial
      autoscan + the 15s periodic re-scan tick, both now exercising the new
      column)

## Review

**What changed and why:** ships the honestly-scoped version discussed —
labels whichever process owns a visible OS window with that window's
title(s), which for Chrome means "which chrome.exe is your actual browser
and what's the active tab in each open window," not "which chrome.exe is
running which background tab" (not achievable without Chromium-internal
IPC). Landed as a genuinely general feature rather than a Chrome-specific
hack, since the underlying mechanism (EnumWindows) doesn't care what kind
of process owns the window — Explorer/Word/Terminal all benefit too.

The sort-crash bug is a good example of the value of testing with
realistic *data shape*, not just "does it run": `py_compile` and a
no-interaction smoke test both would have missed it, since it only
triggers when a user actually clicks the Window column header with a mix
of blank and titled rows present — which, on a real desktop, is the
common case, not an edge case.

# 2026-09-10: Taskbar shows Tk feather instead of ByteDog icon; startup looks like a Python exe

## Root cause (verified by bisection + 12-line repro)
- `setup_window()` calls `root.update_idletasks()` before the UI is built. That maps the
  window, so Explorer creates the taskbar button and sends its async WM_GETICON query
  while Tk is not yet pumping messages.
- `run()` then calls `update_metrics()` synchronously before `mainloop()`, and in compact
  view that runs `get_process_list()` (a multi-second psutil scan on the main thread).
- Explorer's icon query times out, it falls back to the window CLASS icon (Tk's feather)
  and never re-asks. The window itself reports the dog via WM_GETICON the whole time.
- Repro: `Tk(); iconbitmap(ico); update_idletasks(); sleep(5); mainloop()` -> feather.
  `update()` instead of `update_idletasks()` -> dog. Re-calling `iconbitmap` inside the
  loop -> dog. Changing the class icon afterwards -> still feather (taskbar caches).
- Not the cause (all tested): elevation, AppUserModelID binding, PNG-only .ico, alpha /
  topmost / menubar / emoji title / overrideredirect toggles, threads, priority hardening.
- "Python exe with a weird name" at startup: the Run key launched pythonw.exe directly, so
  the UAC prompt, Task Manager Startup tab and Settings > Startup all showed Python's
  identity. Only a real ByteDog.exe can change that.

## Phase 1: taskbar icon (done, commit dcc3ea5)
- [x] `run()` re-announces the icon (`after(0, apply_window_icon, root)`) once the loop
      runs; a fresh WM_SETICON makes Explorer ask again when Tk can answer. Tried
      withdraw-until-mainloop first: icon correct, but the late show lands after the
      launch's foreground grace period and Windows flashes the button (pink "needs
      attention"), so reverted to the original map timing
- [x] Single `apply_window_icon()` helper replaces four copy-pasted iconbitmap blocks and
      now also covers the Guardian alert and protected-process dialog
- [x] `ByteDog_256.ico` regenerated as multi-size BMP (16..256); Tk's native loader now
      handles it (no shell fallback), crisp 16/32 rendering
- [x] Tests (`tests/test_window_icon.py`): run() schedules the re-announce inside the loop;
      root and alert hand Explorer a real HICON (WM_GETICON != 0); icon has BMP 16/32/256.
      Module-scoped app fixture: per-test Tk() creation hit a Windows init.tcl read race
- [x] Verified live: pythonw launch, taskbar shows the dog, window visible, no flash

## Phase 2: ship as ByteDog.exe (done, commit e54aff8)
- [x] `ByteDog.spec` mirroring NetDog: onefile, windowed, icon, bundled .ico, hidden
      imports psutil + pynvml. Decided AGAINST `uac_admin=True`: the existing runas relaunch
      already re-executes ByteDog.exe (so the UAC prompt names ByteDog), and a manifest would
      turn "decline UAC" into "no ByteDog at all", contradicting the documented behaviour
- [x] `version_info.txt`: CompanyName ROSCODE TECH, ProductName ByteDog, FileDescription
      "ByteDog - System Resource Monitor" (verified on the built exe via VersionInfo)
- [x] `build.bat` + README "Build the EXE" section; `.gitignore` un-ignores `ByteDog.spec`
      (README's stale "Task Scheduler logon task" auto-start line corrected to the Run key)
- [x] `guardian.refresh_autostart()`: rewrites an existing Run-key entry when the launcher
      changed, never installs. `main()` calls it on every launch after the first-run install.
      Tests in `tests/test_autostart.py` against a throwaway HKCU key (stale -> rewritten,
      current -> no-op, removed -> stays removed)
- [x] Verified: `pyinstaller ByteDog.spec` builds `dist\ByteDog.exe` (11.6 MB); running it
      shows the window with its own icon (WM_GETICON != 0), the taskbar shows the dog,
      Task Manager lists ByteDog.exe, and the Run key was rewritten from
      `pythonw.exe bytedog.py` to the exe path

## Review

**Taskbar icon (Phase 1):** root cause was a startup ordering race with Explorer, not the
icon file or any window attribute. Explorer asks for the icon when the window is first
shown; ByteDog showed it in `setup_window()` and then blocked the main thread on the
process scan before `mainloop()`, so Explorer's query timed out and it cached the Tk
class icon. Fix is a single scheduled `apply_window_icon(root)` at loop start (fresh
WM_SETICON = Explorer asks again). The "show nothing until the loop runs" alternative was
tried and rejected because the late show trips Windows' foreground-lock flash.

**Startup identity (Phase 2):** a `pythonw.exe` Run-key entry can only ever show Python's
name and icon in UAC / Task Manager / Settings. The packaged exe carries the icon and a
version resource, and the Run key follows the launcher automatically so the switch needs
no manual step. Elevation model unchanged (runas relaunch, declining keeps a non-elevated
instance).

**Mishap:** commit dcc3ea5 carries a corrupted multi-megabyte `tasks/todo.md` (a slice
between a "Phase 1" heading and an OLDER "Phase 2" heading came back empty, and
`str.replace("", block)` inserted the block between every character). Fixed forward here;
the bloated blob stays in history unless the branch is rewritten.

# 2026-10-10: Per-process hog panel (suspend/kill specific processes, labelled)

## Problem
- Alert's "Top memory users" groups by name and includes protected processes, but
  Suspend/Kill Top act on the biggest single eligible PID (select_targets). Display and
  action use different rankings, so "Suspend Top" hit llama-server (0.7 GB) while the
  list showed Chrome 7.7 GB at the top.
- No way to act on one specific process from the alert.
- Manual suspends from the Processes tab are not tracked, so Resume All misses them.
- claude.exe x17 and chrome.exe x60 rows are anonymous.

## Decisions (confirmed 2026-10-10)
- Controls live in a hog panel (the alert's list, plus a maximisable Full view).
- "Top" = biggest single eligible process; the panel shows that exact ranking with the
  next target highlighted.
- Claude rows: local info + an AI one-line summary.

## Phase 1: labelling (guardian.py, pure + tested)
- [x] `chromium_label(cmdline)`: renderer / extension / GPU / network / audio /
      storage / utility / browser, from `--type=` and `--utility-sub-type=`; browser
      process gets its window title(s)
- [x] `claude_sessions()`: read `~/.claude/sessions/*.json` -> {pid: name, cwd, status,
      sessionId}; plus the transcript's latest `ai-title` (read the tail only, bounded)
- [x] `label_process(p)`: one string per PID for the panel
- [x] Tests with fixture cmdlines and a temp sessions dir

## Phase 2: hog panel (new modules `hog_panel.py` + `hog_source.py`, bytedog.py only wires it in)
- [x] Per-app groups (expandable) -> per-PID rows: name, label, GB, state, protected
      lock; sorted by RSS
- [x] Row actions: Suspend / Resume / Kill (protected -> existing override dialog;
      kill -> confirm, graceful close first)
- [x] Next auto/Top target highlighted, using the same select_targets() call as the buttons
- [x] "Full view" opens the same panel in a resizable window that can be maximised
- [x] Refresh with the existing fast snapshot (Norton-safe); label lookups cached by PID

## Phase 3: suspend tracking
- [x] Every suspend (alert, panel, Processes tab) goes into `guardian.suspended`, so
      Resume All covers it; resumed/dead PIDs are removed
- [x] Panel and Processes tab show the suspended state

## Phase 4: AI summary for Claude sessions
- [x] On-demand "Summarise" per claude row (never automatic during a RAM alert: spawning
      a model process under memory pressure makes it worse)
- [x] Runs `claude -p --model haiku` headless with the last ~40 transcript messages
      (text only, truncated), CREATE_NO_WINDOW, 60s timeout, on a worker thread;
      uses your existing Claude Code login, so no API key and no new dependency
- [x] Cache keyed by sessionId + transcript mtime in %APPDATA%\ByteDog
- [x] Tests: prompt building + cache; subprocess mocked

## Verification
- [x] pytest green; live run: open the alert, confirm list order == target order,
      suspend/resume a specific chrome renderer and a specific claude session, Resume All
      undoes a Processes-tab suspend, Summarise returns a note for a live session

## Review

**Ordering root cause:** the alert listed RSS summed per app name (protected processes
included) while Suspend Top / Kill Top Hog acted on `select_targets()`'s biggest single
eligible PID. Now one ranking (`hog_model.build_rows` calls `select_targets` itself) feeds
both the list and the buttons, and the next target is highlighted. Verified live: panel #1
== `_select_top_target()` on the real desktop.

**Built:** `proc_labels.py` (Chromium/WebView2 roles from cmdlines, Claude sessions from
`~/.claude/sessions/<pid>.json` + transcript ai-title), `hog_model.py` (ranked rows, groups,
RoleCache), `hog_source.py` (scan pipeline), `hog_panel.py` (widget in the alert + full
window), `session_notes.py` (on-demand `claude -p --model haiku --tools "" --safe-mode
--no-session-persistence`, cached per transcript mtime). `bytedog.py` gained shared
`suspend_pid/resume_pid/kill_pid` used by the Processes tab and the panel, so every suspend
is tracked and Resume All covers it.

**Code review fixes (python-reviewer):** failed scans froze the panel (except-bound name
used in a later lambda); a scan-wide lock could delay the emergency auto-act (now only the
role cache is locked); transcripts created after the first scan were never found; kill
failures from the main window were silent; the warn alert's 15s countdown could destroy
it under an open confirm dialog (now pinned once used); a failed resume dropped tracking;
the list jumped to the top every refresh; `claude.CMD` timeouts blocked ~20s because only
cmd.exe was killed (now taskkill /T, proven by test); PID reuse (name re-check before
acting, role cache keyed on pid + create time, unreadable cmdlines retried after 60s).
Accepted: first scan reads every Chromium cmdline once (cached after).

**Tests:** 101 pass, 6/6 repeated runs. One Tcl interpreter per session (conftest `dog`):
a second `Tk()` per run flaked on init.tcl and could not be retried.

# 2026-10-10: Task Manager, but better (Process Explorer powers, verified safeguards, docked Guardian)

## Findings
- The "deep task manager" is Sysinternals Process Explorer (ran from Downloads, folders since
  deleted; only `C:\Tools\Sysinternals\handle64.exe` remains). It is GUI-only with no API, so
  it cannot be a backend. The only external control is `procexp64.exe /s:<PID>` (open with
  that process selected). PsSuspend/PsKill make the same Win32 calls ByteDog already makes.
  Sysinternals licence forbids bundling, so ByteDog locates it or downloads it from Microsoft.
- Guardian already runs inside ByteDog and starts enabled; it only looks separate (popup).

## Decisions (confirmed 2026-10-10)
- Native Process Explorer powers + "Open in Process Explorer" jump-out.
- Guardian docked into the main window; popup shrinks to a toast that opens it.
- Protection verified by Windows, not by name; override dialog kept.

## Phase 1: verified safeguards (new `safety.py`, tests first)
- [x] `is_critical(pid)`: NtQueryInformationProcess(ProcessBreakOnTermination). Windows
      blue-screens if these die, so they are always protected
- [x] `image_path(pid)` + `is_ms_system_binary(path)`: under %SystemRoot% AND Authenticode
      signed by Microsoft (WinVerifyTrust, embedded + catalog signatures), cached per
      (path, mtime). A fake `svchost.exe` in Temp is NOT protected; the real one is
- [x] Kernel pseudo-processes with no image (System, Registry, Memory Compression, Secure
      System, Idle) protected by PID/kind; ByteDog itself; user "never touch" list kept
- [x] One `protection(pid, name)` -> (protected, reason) used by the panel, the Processes
      tab, the override dialog text and the Guardian's auto-targeting
- [x] Unknown (access denied, unsigned check failed) on a System32 path = protected (fail safe)

## Phase 2: Process Explorer powers, native (new `proc_actions.py` + details pane)
- [x] Kill process tree (children first, same safeguards per child)
- [x] Restart process (same exe, args, working dir; refused for protected)
- [x] Set priority, set CPU affinity
- [x] Open file location, copy command line / path
- [x] Details pane under the Processes list: path, command line, parent, user, started,
      verified signer, protection reason; replaces the details popup
- [x] Open files / DLLs of a process (psutil), and "Who is locking this file?" via the
      Windows Restart Manager. CHANGED from handle64.exe: it found nothing when not run as
      admin (verified), while Restart Manager works without admin and needs no download

## Phase 3: Process Explorer jump-out (new `sysinternals.py`)
- [x] Locate procexp64.exe: PATH, C:\Tools\Sysinternals,
      %LOCALAPPDATA%\ByteDog\sysinternals
- [x] If missing, offer a one-click download from download.sysinternals.com, then verify
      the Microsoft signature before ever running it (refuse otherwise)
- [x] "Open in Process Explorer" on every process row (hog panel + Processes tab)

## Phase 4: Guardian docked in the main window
- [x] Guardian tab hosts the full hog panel, thresholds and event log in ByteDog's style
- [x] Popup becomes a small toast: RAM %, action taken, [Open ByteDog] [Resume All]
      [Dismiss]; Open jumps to the main window's Guardian tab (detailed view)
- [x] "Full view" / Tools menu / tray paths all land on that tab; separate hog window removed

## Verification
- [x] Unit tests per module (signature + critical checks against real System32 binaries and a
      copied fake svchost.exe in a temp dir)
- [x] Live: tree-kill and restart a throwaway process tree; real svchost/csrss refused
      without override; fake svchost not protected; Process Explorer opens on the right
      PID; handle search finds a file this test locked; toast -> Guardian tab

## Review (task manager phases 1-4)

**Built:** verified protection (`safety.py`, `signing.py`), Process Explorer-style tools
(`proc_actions.py`, `file_locks.py`, `process_tools_ui.py`), Process Explorer jump-out
(`sysinternals.py`), Guardian docked in the main window (`guardian_toast.py`; hog panel in the
Guardian tab). Deviation: Restart Manager + psutil instead of handle64.exe (handle64 saw
nothing without admin).

**Code review fixes (python-reviewer):** signature failures under memory pressure were cached
as "unsigned", which could have unprotected explorer.exe exactly during a thrash; now only
definitive answers are cached and an unverifiable Windows-folder binary is protected.
Signatures are only checked for Windows-folder binaries (first pass 0.89s -> 0.23s).
Process Explorer launch: no location is trusted (WindowsApps alias exemption removed), the
exe must be exactly Microsoft-signed AND name Process Explorer in its version info, and it is
verified on a handle that denies write/rename/delete until the process has started (proven:
write, rename and delete all blocked). PID reuse: verdicts keyed on live start time (ms,
snapshot and psutil agree). Fail-safe on missing image path / unknown critical flag. Restart
validates before killing, launches the exact image, refuses rather than relaunching as admin.
Kill tree / restart / open files moved off the UI thread; download worker reports any error;
duplicate svchost rows in who-locks; attribute list freed only after init; toast RAM bar drew
0 px; second ByteDog.exe instance protected by image path; truncated names broke the PID check.
Kept as designed: no name-list union (user chose verified-not-by-name; the failure mode it
guarded is now closed directly).

**Also found by tests:** switching view mode crashed while any popup was open (pack_forget on
a Toplevel); details() reported explorer.exe as exited because its parent is gone.

**Tests:** 166 pass.
