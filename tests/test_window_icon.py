"""Window-icon regressions (Windows taskbar).

Explorer asks a window for its icon (WM_GETICON) the moment it is first shown
and Tk only answers from inside mainloop(). ByteDog maps its root during
__init__ and then blocks on the startup process scan before mainloop(), so
that query times out and Explorer falls back to Tk's class icon (the feather)
for the life of the process. run() must re-announce the icon from inside the
running loop so Explorer asks again when Tk can answer.
"""
import sys
from pathlib import Path

import pytest
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bytedog  # noqa: E402


def own_icon(window) -> int:
    """HICON the window hands to Explorer (WM_GETICON, ICON_BIG). 0 means
    Explorer would fall back to the Tk class icon (the feather)."""
    import ctypes
    window.update_idletasks()
    hwnd = int(window.wm_frame(), 16)
    return ctypes.windll.user32.SendMessageW(hwnd, 0x7F, 1, 0)

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows taskbar behaviour')


@pytest.fixture(scope='module')
def dog():
    """One ByteDogApp (one Tcl interpreter) for the module: creating and
    destroying Tk() per test intermittently fails to read init.tcl on Windows."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bytedog.ByteDogApp, 'start_monitoring', lambda self: None)
        mp.setattr(bytedog.ByteDogApp, 'process_queue', lambda self: None)
        application = bytedog.ByteDogApp()
    yield application
    try:
        application.root.destroy()
    except tk.TclError:
        pass


def test_run_reannounces_root_icon_inside_loop(dog, monkeypatch):
    calls = []
    monkeypatch.setattr(bytedog, 'apply_window_icon', lambda win: calls.append(win))
    monkeypatch.setattr(dog, 'update_metrics', lambda: None)
    monkeypatch.setattr(dog.root, 'mainloop', lambda: None)
    dog.run()
    assert calls == []          # scheduled, not called synchronously
    dog.root.update()           # first pass of the event loop
    assert calls == [dog.root]


def test_root_has_own_icon(dog):
    dog.root.update()
    assert own_icon(dog.root) != 0


def test_guardian_alert_has_own_icon(dog):
    dog.show_guardian_alert({'type': 'warn', 'ram_pct': 80.0, 'used_gb': 12.0, 'total_gb': 16.0})
    alert = dog.guardian_alert_window
    assert alert is not None and alert.winfo_exists()
    assert own_icon(alert) != 0


def test_icon_file_has_native_small_sizes():
    """Tk's own .ico parser needs uncompressed (BMP) entries; a PNG-only icon
    falls back to a blurry 32px shell icon."""
    import struct
    data = Path(bytedog.resource_path('ByteDog_256.ico')).read_bytes()
    count = struct.unpack('<H', data[4:6])[0]
    entries = {}
    for i in range(count):
        w, h, _, _, _, bits, _, off = struct.unpack('<BBBBHHII', data[6 + 16 * i:6 + 16 * (i + 1)])
        entries[w or 256] = data[off:off + 4] != b'\x89PNG'
    assert entries.get(16) and entries.get(32) and entries.get(256)
