"""Shared fixtures.

Exactly one Tcl interpreter per test session: on Windows, starting a second
Tk() in the same run intermittently fails to read init.tcl, and once that
happens every later Tk() fails too ("invalid command name tcl_findLibrary").
"""
import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope='session')
def dog():
    """The one ByteDogApp (and Tk root) for the whole test session."""
    import bytedog
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bytedog.ByteDogApp, 'start_monitoring', lambda self: None)
        mp.setattr(bytedog.ByteDogApp, 'process_queue', lambda self: None)
        application = bytedog.ByteDogApp()
    yield application
    try:
        application.root.destroy()
    except tk.TclError:
        pass
