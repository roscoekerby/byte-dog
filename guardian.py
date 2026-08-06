"""ByteDog thrash-prevention core: escalation engine, hog targeting, config,
Norton-safe process snapshot, self-hardening, and auto-start management.

Pure logic lives here so it can be unit tested without tkinter. All Windows
API access degrades gracefully on failure or other platforms.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

# Processes that must never be auto-killed or suspended.
# python/pythonw are deliberately NOT here: ByteDog protects its own pid via
# self_pid exclusion, and a runaway Python script is a plausible thrash cause.
DEFAULT_PROTECTED = frozenset({
    'system', 'secure system', 'registry', 'idle', 'memory compression',
    'memcompression', 'smss.exe', 'csrss.exe', 'wininit.exe', 'winlogon.exe',
    'services.exe', 'lsass.exe', 'svchost.exe', 'explorer.exe', 'dwm.exe',
    'ntoskrnl.exe', 'fontdrvhost.exe', 'spoolsv.exe', 'audiodg.exe',
    'taskhostw.exe', 'runtimebroker.exe', 'msmpeng.exe', 'vmmem',
    'bytedog.exe',
})

# Chromium-family browsers: only renderer processes are auto-action targets
CHROMIUM_NAMES = frozenset({'chrome.exe', 'msedge.exe', 'brave.exe'})

# Human-readable consequence text for the manual-kill/suspend override
# dialog. select_targets() already keeps the guardian's own automatic
# escalation off these names; this is what the UI shows a user who
# deliberately targets one by hand and wants to know what breaks.
PROTECTED_INFO = {
    'system': "Windows kernel system process — not a real killable process; "
              "Windows will refuse, or forcing it can crash the machine.",
    'secure system': "Virtualization-based security (VBS) container process. "
                     "Not really killable; core to Windows security isolation.",
    'registry': "Backing process for the Windows Registry hive in memory. "
               "Not a normal killable process.",
    'idle': "Kernel idle-time placeholder — not a real process.",
    'memory compression': "Manages RAM-compressed pages. Killing it forces "
                          "those pages out abruptly and can crash your session.",
    'memcompression': "Manages RAM-compressed pages. Killing it forces those "
                      "pages out abruptly and can crash your session.",
    'smss.exe': "Session Manager Subsystem — creates new Windows sessions. "
               "Killing it typically crashes Windows immediately (BSOD or "
               "forced reboot).",
    'csrss.exe': "Client/Server Runtime Subsystem — core to the Win32 "
                "subsystem. Killing it crashes Windows instantly.",
    'wininit.exe': "Windows Initialization process — starts core services at "
                   "boot. Killing it typically crashes the current session.",
    'winlogon.exe': "Handles sign-in, sign-out, the lock screen, and "
                    "Ctrl+Alt+Del. Killing it logs you out immediately and "
                    "can crash the session.",
    'services.exe': "Service Control Manager — starts, stops, and manages "
                    "every Windows service. Killing it typically crashes "
                    "Windows.",
    'lsass.exe': "Local Security Authority — verifies logins and issues "
                "security tokens. Windows deliberately reboots the machine "
                "if this process dies.",
    'svchost.exe': "Generic host for Windows service DLLs — one svchost.exe "
                   "instance may be running networking, audio, Windows "
                   "Update, or other core services. Killing the wrong one "
                   "can cut network/audio/other services and may "
                   "destabilize the session.",
    'explorer.exe': "Windows shell — your desktop, taskbar, Start menu, and "
                    "File Explorer windows. Killing it closes your desktop "
                    "shell (modern Windows usually restarts it "
                    "automatically, but you'll briefly lose the "
                    "taskbar/desktop).",
    'dwm.exe': "Desktop Window Manager — renders window compositing and the "
              "visible output of your desktop. Killing it can blank or "
              "flash the screen and force your session back to the lock "
              "screen while it restarts.",
    'ntoskrnl.exe': "The Windows kernel executive image itself. Cannot "
                    "actually be killed as a normal process.",
    'fontdrvhost.exe': "Font rendering host used by the desktop compositor "
                       "and lock screen. Killing it can break text "
                       "rendering or crash the sign-in/desktop session.",
    'spoolsv.exe': "Print Spooler service. Killing it only breaks printing "
                   "until Windows restarts it — lower risk than most on "
                   "this list.",
    'audiodg.exe': "Windows Audio Device Graph — mixes and applies effects "
                   "to all system audio. Killing it cuts all sound until "
                   "Windows restarts it.",
    'taskhostw.exe': "Hosts DLL-based background Windows tasks triggered by "
                     "Task Scheduler. Killing it interrupts whatever "
                     "background task it's currently running.",
    'runtimebroker.exe': "Manages permission checks for Windows Store / UWP "
                         "apps. Killing it can break a UWP app's permission "
                         "checks or crash it mid-use.",
    'msmpeng.exe': "Windows Defender Antivirus's real-time protection "
                   "engine. Killing it disables real-time malware "
                   "protection until Windows restarts it.",
    'vmmem': "Memory-management placeholder for WSL2/Hyper-V virtual "
            "machines. Killing it can crash your WSL2/Docker/VM sessions "
            "and lose unsaved work inside them.",
    'bytedog.exe': "ByteDog itself — the app currently protecting you from "
                   "this thrash. Killing it removes your ability to rescue "
                   "the system.",
}


def protected_reason(name: str) -> str:
    """Human-readable consequence text for a protected process name, shown
    on the manual kill/suspend override dialog. Falls back to a generic
    message for names on the user's own 'never touch' list that aren't in
    PROTECTED_INFO (DEFAULT_PROTECTED entries are all covered above)."""
    return PROTECTED_INFO.get(
        (name or '').lower(),
        "This process is on your protected list. It has no specific "
        "consequence description on file, but was marked protected "
        "because killing or suspending it is expected to be risky or "
        "disruptive.")

CONFIG_PATH = Path(os.environ.get('APPDATA', str(Path.home()))) / 'ByteDog' / 'config.json'

AUTOSTART_APP_NAME = 'ByteDog'
AUTOSTART_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass(frozen=True)
class Decision:
    tier: str        # action to take: 'warn' | 'suspend' | 'kill'
    real_tier: str   # pressure tier before any alert_only downgrade
    reason: str


@dataclass
class GuardianConfig:
    warn_pct: float = 75.0
    act_pct: float = 85.0
    crit_pct: float = 92.0
    kill_rate_max: int = 3           # max auto-kills per rolling 60s
    mode: str = 'escalate'           # 'escalate' | 'alert_only'
    user_protected: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> 'GuardianConfig':
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in raw.items() if k in known})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path = CONFIG_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2), encoding='utf-8')

    def protected_names(self) -> frozenset:
        return DEFAULT_PROTECTED | {n.lower() for n in self.user_protected}


class EscalationEngine:
    """Pure decision state machine. Feed it (ram_pct, swap_used, now) samples;
    it returns Decisions subject to per-tier cooldowns and the kill rate cap.
    psutil swap sin/sout are always 0 on Windows, so pagefile-usage growth
    rate is the thrash-onset signal."""

    WARN_COOLDOWN = 60.0
    SUSPEND_COOLDOWN = 30.0
    KILL_MIN_INTERVAL = 10.0
    SWAP_GROWTH_BPS = 20 * 1024 * 1024   # bytes/sec of pagefile growth
    SWAP_WARN_MARGIN = 5.0               # swap trigger active above warn_pct - margin

    def __init__(self, config: GuardianConfig):
        self.config = config
        self.enabled = True
        self._last_warn = 0.0
        self._last_suspend = 0.0
        self._last_kill_decision = 0.0
        self._kill_times = deque(maxlen=32)
        self._prev_swap = None   # (now, swap_used)

    def record_kill(self, now: float) -> None:
        self._kill_times.append(now)

    def _kill_allowed(self, now: float) -> bool:
        if now - self._last_kill_decision < self.KILL_MIN_INTERVAL:
            return False
        recent = [t for t in self._kill_times if now - t < 60.0]
        return len(recent) < self.config.kill_rate_max

    def _swap_rate(self, swap_used: int, now: float) -> float:
        prev = self._prev_swap
        self._prev_swap = (now, swap_used)
        if prev is None:
            return 0.0
        prev_t, prev_used = prev
        elapsed = now - prev_t
        if elapsed <= 0:
            return 0.0
        return (swap_used - prev_used) / elapsed

    def evaluate(self, ram_pct: float, swap_used: int, now: float):
        swap_rate = self._swap_rate(swap_used, now)
        if not self.enabled:
            return None

        cfg = self.config
        real_tier = None
        reason = f"RAM {ram_pct:.1f}%"
        if ram_pct >= cfg.crit_pct:
            real_tier = 'kill'
        elif ram_pct >= cfg.act_pct:
            real_tier = 'suspend'
        elif ram_pct >= cfg.warn_pct:
            real_tier = 'warn'
        elif (swap_rate > self.SWAP_GROWTH_BPS
              and ram_pct >= cfg.warn_pct - self.SWAP_WARN_MARGIN):
            real_tier = 'warn'
            reason = (f"pagefile growing {swap_rate / (1024 * 1024):.0f} MB/s "
                      f"at RAM {ram_pct:.1f}% (swap thrash onset)")
        if real_tier is None:
            return None

        effective = 'warn' if cfg.mode == 'alert_only' else real_tier

        if effective == 'kill':
            if not self._kill_allowed(now):
                return None
            self._last_kill_decision = now
        elif effective == 'suspend':
            if now - self._last_suspend < self.SUSPEND_COOLDOWN:
                return None
            self._last_suspend = now
        else:
            if now - self._last_warn < self.WARN_COOLDOWN:
                return None
            self._last_warn = now

        return Decision(tier=effective, real_tier=real_tier, reason=reason)


# ── Hog targeting ────────────────────────────────────────────────────────


def select_targets(procs, protected, suspended_pids=frozenset(), n=1, self_pid=None):
    """Rank auto-action candidates by RSS. Chromium processes qualify only
    when classified as renderers ('ctype' == 'renderer')."""
    candidates = []
    for p in procs:
        name = (p.get('name') or '').lower()
        if not name or name in protected:
            continue
        if p.get('pid') in suspended_pids or p.get('pid') == self_pid:
            continue
        if name in CHROMIUM_NAMES and p.get('ctype') != 'renderer':
            continue
        if (p.get('rss') or 0) <= 0:
            continue
        candidates.append(p)
    return sorted(candidates, key=lambda p: p['rss'], reverse=True)[:n]


def group_by_name(procs):
    """Sum RSS per process name for display (Chrome's 60 processes -> 1 row)."""
    groups = {}
    for p in procs:
        name = p.get('name') or '?'
        g = groups.setdefault(name, {'name': name, 'rss': 0, 'count': 0})
        g['rss'] += p.get('rss') or 0
        g['count'] += 1
    return sorted(groups.values(), key=lambda g: g['rss'], reverse=True)


def classify_chromium_cmdline(cmdline) -> str:
    args = cmdline or []
    if any(a.startswith('--type=renderer') for a in args):
        return 'renderer'
    if any(a.startswith('--type=') for a in args):
        return 'helper'
    return 'browser'


def enrich_chromium(procs, limit=40):
    """Attach 'ctype' to Chromium processes via psutil cmdline reads.
    Bounded handle count: only Chromium pids, only up to `limit` biggest."""
    import psutil
    chromium = [p for p in procs if (p.get('name') or '').lower() in CHROMIUM_NAMES]
    for p in sorted(chromium, key=lambda p: p.get('rss') or 0, reverse=True)[:limit]:
        try:
            p['ctype'] = classify_chromium_cmdline(psutil.Process(p['pid']).cmdline())
        except Exception:
            p['ctype'] = 'unknown'
    return procs


# ── Norton-safe process snapshot ─────────────────────────────────────────
# One NtQuerySystemInformation syscall returns pid/name/working-set for every
# process with zero per-process handles, so AV OpenProcess hooks add nothing.

if sys.platform == 'win32':
    from ctypes import wintypes

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [('Length', wintypes.USHORT),
                    ('MaximumLength', wintypes.USHORT),
                    ('Buffer', ctypes.c_void_p)]

    class _SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('NextEntryOffset', wintypes.ULONG),
            ('NumberOfThreads', wintypes.ULONG),
            ('WorkingSetPrivateSize', ctypes.c_longlong),
            ('HardFaultCount', wintypes.ULONG),
            ('NumberOfThreadsHighWatermark', wintypes.ULONG),
            ('CycleTime', ctypes.c_ulonglong),
            ('CreateTime', ctypes.c_longlong),
            ('UserTime', ctypes.c_longlong),
            ('KernelTime', ctypes.c_longlong),
            ('ImageName', _UNICODE_STRING),
            ('BasePriority', ctypes.c_long),
            ('UniqueProcessId', ctypes.c_void_p),
            ('InheritedFromUniqueProcessId', ctypes.c_void_p),
            ('HandleCount', wintypes.ULONG),
            ('SessionId', wintypes.ULONG),
            ('UniqueProcessKey', ctypes.c_void_p),
            ('PeakVirtualSize', ctypes.c_size_t),
            ('VirtualSize', ctypes.c_size_t),
            ('PageFaultCount', wintypes.ULONG),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
        ]

    _SystemProcessInformation = 5


