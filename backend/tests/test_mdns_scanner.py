"""
Synthetic tests for passive mDNS mode tracking (mdns_scanner).

The bootloader and the application announce the same service name and differ
only in TXT, so the device table must follow the TXT of whatever packet arrived
last — a probe seen by the sniffer or a response seen by zeroconf — and must not
be reverted by a stale cached TXT read moments later. No network is used.

Run standalone:
    /home/jacob/venv-browser-viewer/bin/python backend/tests/test_mdns_scanner.py
(also discoverable by pytest as test_* functions).
"""

import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from zeroconf import ServiceStateChange

from mdns_scanner import MDNSScanner, SERVICE_TYPE, _parse_txt

NAME = 'vs1.' + SERVICE_TYPE
MAC = '02:00:00:00:00:01'


def _txt(**kv) -> bytes:
    out = b''
    for k, v in kv.items():
        item = f'{k}={v}'.encode()
        out += bytes([len(item)]) + item
    return out


BOOT = _txt(mode='boot', sensor_id=1, fw_bl=23, hw=3, mac=MAC)
APP = _txt(mode='app', sensor_id=1, fw_app=4151, hw=3, mac=MAC)


class _Info:
    """Just enough of zeroconf.ServiceInfo for MDNSScanner._on_change."""
    def __init__(self, raw):
        self.properties = _parse_txt(raw)
        self.addresses = [bytes([192, 0, 2, 10])]
        self.server = 'vs1.local.'
        self.port = 56671


class _ZC:
    def __init__(self, raw):
        self.raw = raw

    def get_service_info(self, *_a, **_k):
        return _Info(self.raw)


def _scanner_with_boot_entry():
    sc = MDNSScanner()
    sc._on_change(_ZC(BOOT), SERVICE_TYPE, NAME, ServiceStateChange.Added)
    assert sc.get_device_by_mac(MAC)['mode'] == 'boot'
    return sc


def test_parse_txt():
    t = _parse_txt(APP)
    assert t[b'mode'] == b'app' and t[b'fw_app'] == b'4151' and t[b'mac'] == MAC.encode()
    assert _parse_txt(b'') == {}
    print('  ok  parse_txt')


def test_txt_switches_mode_and_version_fields():
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, APP, src_ip='192.0.2.10')
    d = sc.get_device_by_mac(MAC)
    assert d['mode'] == 'app' and d['fw_app'] == '4151' and d['fw_bl'] == ''
    sc._on_txt(None, NAME, BOOT)
    d = sc.get_device_by_mac(MAC)
    assert d['mode'] == 'boot' and d['fw_bl'] == '23' and d['fw_app'] == ''
    print('  ok  TXT switches mode, fw_app/fw_bl follow the announcing image')


def test_stale_cache_read_does_not_revert_mode():
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, APP)
    # The browser callback for the same announcement reads the old TXT from cache.
    sc._on_change(_ZC(BOOT), SERVICE_TYPE, NAME, ServiceStateChange.Updated)
    assert sc.get_device_by_mac(MAC)['mode'] == 'app'
    print('  ok  stale cached TXT does not undo a just-announced mode')


def test_requery_of_stale_cache_never_reverts_mode():
    # Announcement lost, only probes seen: the cache keeps the boot TXT for its
    # whole TTL, and the 30 s re-query reads it back long after the switch.
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, APP, src_ip='192.0.2.10')
    sc._devices[NAME]['_txt_at'] -= 30
    sc._on_change(_ZC(BOOT), SERVICE_TYPE, NAME, ServiceStateChange.Updated)
    d = sc.get_device_by_mac(MAC)
    assert d['mode'] == 'app' and d['age_s'] < 1
    print('  ok  re-query of a stale cache refreshes liveness, keeps the mode')


def test_src_ip_updates_address():
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, APP, src_ip='192.0.2.77')
    assert sc.get_device_by_mac(MAC)['ip'] == '192.0.2.77'
    print('  ok  sniffer source address follows a changed lease')


def test_txt_without_mac_or_unknown_name_without_zc_is_ignored():
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, _txt(mode='app'))
    assert sc.get_device_by_mac(MAC)['mode'] == 'boot'
    sc._on_txt(None, 'vs1.' + SERVICE_TYPE, APP)   # unknown, no zc to resolve
    assert len(sc.get_devices()) == 1
    print('  ok  TXT without mac / unresolvable new name ignored')


def test_bootloader_version_kept_through_app_run():
    # Apps before a 0x1A bootloader report bootloader_version 0; the version
    # the bootloader announced at boot is the only true one, so keep it.
    sc = _scanner_with_boot_entry()
    sc._on_txt(None, NAME, APP)
    sc._on_change(_ZC(APP), SERVICE_TYPE, NAME, ServiceStateChange.Updated)
    d = sc.get_device_by_mac(MAC)
    assert d['mode'] == 'app' and d['fw_bl'] == '' and d['bl_seen'] == '23'
    print('  ok  bootloader version heard at boot kept while the app runs')


# ── standalone runner ───────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    print(f'Running {len(tests)} tests...\n')
    for t in tests:
        t()
    print(f'\nAll {len(tests)} tests passed.')


if __name__ == '__main__':
    _run_all()
