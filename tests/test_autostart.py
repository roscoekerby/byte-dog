"""Auto-start Run-key handling (real HKCU registry, under a throwaway test key)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import guardian  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows registry')

TEST_KEY = r"Software\ByteDogTests\Run"


@pytest.fixture
def run_key(monkeypatch):
    import winreg
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY).Close()
    monkeypatch.setattr(guardian, 'AUTOSTART_KEY_PATH', TEST_KEY)
    yield
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\ByteDogTests")


def stored_command() -> str | None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        try:
            return winreg.QueryValueEx(key, guardian.AUTOSTART_APP_NAME)[0]
        except FileNotFoundError:
            return None


def test_refresh_rewrites_stale_command(run_key, monkeypatch):
    guardian.install_autostart()
    assert stored_command() == guardian._autostart_command()
    monkeypatch.setattr(guardian, '_autostart_command', lambda: '"C:\new\ByteDog.exe"')
    changed, _ = guardian.refresh_autostart()
    assert changed is True
    assert stored_command() == '"C:\new\ByteDog.exe"'


def test_refresh_is_noop_when_current(run_key):
    guardian.install_autostart()
    changed, _ = guardian.refresh_autostart()
    assert changed is False
    assert stored_command() == guardian._autostart_command()


def test_refresh_respects_removed_autostart(run_key):
    """A user who removed auto-start must not get it back on the next launch."""
    assert stored_command() is None
    changed, _ = guardian.refresh_autostart()
    assert changed is False
    assert stored_command() is None
