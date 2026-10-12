"""Network numbers for the Network tab: live rates from counter deltas,
adapters sorted so the connected ones come first, and readable units."""
from __future__ import annotations

import socket
from dataclasses import dataclass, field

UNKNOWN_LINK_SPEEDS = frozenset({0, 4294})   # psutil's "not reported" values (Mbps)
APIPA_PREFIX = '169.254.'                    # self-assigned: no network behind it


@dataclass(frozen=True)
class NetSample:
    t: float
    sent: int
    recv: int
    per_nic: dict = field(default_factory=dict)   # name -> (sent, recv)


@dataclass(frozen=True)
class Rates:
    up: float = 0.0       # bytes per second
    down: float = 0.0
    per_nic: dict = field(default_factory=dict)   # name -> (up, down)


@dataclass(frozen=True)
class Adapter:
    name: str
    up: bool
    ipv4: str
    speed_mbps: int
    sent: int
    recv: int
    loopback: bool

    @property
    def connected(self) -> bool:
        return self.up and not self.loopback and bool(self.ipv4) \
            and not self.ipv4.startswith(APIPA_PREFIX)


def sample(t: float, io_total, io_pernic: dict) -> NetSample:
    return NetSample(t, io_total.bytes_sent, io_total.bytes_recv,
                     {n: (c.bytes_sent, c.bytes_recv) for n, c in io_pernic.items()})


def _rate(new: int, old: int, dt: float) -> float:
    return max(new - old, 0) / dt      # counters reset on adapter restart: never negative


def rates(prev: NetSample | None, cur: NetSample) -> Rates:
    if prev is None or cur.t <= prev.t:
        return Rates()
    dt = cur.t - prev.t
    per_nic = {}
    for name, (sent, recv) in cur.per_nic.items():
        if name in prev.per_nic:
            old_sent, old_recv = prev.per_nic[name]
            per_nic[name] = (_rate(sent, old_sent, dt), _rate(recv, old_recv, dt))
    return Rates(_rate(cur.sent, prev.sent, dt), _rate(cur.recv, prev.recv, dt), per_nic)


def adapters(addrs: dict, stats: dict, io_pernic: dict) -> list:
    """Connected adapters first, then the rest; each group by name so rows
    never reshuffle from tick to tick."""
    rows = []
    for name in sorted(set(addrs) | set(stats)):
        ipv4 = next((a.address for a in addrs.get(name, ())
                     if a.family == socket.AF_INET), '')
        st = stats.get(name)
        io = io_pernic.get(name)
        rows.append(Adapter(
            name=name, up=bool(st and st.isup), ipv4=ipv4,
            speed_mbps=0 if not st or st.speed in UNKNOWN_LINK_SPEEDS else st.speed,
            sent=io.bytes_sent if io else 0, recv=io.bytes_recv if io else 0,
            loopback='loopback' in name.lower() or ipv4.startswith('127.')))
    return [a for a in rows if a.connected] + [a for a in rows if not a.connected]


def fmt_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_rate(bytes_per_s: float) -> str:
    return f"{fmt_bytes(bytes_per_s)}/s"


def fmt_link(mbps: int) -> str:
    if not mbps:
        return ''
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps} Mbps"
