"""On-demand one-line AI notes for Claude Code sessions in the hog panel.

Runs the user's own `claude` CLI headless (existing login, no API key, no new
dependency) on the tail of the session transcript. Never called automatically:
spawning a model process during a RAM alert would add to the pressure.
Notes are cached per session and reused until the transcript changes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from guardian import CONFIG_PATH
from proc_labels import read_tail_records

NOTES_PATH = CONFIG_PATH.parent / 'session_notes.json'
SUMMARY_MODEL = 'haiku'
SUMMARY_TIMEOUT_S = 90
MESSAGE_LIMIT = 40
MESSAGE_MAX_CHARS = 500
TRANSCRIPT_TAIL_BYTES = 512 * 1024
NOTE_MAX_CHARS = 160


def run_killing_tree(args, input=None, timeout=None, **kwargs):
    """subprocess.run lookalike that kills the whole process tree on timeout.
    `claude` resolves to an npm .CMD shim: a plain run() timeout kills only
    cmd.exe, and the node grandchild keeps the pipes open, so run() would
    then block in communicate() until node exits on its own."""
    if sys.platform == 'win32':
        kwargs['creationflags'] = kwargs.get('creationflags', 0)             | subprocess.CREATE_NEW_PROCESS_GROUP
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.kill()
            proc.communicate()
            raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


class SummaryError(RuntimeError):
    """A session note could not be produced; the message is user-facing."""


def _texts(content) -> list:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            out.append(block.get('text') or '')
    return out


def recent_messages(path: Path, limit: int = MESSAGE_LIMIT,
                    max_chars: int = MESSAGE_MAX_CHARS) -> list:
    """Last `limit` user/assistant text messages, each cut to `max_chars`.
    Drops tool calls/results, thinking and injected <system-...> text."""
    msgs = []
    for rec in read_tail_records(path, TRANSCRIPT_TAIL_BYTES):
        if not isinstance(rec, dict) or rec.get('type') not in ('user', 'assistant'):
            continue
        for text in _texts((rec.get('message') or {}).get('content')):
            text = ' '.join(text.split())
            if text and not text.startswith('<'):
                msgs.append(f"{rec['type']}: {text[:max_chars]}")
    return msgs[-limit:]


def build_prompt(session: dict, messages: list) -> str:
    return (
        "You label Claude Code sessions in a task manager so the user can decide "
        "which ones to close. Reply with ONE line (max 120 characters, no quotes, "
        "no preamble) saying what this session is working on and where it is up to.\n\n"
        f"Session: {session.get('name', '?')}\n"
        f"Folder: {session.get('cwd', '?')}\n"
        f"Status: {session.get('status', '?')}\n\n"
        "Recent messages:\n" + '\n'.join(messages))


class NoteCache:
    """{sessionId: {mtime, note}} persisted as JSON. Never raises on read."""

    def __init__(self, path: Path = NOTES_PATH):
        self.path = Path(path)
        try:
            self._data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, session_id: str, mtime: float) -> str | None:
        entry = self._data.get(session_id) or {}
        return entry.get('note') if entry.get('mtime') == mtime else None

    def latest(self, session_id: str) -> str | None:
        return (self._data.get(session_id) or {}).get('note')

    def put(self, session_id: str, mtime: float, note: str) -> None:
        self._data = {**self._data, session_id: {'mtime': mtime, 'note': note}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding='utf-8')


def summarise_session(session: dict, transcript: Path | None,
                      runner=run_killing_tree, claude_exe: str | None = None) -> str:
    """One-line note for a session via headless `claude -p`. Raises SummaryError."""
    if transcript is None:
        raise SummaryError("no transcript found for this session")
    if claude_exe is None:
        raise SummaryError("claude CLI not found on PATH")
    messages = recent_messages(transcript)
    if not messages:
        raise SummaryError("no transcript messages to summarise yet")
    args = [claude_exe, '-p', '--model', SUMMARY_MODEL, '--tools', '',
            '--safe-mode', '--no-session-persistence']
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        result = runner(args, input=build_prompt(session, messages),
                        text=True, encoding='utf-8', errors='replace',
                        timeout=SUMMARY_TIMEOUT_S, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise SummaryError(f"claude timed out after {SUMMARY_TIMEOUT_S}s") from None
    except OSError as e:
        raise SummaryError(f"could not start claude: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
        raise SummaryError(f"claude failed (exit {result.returncode}) {detail[0]}".strip())
    lines = [ln.strip() for ln in (result.stdout or '').splitlines() if ln.strip()]
    if not lines:
        raise SummaryError("claude returned an empty note")
    return lines[0][:NOTE_MAX_CHARS]


def find_claude() -> str | None:
    return shutil.which('claude')
