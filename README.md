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
  - **Warn (default 75% RAM):** a small topmost notice naming the next target,
    with Open ByteDog / Suspend Top / Resume All (auto-closes after 15s unless
    you use it). The full controls live in the main window's Guardian tab
  - **Suspend (default 85%):** automatically freezes the top memory hog
    (reversible via Resume All)
  - **Kill (default 92%):** automatically kills the top hog, max 3 kills/min
  - Early-warning trigger when the pagefile starts growing fast (thrash onset)
    even below the warn threshold
  - Chrome/Edge/Brave aware: only individual tab (renderer) processes are ever
    auto-targeted, never the browser itself
  - **Verified protection, not a name list:** a process is protected when
    Windows marks it critical (killing it blue-screens), runs it as a
    protected process, or it is a Microsoft-signed binary under the Windows
    folder (embedded or catalog signature, checked offline). Kernel
    pseudo-processes, ByteDog itself, anything ByteDog cannot open, and your
    "never touch" list are protected too. A fake `svchost.exe` in Temp is not
    protected; the real one is. Protected processes need an explicit override
    that says why
  - Alert-only mode if you want no automatic actions
- **Hog panel** (main window, Guardian tab; also Tools > Memory Hog Panel and
  the notice's Open ByteDog):
  - Every process, ranked in exactly the order Suspend Top / Kill Top Hog and
    the automatic guardian act (biggest single eligible process first). The
    highlighted row is the next target; unranked rows say why (protected,
    browser core, suspended)
  - Suspend / Resume / Kill on any specific row (right-click works too);
    protected processes still need the explicit override dialog
  - Optional "Group by app" view (Chrome x60 -> expand to each process)
  - Chrome/Edge/Brave/WebView2 processes labelled by role: tab / page,
    extension, GPU, network / audio / storage service, browser (main) with its
    window title. WebView2 rows name their host app (Teams, WhatsApp...).
    Per-tab URLs are not exposed by Chromium to other processes, so roles are
    the finest breakdown available
  - Claude Code sessions (`claude.exe`) labelled with session name, busy/idle
    status, auto title and folder, read from `~/.claude/sessions/<pid>.json`
  - **AI note** on a Claude row: runs your own `claude` CLI headless
    (`claude -p --model haiku`, no tools, no hooks/plugins, no session saved)
    on the last ~40 messages for a one-line "what is this session doing".
    About 10s, only on click (never during an alert), cached per session in
    `%APPDATA%\ByteDog\session_notes.json` until the transcript changes
- Every suspend (automatic, Top button, hog panel, Processes tab) is tracked,
  so Resume All undoes all of them
- **Select several at once:** Ctrl/Shift-click rows in the Processes tab or the
  hog panel (selecting an app group selects all its processes), then Suspend /
  Resume / Kill them together: one confirmation, protected ones listed with a
  choice to include (override) or skip them, progress shown as it goes
- **Process Explorer-style tools** (right-click in the Processes tab or hog
  panel): suspend / resume / kill a whole process tree (protected descendants
  skipped; paused ones are tracked for Resume All), restart with the
  same command line (an admin ByteDog relaunches non-admin apps without admin
  rights), set priority, set CPU affinity, open file location, copy path /
  command line, open files and DLLs
- **Details pane** under the process list: image, command line, working
  folder, parent, user, start time, priority, threads/handles, admin, verified
  signer and protection reason
- **Tools > Who Is Locking a File:** finds the processes holding a file open
  (Windows Restart Manager, no admin needed) and can kill them, safeguarded
- **Open in Process Explorer:** jumps to Microsoft Sysinternals Process
  Explorer with the process selected. Uses your copy (PATH, `C:\Tools\Sysinternals`)
  or downloads the official zip from Microsoft on request into
  `%LOCALAPPDATA%\ByteDog\sysinternals`. Any copy must be Microsoft-signed or
  ByteDog refuses to run it. (Sysinternals' licence does not allow bundling it.)
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
python -m pytest tests/ -q          # unit + Tk tests (engine, targeting, labels, hog panel, notes)
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
