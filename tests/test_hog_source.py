"""HogSource wiring: snapshot + roles + sessions + notes -> labelled ranked rows."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian import DEFAULT_PROTECTED
from hog_source import HogSource
from session_notes import NoteCache, SummaryError

GB = 1024 ** 3


@pytest.fixture
def source(tmp_path):
    sessions, projects = tmp_path / 'sessions', tmp_path / 'projects'
    sessions.mkdir()
    (sessions / '30.json').write_text(json.dumps(
        {'pid': 30, 'sessionId': 'abc', 'cwd': r'C:\x\byte-dog', 'name': 'bd-1',
         'status': 'busy'}), encoding='utf-8')
    t = projects / 'C--x-byte-dog' / 'abc.jsonl'
    t.parent.mkdir(parents=True)
    t.write_text(json.dumps({'type': 'ai-title', 'aiTitle': 'Hog panel'}) + '\n'
                 + json.dumps({'type': 'user', 'message': {'content': 'go'}}) + '\n',
                 encoding='utf-8')
    snap = [{'pid': 10, 'name': 'chrome.exe', 'rss': 2 * GB},
            {'pid': 20, 'name': 'chrome.exe', 'rss': 1 * GB},
            {'pid': 30, 'name': 'claude.exe', 'rss': GB // 2}]
    src = HogSource(snapshot=lambda: snap, titles=lambda: {10: ['Gmail']},
                    sessions_dir=sessions, projects_dir=projects,
                    notes=NoteCache(tmp_path / 'notes.json'))
    src.roles._read = lambda pid: ['chrome.exe'] if pid == 10 else ['c', '--type=renderer']
    return src


def test_rows_are_ranked_and_labelled(source):
    rows = source.rows(DEFAULT_PROTECTED, set(), None)
    by_pid = {r.pid: r for r in rows}
    assert rows[0].pid == 20 and rows[0].rank == 1      # renderer outranks the browser core
    assert by_pid[10].reason == 'browser core'
    assert by_pid[10].label == 'browser (main): Gmail'
    assert by_pid[30].label == 'bd-1 [busy] Hog panel (byte-dog)'


def test_summarise_caches_and_shows_note(source, monkeypatch):
    source.rows(DEFAULT_PROTECTED, set(), None)
    calls = []

    def fake(session, transcript, claude_exe=None):
        calls.append(session['sessionId'])
        return 'Building the hog panel'
    monkeypatch.setattr('hog_source.summarise_session', fake)
    assert source.summarise(30) == 'Building the hog panel'
    assert source.summarise(30) == 'Building the hog panel'
    assert calls == ['abc']
    rows = {r.pid: r for r in source.rows(DEFAULT_PROTECTED, set(), None)}
    assert rows[30].label.endswith('| Building the hog panel')


def test_summarise_unknown_pid_raises(source):
    source.rows(DEFAULT_PROTECTED, set(), None)
    with pytest.raises(SummaryError):
        source.summarise(10)


def test_transcript_created_after_first_scan_is_found(source, tmp_path):
    sessions = tmp_path / 'sessions'
    (sessions / '40.json').write_text(json.dumps(
        {'pid': 40, 'sessionId': 'late', 'cwd': r'C:\x\y', 'name': 'new-1',
         'status': 'idle'}), encoding='utf-8')
    source._snapshot = lambda: [{'pid': 40, 'name': 'claude.exe', 'rss': GB}]
    first = {r.pid: r for r in source.rows(DEFAULT_PROTECTED, set(), None)}
    assert first[40].label == 'new-1 [idle] (y)'
    t = tmp_path / 'projects' / 'C--x-y' / 'late.jsonl'
    t.parent.mkdir(parents=True)
    t.write_text(json.dumps({'type': 'ai-title', 'aiTitle': 'Fresh'}) + '\n', encoding='utf-8')
    second = {r.pid: r for r in source.rows(DEFAULT_PROTECTED, set(), None)}
    assert second[40].label == 'new-1 [idle] Fresh (y)'
