import socket
from types import SimpleNamespace as NS

from net_model import (NetSample, adapters, fmt_bytes, fmt_link, fmt_rate, rates, sample)


def _io(sent, recv):
    return NS(bytes_sent=sent, bytes_recv=recv)


def _addr(ip):
    return NS(family=socket.AF_INET, address=ip)


def test_rates_are_bytes_per_second_between_samples():
    a = NetSample(10.0, 1_000, 5_000, {'Wi-Fi': (1_000, 5_000)})
    b = NetSample(12.0, 3_000, 9_000, {'Wi-Fi': (3_000, 9_000)})
    r = rates(a, b)
    assert (r.up, r.down) == (1_000, 2_000)
    assert r.per_nic['Wi-Fi'] == (1_000, 2_000)


def test_first_sample_and_counter_reset_give_zero_not_negative():
    b = NetSample(12.0, 3_000, 9_000, {'Wi-Fi': (3_000, 9_000)})
    assert rates(None, b).up == 0
    reset = NetSample(13.0, 10, 10, {'Wi-Fi': (10, 10)})
    r = rates(b, reset)
    assert (r.up, r.down) == (0, 0) and r.per_nic['Wi-Fi'] == (0, 0)


def test_sample_reads_psutil_shapes():
    s = sample(1.0, _io(5, 6), {'Wi-Fi': _io(5, 6)})
    assert (s.sent, s.recv, s.per_nic) == (5, 6, {'Wi-Fi': (5, 6)})


def test_connected_adapters_come_first_and_dead_ones_are_flagged():
    addrs = {
        'Wi-Fi': [_addr('192.168.0.230')],
        'Ethernet': [_addr('169.254.222.183')],
        'Norton VPN': [_addr('169.254.219.222')],
        'Loopback Pseudo-Interface 1': [_addr('127.0.0.1')],
        'Busy LAN': [_addr('10.0.0.5')],
    }
    stats = {
        'Wi-Fi': NS(isup=True, speed=913), 'Ethernet': NS(isup=False, speed=0),
        'Norton VPN': NS(isup=False, speed=4294),
        'Loopback Pseudo-Interface 1': NS(isup=True, speed=1073),
        'Busy LAN': NS(isup=True, speed=1000),
    }
    io = {'Wi-Fi': _io(10, 10), 'Busy LAN': _io(500, 500)}
    rows = adapters(addrs, stats, io)
    assert [a.name for a in rows if a.connected] == ['Busy LAN', 'Wi-Fi']
    assert [a.name for a in rows[:2]] == ['Busy LAN', 'Wi-Fi']
    loop = next(a for a in rows if a.name.startswith('Loopback'))
    assert loop.loopback and not loop.connected
    vpn = next(a for a in rows if a.name == 'Norton VPN')
    assert vpn.speed_mbps == 0 and not vpn.connected


def test_formatting():
    assert fmt_bytes(512) == '512 B'
    assert fmt_bytes(6.62 * 1024 ** 3) == '6.6 GB'
    assert fmt_rate(1536) == '1.5 KB/s'
    assert fmt_link(913) == '913 Mbps' and fmt_link(1000) == '1 Gbps' and fmt_link(0) == ''
