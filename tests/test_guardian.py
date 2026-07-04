"""Unit tests for guardian.py — escalation engine, target selection, config."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian import (
    CHROMIUM_NAMES,
    DEFAULT_PROTECTED,
    Decision,
    EscalationEngine,
    GuardianConfig,
    classify_chromium_cmdline,
    group_by_name,
    select_targets,
)

MB = 1024 * 1024
GB = 1024 * MB


def make_engine(mode: str = 'escalate') -> EscalationEngine:
    return EscalationEngine(GuardianConfig(mode=mode))


# ── EscalationEngine: tiers ──────────────────────────────────────────────


class TestTiers:
    def test_below_warn_returns_none(self):
        assert make_engine().evaluate(74.9, 0, now=100.0) is None

    def test_warn_tier_at_threshold(self):
        d = make_engine().evaluate(75.0, 0, now=100.0)
        assert d is not None and d.tier == 'warn'

    def test_act_tier_returns_suspend(self):
        d = make_engine().evaluate(85.0, 0, now=100.0)
        assert d is not None and d.tier == 'suspend'

    def test_crit_tier_returns_kill(self):
        d = make_engine().evaluate(92.0, 0, now=100.0)
        assert d is not None and d.tier == 'kill'

    def test_alert_only_mode_downgrades_to_warn(self):
        d = make_engine(mode='alert_only').evaluate(95.0, 0, now=100.0)
        assert d is not None
        assert d.tier == 'warn'
        assert d.real_tier == 'kill'

    def test_disabled_engine_returns_none(self):
        eng = make_engine()
        eng.enabled = False
        assert eng.evaluate(95.0, 0, now=100.0) is None


# ── EscalationEngine: cooldowns ──────────────────────────────────────────


class TestCooldowns:
    def test_warn_cooldown_suppresses_repeat(self):
        eng = make_engine()
        assert eng.evaluate(76.0, 0, now=100.0) is not None
        assert eng.evaluate(76.0, 0, now=130.0) is None
        assert eng.evaluate(76.0, 0, now=161.0) is not None

    def test_suspend_cooldown(self):
        eng = make_engine()
        assert eng.evaluate(86.0, 0, now=100.0).tier == 'suspend'
        assert eng.evaluate(86.0, 0, now=110.0) is None
        assert eng.evaluate(86.0, 0, now=131.0).tier == 'suspend'

    def test_kill_min_interval(self):
        eng = make_engine()
        assert eng.evaluate(93.0, 0, now=100.0).tier == 'kill'
        assert eng.evaluate(93.0, 0, now=105.0) is None
        assert eng.evaluate(93.0, 0, now=111.0).tier == 'kill'

    def test_kill_rate_cap(self):
        eng = make_engine()
        for t in (100.0, 111.0, 122.0):
            assert eng.evaluate(93.0, 0, now=t).tier == 'kill'
            eng.record_kill(t)
        assert eng.evaluate(93.0, 0, now=133.0) is None
        # window rolls: first kill at t=100 ages out after 160
        assert eng.evaluate(93.0, 0, now=161.0).tier == 'kill'


# ── EscalationEngine: swap growth trigger ────────────────────────────────


class TestSwapTrigger:
    def test_fast_swap_growth_promotes_to_warn_below_threshold(self):
        eng = make_engine()
        assert eng.evaluate(72.0, 1 * GB, now=100.0) is None
        # +100MB of pagefile in 2s = 50 MB/s, well above the 20 MB/s trigger
        d = eng.evaluate(72.0, 1 * GB + 100 * MB, now=102.0)
        assert d is not None and d.tier == 'warn'
        assert 'swap' in d.reason.lower() or 'page' in d.reason.lower()

    def test_slow_swap_growth_no_trigger(self):
        eng = make_engine()
        assert eng.evaluate(72.0, 1 * GB, now=100.0) is None
        assert eng.evaluate(72.0, 1 * GB + 2 * MB, now=102.0) is None

    def test_swap_growth_ignored_when_ram_low(self):
        eng = make_engine()
        assert eng.evaluate(40.0, 1 * GB, now=100.0) is None
        assert eng.evaluate(40.0, 2 * GB, now=102.0) is None


# ── Target selection ─────────────────────────────────────────────────────


def proc(pid, name, rss, ctype=None):
    p = {'pid': pid, 'name': name, 'rss': rss}
    if ctype is not None:
        p['ctype'] = ctype
    return p


class TestSelectTargets:
    def test_biggest_rss_first(self):
        procs = [proc(1, 'app.exe', 1 * GB), proc(2, 'big.exe', 4 * GB)]
        targets = select_targets(procs, DEFAULT_PROTECTED)
        assert [t['pid'] for t in targets] == [2]

    def test_protected_excluded(self):
        procs = [proc(1, 'explorer.exe', 8 * GB), proc(2, 'app.exe', 1 * GB)]
        targets = select_targets(procs, DEFAULT_PROTECTED)
        assert [t['pid'] for t in targets] == [2]

    def test_user_protected_excluded(self):
        protected = DEFAULT_PROTECTED | {'myapp.exe'}
        procs = [proc(1, 'MyApp.exe', 8 * GB), proc(2, 'other.exe', 1 * GB)]
        targets = select_targets(procs, protected)
        assert [t['pid'] for t in targets] == [2]

    def test_chromium_renderer_is_candidate(self):
        procs = [
            proc(1, 'chrome.exe', 2 * GB, ctype='renderer'),
            proc(2, 'app.exe', 1 * GB),
        ]
        targets = select_targets(procs, DEFAULT_PROTECTED)
        assert [t['pid'] for t in targets] == [1]

    def test_chromium_browser_and_helpers_never_targets(self):
        procs = [
            proc(1, 'chrome.exe', 8 * GB, ctype='browser'),
            proc(2, 'chrome.exe', 6 * GB, ctype='helper'),
            proc(3, 'chrome.exe', 5 * GB),  # unclassified chromium
            proc(4, 'app.exe', 1 * GB),
        ]
        targets = select_targets(procs, DEFAULT_PROTECTED)
        assert [t['pid'] for t in targets] == [4]

    def test_suspended_and_self_excluded(self):
        procs = [proc(1, 'a.exe', 3 * GB), proc(2, 'b.exe', 2 * GB), proc(3, 'c.exe', 1 * GB)]
        targets = select_targets(procs, DEFAULT_PROTECTED,
                                 suspended_pids={1}, self_pid=2, n=3)
        assert [t['pid'] for t in targets] == [3]

    def test_n_limits_results(self):
        procs = [proc(i, f'p{i}.exe', i * GB) for i in range(1, 6)]
        targets = select_targets(procs, DEFAULT_PROTECTED, n=2)
        assert [t['pid'] for t in targets] == [5, 4]


class TestGrouping:
    def test_group_by_name_sums_and_counts(self):
        procs = [
            proc(1, 'chrome.exe', 2 * GB),
            proc(2, 'chrome.exe', 1 * GB),
            proc(3, 'code.exe', 4 * GB),
        ]
        groups = group_by_name(procs)
        assert groups[0] == {'name': 'code.exe', 'rss': 4 * GB, 'count': 1}
        assert groups[1] == {'name': 'chrome.exe', 'rss': 3 * GB, 'count': 2}


class TestClassify:
    def test_renderer(self):
        assert classify_chromium_cmdline(
            ['chrome.exe', '--type=renderer', '--lang=en']) == 'renderer'

    def test_helper(self):
        assert classify_chromium_cmdline(
            ['chrome.exe', '--type=gpu-process']) == 'helper'

    def test_browser(self):
        assert classify_chromium_cmdline(['chrome.exe']) == 'browser'


# ── Config persistence ───────────────────────────────────────────────────


class TestConfig:
    def test_round_trip(self, tmp_path):
        cfg = GuardianConfig(warn_pct=70.0, act_pct=80.0, crit_pct=90.0,
                             mode='alert_only', user_protected=['ollama.exe'])
        path = tmp_path / 'sub' / 'config.json'
        cfg.save(path)
        loaded = GuardianConfig.load(path)
        assert loaded == cfg

    def test_missing_file_gives_defaults(self, tmp_path):
        loaded = GuardianConfig.load(tmp_path / 'nope.json')
        assert loaded == GuardianConfig()

    def test_corrupt_file_gives_defaults(self, tmp_path):
        path = tmp_path / 'config.json'
        path.write_text('{not json!!')
        assert GuardianConfig.load(path) == GuardianConfig()

    def test_unknown_keys_ignored(self, tmp_path):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps({'warn_pct': 60, 'bogus_key': 1}))
        loaded = GuardianConfig.load(path)
        assert loaded.warn_pct == 60
        assert loaded.act_pct == GuardianConfig().act_pct

    def test_protected_names_merges_and_lowercases(self):
        cfg = GuardianConfig(user_protected=['MyApp.EXE'])
        names = cfg.protected_names()
        assert 'myapp.exe' in names
        assert 'explorer.exe' in names  # from defaults
