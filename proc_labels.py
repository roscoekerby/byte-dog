"""Per-process labels for the hog panel: which Chromium role a chrome.exe is,
which Claude Code session a claude.exe is, and window titles for the rest.

Chromium cannot be broken down per tab from outside the browser (renderers own
no window and their command line carries no URL; see guardian.py's window-title
note), so Chromium processes are labelled by role instead. Claude Code writes
~/.claude/sessions/<pid>.json for every live CLI process, which maps a PID to
its session name, working folder and busy/idle status.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

CLAUDE_HOME = Path.home() / '.claude'
SESSIONS_DIR = CLAUDE_HOME / 'sessions'
PROJECTS_DIR = CLAUDE_HOME / 'projects'

LABELLED_CHROMIUM = frozenset({'chrome.exe', 'msedge.exe', 'brave.exe', 'msedgewebview2.exe'})
CLAUDE_NAMES = frozenset({'claude.exe'})

_TYPE_LABELS = {
    'renderer': 'tab / page',
    'gpu-process': 'GPU',
    'crashpad-handler': 'crash reporter',
    'ppapi': 'plugin',
}
_UTILITY_LABELS = {
    'network.mojom.NetworkService': 'network service',
    'audio.mojom.AudioService': 'audio service',
    'storage.mojom.StorageService': 'storage service',
    'video_capture.mojom.VideoCaptureService': 'video capture service',
    'data_decoder.mojom.DataDecoderService': 'data decoder',
}
TITLE_TAIL_BYTES = 256 * 1024


def _arg_value(args: list, prefix: str) -> str:
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return ''


def chromium_label(cmdline: Iterable[str] | None) -> str:
    """Human role of one Chromium process from its command line. WebView2
    processes are prefixed with the app that hosts them (Teams, WhatsApp...)."""
    args = list(cmdline or [])
    if not args:
        return ''
    ptype = _arg_value(args, '--type=')
    if not ptype:
        role = 'browser (main)'
    elif ptype == 'renderer' and '--extension-process' in args:
        role = 'extension'
    elif ptype == 'utility':
        sub = _arg_value(args, '--utility-sub-type=')
        role = _UTILITY_LABELS.get(sub) or f"utility: {sub.split('.')[0] or '?'}"
    else:
        role = _TYPE_LABELS.get(ptype, ptype)
    host = _arg_value(args, '--webview-exe-name=')
    if host:
        return f"{host.removesuffix('.exe')}: {role}"
    return role


def claude_sessions(sessions_dir: Path = SESSIONS_DIR) -> dict:
    """{pid: session dict} from Claude Code's per-PID session files. Skips
    unreadable files and the unrelated *-session.tmp notes in the same folder."""
    sessions = {}
    try:
        files = list(Path(sessions_dir).glob('*.json'))
    except OSError:
        return {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            sessions[int(data['pid'])] = data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sessions


def find_transcript(projects_dir: Path, session_id: str) -> Path | None:
    """Locate <projects>/<any project>/<session_id>.jsonl."""
    if not session_id:
        return None
    try:
        return next(Path(projects_dir).glob(f'*/{session_id}.jsonl'), None)
    except OSError:
        return None


def read_tail_records(path: Path, tail_bytes: int) -> list:
    """Parse the JSON lines in the last `tail_bytes` of a transcript. The
    first (possibly cut) line is dropped. Never raises."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.split(b'\n')
    if size > tail_bytes:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def latest_ai_title(path: Path, tail_bytes: int = TITLE_TAIL_BYTES) -> str:
    """Most recent auto-generated session title in a transcript's tail."""
    for rec in reversed(read_tail_records(path, tail_bytes)):
        if isinstance(rec, dict) and rec.get('type') == 'ai-title' and rec.get('aiTitle'):
            return str(rec['aiTitle'])
    return ''


def claude_label(session: dict | None) -> str:
    if not session:
        return 'no session file (subagent or helper)'
    folder = Path(session.get('cwd') or '').name
    parts = [session.get('name') or session.get('sessionId', '?')[:8],
             f"[{session.get('status') or '?'}]"]
    if session.get('title'):
        parts.append(session['title'])
    if folder:
        parts.append(f'({folder})')
    return ' '.join(parts)


def label_processes(procs: list, window_titles: dict, sessions: dict,
                    cmdline_of: Callable[[int], list | None]) -> dict:
    """{pid: label} for every process. `cmdline_of` is only called for
    Chromium processes; any error from it leaves that label blank."""
    labels = {}
    for p in procs:
        pid, name = p['pid'], (p.get('name') or '').lower()
        titles = '; '.join(window_titles.get(pid, []))
        if name in LABELLED_CHROMIUM:
            try:
                role = chromium_label(cmdline_of(pid))
            except Exception:
                role = ''
            labels[pid] = f'{role}: {titles}' if role and titles else role or titles
        elif name in CLAUDE_NAMES:
            labels[pid] = claude_label(sessions.get(pid))
        else:
            labels[pid] = titles
    return labels
