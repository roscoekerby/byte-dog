"""Unit tests for proc_labels.py: Chromium role labels and Claude session lookup."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proc_labels import (
    chromium_label,
    claude_sessions,
    find_transcript,
    label_processes,
    latest_ai_title,
)


# ── chromium_label ───────────────────────────────────────────────────────

def test_renderer_is_tab_or_page():
    assert chromium_label(['chrome.exe', '--type=renderer']) == 'tab / page'


def test_extension_renderer():
    assert chromium_label(['chrome.exe', '--type=renderer', '--extension-process']) == 'extension'


def test_gpu_and_crashpad():
    assert chromium_label(['x', '--type=gpu-process']) == 'GPU'
    assert chromium_label(['x', '--type=crashpad-handler']) == 'crash reporter'


def test_utility_sub_types_are_named():
    assert chromium_label(['x', '--type=utility',
                           '--utility-sub-type=network.mojom.NetworkService']) == 'network service'
    assert chromium_label(['x', '--type=utility',
                           '--utility-sub-type=audio.mojom.AudioService']) == 'audio service'
    assert chromium_label(['x', '--type=utility',
                           '--utility-sub-type=video_capture.mojom.VideoCaptureService']) \
        == 'video capture service'


def test_browser_process_has_no_type():
    assert chromium_label(['chrome.exe', '--profile-directory=Default']) == 'browser (main)'


def test_webview_names_its_host_app():
    assert chromium_label(['msedgewebview2.exe', '--type=renderer',
                           '--webview-exe-name=WhatsApp.Root.exe']) == 'WhatsApp.Root: tab / page'
    assert chromium_label(['msedgewebview2.exe',
                           '--webview-exe-name=ms-teams.exe']) == 'ms-teams: browser (main)'


def test_empty_cmdline_is_unknown():
    assert chromium_label([]) == ''
    assert chromium_label(None) == ''


# ── claude_sessions / transcripts ────────────────────────────────────────

def _write_session(d: Path, pid: int, **extra):
    data = {'pid': pid, 'sessionId': f'sess-{pid}', 'cwd': r'C:\work\proj',
            'name': f'proj-{pid}', 'status': 'idle', 'kind': 'interactive'}
    data.update(extra)
    (d / f'{pid}.json').write_text(json.dumps(data), encoding='utf-8')


def test_claude_sessions_reads_json_and_skips_other_files(tmp_path):
    _write_session(tmp_path, 101, status='busy')
    _write_session(tmp_path, 202)
    (tmp_path / '2026-04-01-skinvest-session.tmp').write_text('not json')
    (tmp_path / 'broken.json').write_text('{oops')
    sessions = claude_sessions(tmp_path)
    assert set(sessions) == {101, 202}
    assert sessions[101]['status'] == 'busy'
    assert sessions[202]['name'] == 'proj-202'


def test_claude_sessions_missing_dir_is_empty(tmp_path):
    assert claude_sessions(tmp_path / 'nope') == {}


def _write_transcript(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')


def test_find_transcript_globs_projects(tmp_path):
    t = tmp_path / 'C--work-proj' / 'sess-1.jsonl'
    _write_transcript(t, [{'type': 'user'}])
    assert find_transcript(tmp_path, 'sess-1') == t
    assert find_transcript(tmp_path, 'sess-missing') is None
    assert find_transcript(tmp_path, '') is None


def test_latest_ai_title_takes_last_one(tmp_path):
    t = tmp_path / 'a.jsonl'
    _write_transcript(t, [
        {'type': 'ai-title', 'aiTitle': 'Old title'},
        {'type': 'user', 'message': {'content': 'hi'}},
        {'type': 'ai-title', 'aiTitle': 'Process manager per-process controls'},
        {'type': 'assistant', 'message': {'content': []}},
    ])
    assert latest_ai_title(t) == 'Process manager per-process controls'


def test_latest_ai_title_reads_only_the_tail(tmp_path):
    t = tmp_path / 'big.jsonl'
    filler = [{'type': 'user', 'message': {'content': 'x' * 1000}}] * 400
    _write_transcript(t, [{'type': 'ai-title', 'aiTitle': 'early'}] + filler)
    # title is ~400 KB back: outside a 64 KB tail window -> not found, not slow
    assert latest_ai_title(t, tail_bytes=64 * 1024) == ''
    assert latest_ai_title(t, tail_bytes=1 << 20) == 'early'


def test_latest_ai_title_missing_file(tmp_path):
    assert latest_ai_title(tmp_path / 'none.jsonl') == ''


# ── label_processes ──────────────────────────────────────────────────────

def test_label_processes_combines_sources():
    procs = [
        {'pid': 1, 'name': 'chrome.exe', 'rss': 10},
        {'pid': 2, 'name': 'chrome.exe', 'rss': 10},
        {'pid': 3, 'name': 'claude.exe', 'rss': 10},
        {'pid': 4, 'name': 'notepad.exe', 'rss': 10},
        {'pid': 5, 'name': 'claude.exe', 'rss': 10},
    ]
    cmdlines = {1: ['chrome.exe'], 2: ['chrome.exe', '--type=renderer']}
    titles = {1: ['Inbox - Gmail - Google Chrome'], 4: ['notes.txt - Notepad']}
    sessions = {3: {'name': 'byte-dog-2', 'cwd': r'C:\Utility Apps\byte-dog',
                    'status': 'busy', 'title': 'Hog panel'}}
    labels = label_processes(procs, titles, sessions, cmdlines.get)
    assert labels[1] == 'browser (main): Inbox - Gmail - Google Chrome'
    assert labels[2] == 'tab / page'
    assert labels[3] == 'byte-dog-2 [busy] Hog panel (byte-dog)'
    assert labels[4] == 'notes.txt - Notepad'
    assert labels[5] == 'no session file (subagent or helper)'


def test_label_processes_survives_cmdline_errors():
    def boom(pid):
        raise PermissionError('denied')
    labels = label_processes([{'pid': 9, 'name': 'chrome.exe', 'rss': 1}], {}, {}, boom)
    assert labels[9] == ''
