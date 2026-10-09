"""Unit tests for hog_model.py: the ranking shared by the hog panel and the
Suspend Top / Kill Top Hog buttons."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian import DEFAULT_PROTECTED
from hog_model import RoleCache, build_rows, group_rows

GB = 1024 ** 3


def P(pid, name, gb, ctype=None):
    d = {'pid': pid, 'name': name, 'rss': int(gb * GB)}
    if ctype:
        d['ctype'] = ctype
    return d


SCREENSHOT = [
    P(1, 'Memory Compression', 12.4),
    P(2, 'vmmemWSL', 10.7),
    P(3, 'chrome.exe', 0.6, 'browser'),
    P(4, 'chrome.exe', 0.5, 'renderer'),
    P(5, 'chrome.exe', 0.4, 'renderer'),
    P(6, 'claude.exe', 0.45),
    P(7, 'llama-server.exe', 0.7),
    P(8, 'svchost.exe', 0.3),
    P(99, 'bytedog.exe', 0.2),
]


def test_rank_matches_select_targets_order():
    rows = build_rows(SCREENSHOT, DEFAULT_PROTECTED | {'vmmemwsl'}, self_pid=99)
    ranked = [r for r in rows if r.rank]
    assert [(r.rank, r.name) for r in ranked] == [
        (1, 'llama-server.exe'), (2, 'chrome.exe'), (3, 'claude.exe'), (4, 'chrome.exe')]
    assert ranked[1].pid == 4


def test_ranked_rows_come_first_then_the_rest_by_size():
    rows = build_rows(SCREENSHOT, DEFAULT_PROTECTED | {'vmmemwsl'}, self_pid=99)
    assert [r.rank for r in rows[:4]] == [1, 2, 3, 4]
    rest = rows[4:]
    assert all(r.rank is None for r in rest)
    assert [r.rss for r in rest] == sorted((r.rss for r in rest), reverse=True)


def test_reasons_explain_why_not_ranked():
    rows = {r.pid: r for r in build_rows(SCREENSHOT, DEFAULT_PROTECTED | {'vmmemwsl'},
                                         suspended_pids={5}, self_pid=99)}
    assert rows[1].reason == 'protected'
    assert rows[3].reason == 'browser core'
    assert rows[5].reason == 'suspended' and rows[5].suspended
    assert rows[99].reason == 'ByteDog'
    assert rows[7].reason == ''


def test_protected_flag_is_independent_of_rank():
    rows = {r.pid: r for r in build_rows(SCREENSHOT, DEFAULT_PROTECTED)}
    assert rows[8].protected and not rows[7].protected


def test_labels_are_attached():
    rows = {r.pid: r for r in build_rows(SCREENSHOT, DEFAULT_PROTECTED,
                                         labels={6: 'byte-dog-2 [busy]'})}
    assert rows[6].label == 'byte-dog-2 [busy]'
    assert rows[7].label == ''


def test_min_rss_hides_small_processes_but_keeps_suspended():
    procs = SCREENSHOT + [P(50, 'tiny.exe', 0.001), P(51, 'tinysusp.exe', 0.001)]
    pids = {r.pid for r in build_rows(procs, DEFAULT_PROTECTED, suspended_pids={51},
                                      min_rss=10 * 1024 * 1024)}
    assert 50 not in pids and 51 in pids


def test_group_rows_orders_groups_by_best_rank_then_size():
    rows = build_rows(SCREENSHOT, DEFAULT_PROTECTED | {'vmmemwsl'}, self_pid=99)
    groups = group_rows(rows)
    assert [g.name for g in groups[:3]] == ['llama-server.exe', 'chrome.exe', 'claude.exe']
    chrome = groups[1]
    assert chrome.count == 3
    assert chrome.rss == sum(r.rss for r in chrome.rows)
    assert [r.pid for r in chrome.rows] == [4, 5, 3]  # ranked first, then core
    unranked = groups[3:]
    assert [g.rss for g in unranked] == sorted((g.rss for g in unranked), reverse=True)


# ── RoleCache ────────────────────────────────────────────────────────────

def test_role_cache_reads_each_chromium_pid_once_and_prunes():
    calls = []

    def fake_cmdline(pid):
        calls.append(pid)
        return ['chrome.exe', '--type=renderer'] if pid == 4 else ['chrome.exe']

    cache = RoleCache(cmdline_reader=fake_cmdline)
    procs = [P(3, 'chrome.exe', 1), P(4, 'chrome.exe', 1), P(7, 'notepad.exe', 1)]
    out = cache.annotate(procs)
    assert {p['pid']: p.get('ctype') for p in out} == {3: 'browser', 4: 'renderer', 7: None}
    assert 'ctype' not in procs[0]  # input not mutated
    cache.annotate(procs)
    assert sorted(calls) == [3, 4]
    assert cache.cmdline_of(4) == ['chrome.exe', '--type=renderer']
    cache.annotate([P(4, 'chrome.exe', 1)])
    assert cache.cmdline_of(3) is None


def test_role_cache_marks_unreadable_as_unknown_and_retries_later():
    now = [0.0]
    calls = []

    def flaky(pid):
        calls.append(pid)
        if len(calls) == 1:
            raise PermissionError
        return ['c', '--type=renderer']

    cache = RoleCache(cmdline_reader=flaky, clock=lambda: now[0])
    procs = [P(3, 'chrome.exe', 1)]
    assert cache.annotate(procs)[0]['ctype'] == 'unknown'
    now[0] = 10
    assert cache.annotate(procs)[0]['ctype'] == 'unknown'   # not retried yet
    now[0] = 61
    assert cache.annotate(procs)[0]['ctype'] == 'renderer'
    assert calls == [3, 3]


def test_role_cache_rereads_a_reused_pid():
    cmd = {'first': ['c', '--type=renderer'], 'second': ['c']}
    which = ['first']
    cache = RoleCache(cmdline_reader=lambda pid: cmd[which[0]])
    assert cache.annotate([{**P(3, 'chrome.exe', 1), 'ctime': 100}])[0]['ctype'] == 'renderer'
    which[0] = 'second'
    assert cache.annotate([{**P(3, 'chrome.exe', 1), 'ctime': 100}])[0]['ctype'] == 'renderer'
    assert cache.annotate([{**P(3, 'chrome.exe', 1), 'ctime': 200}])[0]['ctype'] == 'browser'


def test_build_rows_uses_verified_protection_not_names():
    from safety import Verdict
    procs = [P(1, 'svchost.exe', 3), P(2, 'svchost.exe', 2), P(3, 'app.exe', 1)]
    # pid 1 is a fake svchost (not protected); pid 2 is the real one
    verdicts = {1: Verdict(False), 2: Verdict(True, 'Microsoft-signed Windows component'),
                3: Verdict(False)}
    rows = {r.pid: r for r in build_rows(procs, frozenset(),
                                         protection=lambda p: verdicts[p['pid']])}
    assert rows[1].rank == 1 and not rows[1].protected
    assert rows[2].rank is None and rows[2].reason == 'protected'
    assert rows[2].why == 'Microsoft-signed Windows component'
    assert rows[3].rank == 2
