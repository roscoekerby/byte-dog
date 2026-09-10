# Lessons

## 2026-07-05: Console flashes under pythonw (repeat of Mar 2026 issue class)

**Symptom:** terminal window flashing every ~2s while ByteDog runs via run.bat.

**Rule:** any app launched with pythonw has no console, so EVERY child process
spawned without `creationflags=subprocess.CREATE_NO_WINDOW` pops a visible
console window. This includes subprocess calls hidden inside third-party
libraries: GPUtil spawns nvidia-smi via bare `Popen` on every `getGPUs()` poll.

**Prevention checklist for this repo:**
- Grep any new dependency for `Popen`/`subprocess`/`os.system` before wiring it
  into a polling loop (`grep -rn "Popen" site-packages/<lib>`).
- All our own subprocess calls must pass `CREATE_NO_WINDOW` (guardian.py
  schtasks calls already do).
- After fixing, verify with a live poll for visible console windows while the
  app runs under pythonw, and remember an already-running old instance will
  keep flashing: restart the app before judging the fix. Elevated instances
  hide their CommandLine from non-elevated shells; identify them by that.

## 2026-09-10: Slice-and-replace patching corrupted tasks/todo.md (self-inflicted)

**Symptom:** `tasks/todo.md` ballooned to 390k lines and was committed/pushed that way.

**Cause:** `s[s.index("## Phase 1"):s.index("## Phase 2")]` picked an OLDER "## Phase 2"
heading from a previous session's plan, the slice came back empty, and
`str.replace("", new)` inserts `new` between every character.

**Rule:** when patching files by string, never slice between two markers that are not
unique in the file. Assert `old` is non-empty and `s.count(old) == 1` before every
replace (the bytedog.py patch script did this and refused to write; the todo patch did
not). For append-only notes like `tasks/todo.md`, append; do not re-slice earlier text.
Check `wc -l` / `git diff --stat` on non-code files before committing them.
