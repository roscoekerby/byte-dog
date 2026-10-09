"""Hog ranking shared by the hog panel and the Suspend Top / Kill Top Hog
buttons, so the list the user sees is the order the buttons act in.

Ranked rows are exactly guardian.select_targets()'s order (biggest single
eligible process first). Everything else follows by size with the reason it
is not a target (protected, Chromium browser core, suspended, ByteDog).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from guardian import CHROMIUM_NAMES, classify_chromium_cmdline, select_targets
from proc_labels import LABELLED_CHROMIUM


@dataclass(frozen=True)
class HogRow:
    pid: int
    name: str
    rss: int
    rank: int | None
    reason: str
    protected: bool
    suspended: bool
    label: str = ''
    why: str = ''        # verified protection reason (safety.Verdict.reason)


@dataclass(frozen=True)
class HogGroup:
    name: str
    rss: int
    count: int
    best_rank: int | None
    rows: tuple


def _reason(p: dict, is_prot: bool, suspended: set, self_pid) -> str:
    name = (p.get('name') or '').lower()
    if p['pid'] == self_pid:
        return 'ByteDog'
    if p['pid'] in suspended:
        return 'suspended'
    if is_prot:
        return 'protected'
    if name in CHROMIUM_NAMES and p.get('ctype') != 'renderer':
        return 'browser core'
    return 'too small'


def build_rows(procs: list, protected: frozenset, suspended_pids=frozenset(),
               self_pid=None, labels: dict | None = None, min_rss: int = 0,
               protection=None) -> list:
    """Ranked targets first (select_targets order), then everything else by RSS.
    `protection(p)` returns a safety.Verdict; without it, `protected` names decide."""
    labels = labels or {}
    suspended = set(suspended_pids)

    def verdict(p):
        if protection is not None:
            v = protection(p)
            return v.protected, v.reason
        return (p.get('name') or '').lower() in protected, ''

    targets = select_targets(procs, protected, suspended_pids=suspended,
                             n=len(procs), self_pid=self_pid,
                             is_protected=(lambda p: verdict(p)[0]) if protection else None)
    rank_of = {t['pid']: i + 1 for i, t in enumerate(targets)}
    rows = []
    for p in procs:
        rss = p.get('rss') or 0
        if rss < min_rss and p['pid'] not in suspended:
            continue
        rank = rank_of.get(p['pid'])
        is_prot, why = verdict(p)
        rows.append(HogRow(
            pid=p['pid'], name=p.get('name') or '?', rss=rss, rank=rank,
            reason='' if rank else _reason(p, is_prot, suspended, self_pid),
            protected=is_prot, suspended=p['pid'] in suspended,
            label=labels.get(p['pid'], ''), why=why))
    return sorted(rows, key=lambda r: (r.rank is None, r.rank or 0, -r.rss))


def group_rows(rows: list) -> list:
    """Per-app groups, ordered by their best-ranked member, then by total size."""
    by_name = {}
    for r in rows:
        by_name.setdefault(r.name, []).append(r)
    groups = []
    for name, members in by_name.items():
        ranks = [r.rank for r in members if r.rank]
        groups.append(HogGroup(name=name, rss=sum(r.rss for r in members),
                               count=len(members), best_rank=min(ranks) if ranks else None,
                               rows=tuple(members)))
    return sorted(groups, key=lambda g: (g.best_rank is None, g.best_rank or 0, -g.rss))


UNREADABLE_RETRY_S = 60


def _psutil_cmdline(pid: int) -> list:
    import psutil
    return psutil.Process(pid).cmdline()


class RoleCache:
    """Chromium command lines cached per process identity (pid + create time;
    a command line never changes for a live process), so repeated panel
    refreshes open no new process handles and a reused PID is re-read.
    Unreadable processes are retried after UNREADABLE_RETRY_S."""

    def __init__(self, cmdline_reader: Callable[[int], list] = _psutil_cmdline,
                 clock: Callable[[], float] = time.monotonic):
        self._read = cmdline_reader
        self._clock = clock
        self._lock = threading.Lock()
        self._cmdlines = {}   # pid -> (ctime, cmdline or None, failed_at or None)

    def cmdline_of(self, pid: int):
        entry = self._cmdlines.get(pid)
        return entry[1] if entry else None

    def _lookup(self, p: dict):
        pid, ctime = p['pid'], p.get('ctime')
        entry = self._cmdlines.get(pid)
        fresh = entry and entry[0] == ctime and (
            entry[2] is None or self._clock() - entry[2] < UNREADABLE_RETRY_S)
        if not fresh:
            try:
                entry = (ctime, self._read(pid) or [], None)
            except Exception:
                entry = (ctime, None, self._clock())
            self._cmdlines[pid] = entry
        return entry[1]

    def annotate(self, procs: list) -> list:
        """New proc dicts with 'ctype' set on Chromium processes; prunes dead PIDs."""
        with self._lock:
            live = {p['pid'] for p in procs}
            self._cmdlines = {pid: v for pid, v in self._cmdlines.items() if pid in live}
            out = []
            for p in procs:
                if (p.get('name') or '').lower() not in LABELLED_CHROMIUM:
                    out.append(p)
                    continue
                cmd = self._lookup(p)
                ctype = 'unknown' if cmd is None else classify_chromium_cmdline(cmd)
                out.append({**p, 'ctype': ctype})
            return out

