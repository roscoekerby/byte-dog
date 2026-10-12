"""Look-and-feel choices that survive a restart: theme and see-through window.
Kept apart from the Guardian config so a bad UI file can never touch it."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ui_theme import THEMES

PREFS_PATH = Path(os.environ.get('APPDATA', str(Path.home()))) / 'ByteDog' / 'ui.json'


@dataclass(frozen=True)
class UiPrefs:
    theme: str = 'system'
    see_through: bool = False


def load(path: Path | None = None) -> UiPrefs:
    """Saved prefs; defaults for anything missing, unreadable or invalid."""
    try:
        raw = json.loads((path or PREFS_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return UiPrefs()
    if not isinstance(raw, dict):
        return UiPrefs()
    prefs = UiPrefs()
    if raw.get('theme') in THEMES:
        prefs = replace(prefs, theme=raw['theme'])
    if isinstance(raw.get('see_through'), bool):
        prefs = replace(prefs, see_through=raw['see_through'])
    return prefs


def save(prefs: UiPrefs, path: Path | None = None) -> None:
    path = path or PREFS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(asdict(prefs), indent=2), encoding='utf-8')
    tmp.replace(path)