def _snapshot_ntquery():
    ntdll = ctypes.WinDLL('ntdll')
    size = ctypes.c_ulong(1 << 20)
    for _ in range(8):
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQuerySystemInformation(
            _SystemProcessInformation, buf, size, ctypes.byref(size))
        if status == 0:
            break
        if status & 0xFFFFFFFF != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08X}")
        size = ctypes.c_ulong(size.value + (1 << 20))
    else:
        raise OSError("NtQuerySystemInformation: buffer never large enough")

    procs = []
    offset = 0
    base = ctypes.addressof(buf)
    while True:
        info = _SYSTEM_PROCESS_INFORMATION.from_address(base + offset)
        pid = info.UniqueProcessId or 0
        if pid:
            img = info.ImageName
            name = (ctypes.wstring_at(img.Buffer, img.Length // 2)
                    if img.Buffer and img.Length else '?')
            procs.append({'pid': int(pid), 'name': name,
                          'rss': int(info.WorkingSetSize)})
        if info.NextEntryOffset == 0:
            break
        offset += info.NextEntryOffset
    return procs


def fast_memory_snapshot():
    """(pid, name, rss) for all processes. Norton-safe syscall first,
    psutil per-process fallback if it fails."""
    if sys.platform == 'win32':
        try:
            return _snapshot_ntquery()
        except Exception:
            pass
    import psutil
    procs = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            mi = proc.info.get('memory_info')
            procs.append({'pid': proc.info['pid'],
                          'name': proc.info.get('name') or '?',
                          'rss': mi.rss if mi else 0})
        except Exception:
            continue
    return procs


# ── Elevation ─────────────────────────────────────────────────────────────
# Kill/suspend failures ("AccessDenied") and a partial self-hardening
# (working-set pin silently no-ops without admin, see harden_self) both trace
# back to one root cause: ByteDog usually isn't elevated. Self-relaunch with
# a UAC prompt closes that gap instead of just telling the user to re-launch
# manually — the whole point of the guardian is to act *during* a thrash,
# when fighting with Explorer to right-click "Run as administrator" is
# exactly the kind of interaction that becomes impossible.


def is_admin() -> bool:
    """True if the current process token has administrator privileges."""
    if sys.platform != 'win32':
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate_command() -> tuple:
    """(executable, params) used to relaunch ByteDog elevated via ShellExecuteW."""
    if getattr(sys, 'frozen', False):
        return sys.executable, ' '.join(f'"{a}"' for a in sys.argv[1:])
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    script = Path(__file__).with_name('bytedog.py')
    args = [str(script)] + sys.argv[1:]
    return str(interpreter), ' '.join(f'"{a}"' for a in args)


def relaunch_elevated() -> bool:
    """Relaunch ByteDog with a UAC elevation prompt. Returns True if a new
    elevated instance was launched (caller should exit this one); False if
    the user declined the prompt or the relaunch failed (caller should keep
    running non-elevated rather than block on it)."""
    if sys.platform != 'win32':
        return False
    exe, params = _elevate_command()
    try:
        # >32 = success per ShellExecute docs (32 itself is the last error
        # code, everything <=32 is a failure); 1223 = user cancelled the UAC
        # prompt, which ShellExecuteW also reports via a <=32 return value.
        result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', exe, params, None, 1)
        return result > 32
    except Exception:
        return False


def enable_debug_privilege() -> str:
    """Enable SeDebugPrivilege on the current process token so kill/suspend
    can reach processes outside the normal same-user ACL (e.g. another user's
    session, or a process with a hardened DACL) — still subject to the
    DEFAULT_PROTECTED denylist, which is a decision, not a permission. Only
    takes effect when already elevated; never raises."""
    if sys.platform != 'win32':
        return 'skipped (not Windows)'

    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002
    ERROR_NOT_ALL_ASSIGNED = 1300

    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Luid', LUID), ('Attributes', wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [('PrivilegeCount', wintypes.DWORD),
                    ('Privileges', LUID_AND_ATTRIBUTES * 1)]

    # Without explicit argtypes/restype, ctypes marshals the -1 pseudo-handle
    # from GetCurrentProcess() as a 32-bit value on 64-bit Windows, silently
    # corrupting it before it reaches OpenProcessToken (fails as "invalid
    # handle"). Every handle-bearing call below must declare its signature.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    htoken = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(htoken)):
        return f'debug privilege failed: OpenProcessToken error {ctypes.get_last_error()}'

    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, 'SeDebugPrivilege', ctypes.byref(luid)):
            return f'debug privilege failed: LookupPrivilegeValue error {ctypes.get_last_error()}'

        tp = TOKEN_PRIVILEGES(PrivilegeCount=1,
                               Privileges=(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED),))
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(htoken, False, ctypes.byref(tp), 0, None, None):
            return f'debug privilege failed: AdjustTokenPrivileges error {ctypes.get_last_error()}'
        if ctypes.get_last_error() == ERROR_NOT_ALL_ASSIGNED:
            return 'debug privilege not granted (requires admin)'
        return 'debug privilege: enabled'
    finally:
        kernel32.CloseHandle(htoken)


