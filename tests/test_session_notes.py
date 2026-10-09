"""Unit tests for session_notes.py: on-demand AI notes for Claude sessions.
The claude CLI is never really called: the runner is injected."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_notes import (
    NoteCache,
    SummaryError,
    build_prompt,
    recent_messages,
    summarise_session,
)


def _transcript(tmp_path, records):
    t = tmp_path / 's.jsonl'
    t.write_text('\n'.join(json.dumps(r) for r in records) + '\n', encoding='utf-8')
    return t


RECORDS = [
    {'type': 'user', 'message': {'role': 'user', 'content': 'Fix the hog ordering in ByteDog'}},
    {'type': 'user', 'message': {'role': 'user', 'content': '<system-reminder>noise</system-reminder>'}},
    {'type': 'assistant', 'message': {'content': [
        {'type': 'thinking', 'thinking': 'secret'},
        {'type': 'text', 'text': 'Reading guardian.py now.'},
        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'ls'}}]}},
    {'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'content': 'huge output'}]}},
    {'type': 'attachment', 'attachment': {'type': 'x'}},
    {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'execute'}]}},
]


def test_recent_messages_keeps_only_conversation_text(tmp_path):
    msgs = recent_messages(_transcript(tmp_path, RECORDS))
    assert msgs == ['user: Fix the hog ordering in ByteDog',
                    'assistant: Reading guardian.py now.',
                    'user: execute']


def test_recent_messages_limits_count_and_length(tmp_path):
    recs = [{'type': 'user', 'message': {'content': f'm{i} ' + 'x' * 2000}} for i in range(100)]
    msgs = recent_messages(_transcript(tmp_path, recs), limit=5, max_chars=50)
    assert len(msgs) == 5
    assert msgs[0].startswith('user: m95')
    assert all(len(m) <= len('user: ') + 50 + 1 for m in msgs)


def test_build_prompt_includes_context_and_asks_for_one_line():
    prompt = build_prompt({'name': 'byte-dog-2', 'cwd': r'C:\x\byte-dog', 'status': 'busy'},
                          ['user: hi', 'assistant: hello'])
    assert 'byte-dog-2' in prompt and r'C:\x\byte-dog' in prompt and 'busy' in prompt
    assert 'user: hi\nassistant: hello' in prompt
    assert 'one line' in prompt.lower()


# ── NoteCache ────────────────────────────────────────────────────────────

def test_note_cache_round_trip_and_staleness(tmp_path):
    path = tmp_path / 'notes.json'
    cache = NoteCache(path)
    assert cache.get('s1', 10.0) is None
    cache.put('s1', 10.0, 'Fixing hog ordering')
    assert NoteCache(path).get('s1', 10.0) == 'Fixing hog ordering'
    assert NoteCache(path).get('s1', 11.0) is None  # transcript changed since
    assert NoteCache(path).latest('s1') == 'Fixing hog ordering'


def test_note_cache_tolerates_corrupt_file(tmp_path):
    path = tmp_path / 'notes.json'
    path.write_text('{nope', encoding='utf-8')
    cache = NoteCache(path)
    assert cache.get('s1', 1.0) is None
    cache.put('s1', 1.0, 'ok')
    assert NoteCache(path).get('s1', 1.0) == 'ok'


# ── summarise_session ────────────────────────────────────────────────────

class FakeRun:
    def __init__(self, stdout='Fixing ByteDog hog ordering.\n', returncode=0, exc=None):
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, 'boom')


SESSION = {'sessionId': 's1', 'name': 'byte-dog-2', 'cwd': r'C:\x', 'status': 'busy'}


def test_summarise_runs_lean_headless_claude(tmp_path):
    run = FakeRun()
    note = summarise_session(SESSION, _transcript(tmp_path, RECORDS),
                             runner=run, claude_exe='claude')
    assert note == 'Fixing ByteDog hog ordering.'
    args, kwargs = run.calls[0]
    assert args[0] == 'claude' and '-p' in args
    for flag in ('--safe-mode', '--no-session-persistence'):
        assert flag in args
    assert args[args.index('--model') + 1] == 'haiku'
    assert args[args.index('--tools') + 1] == ''
    assert 'Fix the hog ordering' in kwargs['input']
    assert kwargs['timeout'] > 0
    if sys.platform == 'win32':
        assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW


def test_summarise_uses_first_line_and_caps_length(tmp_path):
    run = FakeRun(stdout='\n  ' + 'a' * 300 + '\nsecond line\n')
    note = summarise_session(SESSION, _transcript(tmp_path, RECORDS), runner=run, claude_exe='c')
    assert '\n' not in note and len(note) <= 160


def test_summarise_errors_are_explicit(tmp_path):
    t = _transcript(tmp_path, RECORDS)
    with pytest.raises(SummaryError, match='not found'):
        summarise_session(SESSION, t, runner=FakeRun(), claude_exe=None)
    with pytest.raises(SummaryError, match='timed out'):
        summarise_session(SESSION, t, claude_exe='c',
                          runner=FakeRun(exc=subprocess.TimeoutExpired('c', 60)))
    with pytest.raises(SummaryError, match='exit 1'):
        summarise_session(SESSION, t, claude_exe='c', runner=FakeRun(returncode=1))
    with pytest.raises(SummaryError, match='empty'):
        summarise_session(SESSION, t, claude_exe='c', runner=FakeRun(stdout='  \n'))
    with pytest.raises(SummaryError, match='no transcript'):
        summarise_session(SESSION, None, claude_exe='c', runner=FakeRun())


@pytest.mark.skipif(sys.platform != 'win32', reason='cmd shim + taskkill behaviour')
def test_run_killing_tree_kills_grandchild_through_cmd_shim(tmp_path):
    """A .cmd wrapper whose child outlives the timeout must not block the caller."""
    import time
    from session_notes import run_killing_tree
    shim = tmp_path / 'slow.cmd'
    shim.write_text(f'@"{sys.executable}" -c "import time; time.sleep(30)"\n')
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_killing_tree([str(shim)], input='', timeout=1, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    assert time.monotonic() - start < 10


def test_run_killing_tree_returns_output():
    from session_notes import run_killing_tree
    r = run_killing_tree([sys.executable, '-c', 'import sys; print(sys.stdin.read().upper())'],
                         input='hi', timeout=20, text=True)
    assert r.returncode == 0 and r.stdout.strip() == 'HI'
