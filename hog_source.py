"""One scan pipeline for the hog panel and the Top buttons: Norton-safe
snapshot -> Chromium roles (cached cmdlines) -> labels -> ranked rows.

Session titles and AI notes are cached per transcript mtime, so a refresh
re-reads nothing that has not changed. All methods run on worker threads.
"""
from __future__ import annotations

from pathlib import Path

from guardian import collect_window_titles, fast_memory_snapshot
from hog_model import RoleCache, build_rows
from proc_labels import (
    PROJECTS_DIR, SESSIONS_DIR, claude_sessions, find_transcript, label_processes,
    latest_ai_title,
)
from session_notes import NoteCache, SummaryError, find_claude, summarise_session

PANEL_MIN_RSS = 20 * 1024 * 1024


class HogSource:
    def __init__(self, snapshot=fast_memory_snapshot, titles=collect_window_titles,
                 sessions_dir: Path = SESSIONS_DIR, projects_dir: Path = PROJECTS_DIR,
                 notes: NoteCache | None = None, safety=None):
        self._snapshot = snapshot
        self._titles = titles
        self._sessions_dir = sessions_dir
        self._projects_dir = projects_dir
        self.roles = RoleCache()
        self.notes = notes or NoteCache()
        self.safety = safety      # safety.ProtectionChecker; None -> name list
        self._title_cache = {}   # sessionId -> (mtime, title, transcript)
        self.sessions = {}       # pid -> enriched session dict, from the last scan

    def procs(self) -> list:
        """Snapshot with Chromium roles, for ranking only (no labels). Only the
        role cache is locked, so the guardian's auto-act never waits on a panel
        scan's window/session/transcript work."""
        procs = self.roles.annotate(self._snapshot())
        if self.safety is not None:
            self.safety.prune(p['pid'] for p in procs)
        return procs

    def is_protected(self, p: dict) -> bool:
        return self.safety.check(p).protected if self.safety is not None else False

    def _session_info(self, session: dict) -> dict:
        sid = session.get('sessionId') or ''
        cached = self._title_cache.get(sid)
        transcript = cached[2] if cached and cached[2] else             find_transcript(self._projects_dir, sid)
        try:
            mtime = transcript.stat().st_mtime if transcript else None
        except OSError:
            transcript, mtime = None, None
        if cached and cached[0] == mtime:
            title = cached[1]
        else:
            title = latest_ai_title(transcript) if transcript else ''
            self._title_cache[sid] = (mtime, title, transcript)
        return {**session, 'title': title, 'transcript': transcript, 'mtime': mtime,
                'note': self.notes.latest(sid) if sid else None}

    def rows(self, protected, suspended_pids, self_pid, min_rss: int = PANEL_MIN_RSS) -> list:
        procs = self.procs()
        sessions = {pid: self._session_info(s)
                    for pid, s in claude_sessions(self._sessions_dir).items()}
        self.sessions = sessions
        labels = label_processes(procs, self._titles(), sessions, self.roles.cmdline_of)
        for pid, s in sessions.items():
            if s.get('note') and pid in labels:
                labels[pid] = f"{labels[pid]} | {s['note']}"
        return build_rows(procs, protected, suspended_pids=suspended_pids,
                          self_pid=self_pid, labels=labels, min_rss=min_rss,
                          protection=self.safety.check if self.safety is not None else None)

    def summarise(self, pid: int) -> str:
        """AI note for the Claude session behind pid. Raises SummaryError."""
        session = self.sessions.get(pid) or {}
        if not session:
            raise SummaryError("this claude.exe has no session file")
        cached = self.notes.get(session['sessionId'], session.get('mtime'))
        if cached:
            return cached
        note = summarise_session(session, session.get('transcript'), claude_exe=find_claude())
        self.notes.put(session['sessionId'], session.get('mtime'), note)
        return note