# ── Self-hardening ───────────────────────────────────────────────────────


def harden_self(min_ws_mb: int = 60, max_ws_mb: int = 250) -> list:
    """Keep ByteDog's rescue UI alive during a thrash: HIGH priority class and
    a hard-minimum pinned working set so Windows cannot page us out.
    Returns a list of human-readable result strings; never raises."""
    results = []
    if sys.platform != 'win32':
        return ['skipped (not Windows)']
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetProcessWorkingSetSizeEx.argtypes = [
        wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    handle = kernel32.GetCurrentProcess()

    HIGH_PRIORITY_CLASS = 0x00000080
    if kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS):
        results.append('priority: HIGH')
    else:
        results.append(f'priority failed (err {ctypes.get_last_error()})')

    QUOTA_LIMITS_HARDWS_MIN_ENABLE = 0x1
    QUOTA_LIMITS_HARDWS_MAX_DISABLE = 0x8
    ok = kernel32.SetProcessWorkingSetSizeEx(
        handle,
        ctypes.c_size_t(min_ws_mb * 1024 * 1024),
        ctypes.c_size_t(max_ws_mb * 1024 * 1024),
        wintypes.DWORD(QUOTA_LIMITS_HARDWS_MIN_ENABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE))
    if ok:
        results.append(f'working set pinned: {min_ws_mb} MB min')
    else:
        results.append(f'working set pin failed (err {ctypes.get_last_error()}, needs admin)')
    return results


# ── Auto-start (per-user registry Run key) ───────────────────────────────


def _autostart_command() -> str:
    """Command line used to relaunch ByteDog at logon."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    script = Path(__file__).with_name('bytedog.py')
    return f'"{interpreter}" "{script}"'


def install_autostart() -> tuple:
    """Register ByteDog to launch at Windows logon via the per-user Run key.
    No elevation required. Returns (ok, message)."""
    if sys.platform != 'win32':
        return False, 'auto-start is Windows-only'
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH,
                             0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        return True, 'Auto-start installed (runs at logon)'
    except OSError as e:
        return False, f'registry write failed: {e}'


def uninstall_autostart() -> tuple:
    """Remove ByteDog from the per-user Run key. Returns (ok, message)."""
    if sys.platform != 'win32':
        return False, 'auto-start is Windows-only'
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH,
                             0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, AUTOSTART_APP_NAME)
            except FileNotFoundError:
                pass
        return True, 'Auto-start removed'
    except OSError as e:
        return False, f'registry write failed: {e}'


def autostart_installed() -> bool:
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH,
                             0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_APP_NAME)
        return True
    except OSError:
        return False
